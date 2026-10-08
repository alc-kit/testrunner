"""The runner: walk a compiled program, one step at a time, on one asyncio loop.

For each step: evaluate `when`, check `requires` against the CURRENT state, set up the
step's fixtures, run the action as a task (so the operator can abort it), map its result
to an outcome, apply what that outcome produces, let observers read reality, check the
plan's expectation, tear down, journal it, and ask the walker what comes next.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TextIO

from .api import (ABORTED, ERRORED, FAILED, PASSED, SKIPPED, Outcome, OutcomeError)
from .config import RunConfig, RunnerConfig, deep_merge
from .discovery import ActionSpec, Registry
from .fixtures import FixtureError, Fixtures, Scope, accepted, call
from .planner import Program, Step, Walker, produced, requires_holds, when_holds
from .proc import Proc
from .store import Store
from .term import Style, color_enabled
from .ui import InputBroker

BUILTIN_RUN = {"run", "config", "store", "ui", "registry"}
BUILTIN_STEP = {"step", "params", "proc", "state"}


@dataclass
class StepResult:
    id: str
    action: str
    outcome: str
    detail: str = ""
    verdict: str = ""
    seconds: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    passed: bool
    steps: list[StepResult]
    why: str = ""


class Background:
    """Tasks started with ctx.spawn(); the owning scope cancels or awaits them at its end."""

    def __init__(self, label: str):
        self.label = label
        self.tasks: list[tuple[asyncio.Task, bool, bool]] = []   # (task, wait, fatal)

    def spawn(self, coro: Awaitable, *, name: str = "", wait: bool = False, fatal: bool = False) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        task.set_name(name or f"{self.label}-bg{len(self.tasks) + 1}")
        self.tasks.append((task, wait, fatal))
        return task

    async def finish(self) -> list[str]:
        """Await `wait` tasks, cancel the rest; return the failures of `fatal` ones."""
        problems = []
        for task, wait, fatal in self.tasks:
            if not wait and not task.done():
                task.cancel()
        for task, wait, fatal in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                if wait and fatal:
                    problems.append(f"background {task.get_name()} was cancelled")
            except Exception as e:  # noqa: BLE001 — reported, not swallowed
                if fatal:
                    problems.append(f"background {task.get_name()} failed: {e}")
        return problems


class RunContext(Background):
    """The `run` fixture."""

    def __init__(self, rc: RunnerConfig, run_config: RunConfig, program: Program, store: Store):
        super().__init__("run")
        self.rc, self.run_config, self.program, self.store = rc, run_config, program, store
        self.root = rc.root
        self.state: dict[str, Any] = {}
        self.results: list[StepResult] = []
        self.current: StepContext | None = None


class StepContext(Background):
    """The `step` fixture."""

    def __init__(self, step: Step, action: ActionSpec, index: int, log_dir: Path, params: dict):
        super().__init__(step.id)
        self.id, self.action, self.index = step.id, action.name, index
        self.spec, self.log_dir, self.params = step, log_dir, params
        self.started = time.monotonic()


class StateView(dict):
    """The `state` fixture: the current state values (a copy; actions report by outcome)."""


class Runner:
    def __init__(self, rc: RunnerConfig, reg: Registry, run_config: RunConfig, store: Store,
                 ui: InputBroker | None = None, echo: TextIO | None = None, color: str = "auto",
                 run_id: str = "", readonly: bool = False):
        self.run_id = run_id or uuid.uuid4().hex[:8]
        # A run of read-only steps takes no writer lock and persists no state: it may run
        # beside a state-changing run in the same directory.
        self.readonly = readonly
        self.rc, self.reg, self.run_config, self.store = rc, reg, run_config, store
        self.ui = ui or InputBroker(interactive=False)
        self.echo = echo if echo is not None else sys.stdout
        self.color = color_enabled(color, self.echo)
        self.style = Style(self.color)
        self.fixtures = Fixtures(reg.fixtures, BUILTIN_RUN | BUILTIN_STEP)
        self._current_task: asyncio.Task | None = None
        self._aborted = False

    # ── reporting ──
    def say(self, text: str) -> None:
        self.echo.write(text + "\n")
        self.echo.flush()

    async def hook(self, name: str, **available: Any) -> None:
        for fn in self.reg.hooks.get(name, []):
            await call(fn, accepted(fn, available))

    # ── operator commands ──
    def _cmd_status(self, run: RunContext) -> Callable[[str], None]:
        def status(_: str) -> None:
            cur = run.current
            if cur is None:
                self.ui.say("between steps")
            else:
                self.ui.say(f"running {cur.id} ({cur.action}) for {time.monotonic() - cur.started:.0f}s; "
                            f"state {run.state}")
        return status

    def _cmd_abort(self, _: str) -> None:
        self._aborted = True
        self.ui.say("aborting: cancelling the current step; the system is left as it is")
        if self._current_task is not None:
            self._current_task.cancel()

    # ── the run ──
    async def run(self, program: Program) -> RunResult:
        if self.readonly:
            return await self._run(program)
        with self.store.lock():
            return await self._run(program)

    def journal(self, event: str, **fields: Any) -> None:
        self.store.journal.append(event, run=self.run_id, **fields)

    async def _run(self, program: Program) -> RunResult:
        rc = self.rc
        data = deep_merge(self.run_config.data, {rc.params_section: program.params})
        run = RunContext(rc, self.run_config, program, self.store)
        run_scope = Scope("run", {"run": run, "config": data, "store": self.store, "ui": self.ui,
                                  "registry": self.reg})
        self.ui.attach()
        self.ui.command("status", self._cmd_status(run))
        self.ui.command("abort", self._cmd_abort)
        walker = Walker(program)
        state = {var: spec.get("initial") for var, spec in rc.states.items()}
        state.update({k: v for k, v in self.store.get_state().items() if k in state})
        run.state = state
        result = RunResult(True, run.results)
        self.journal("run_start", config=str(self.run_config.file or ""),
                                  steps=[s.id for s in program.main], state=state, pid=os.getpid(),
                     readonly=self.readonly)
        try:
            for var in self.reg.observers:
                state[var] = await self._observe(var, run_scope)
            await self.hook("run_start", run=run)
            cur = walker.start()
            visits: dict[str, int] = {}
            index = 0
            while cur is not None:
                step = walker.current(cur)
                index += 1
                visits[step.id] = visits.get(step.id, 0) + 1
                if visits[step.id] > step.max_visits:
                    sr = StepResult(step.id, step.action, ERRORED,
                                    f"visited more than max_visits={step.max_visits} times")
                    run.results.append(sr)
                    result.passed, result.why = False, sr.detail
                    break
                sr = await self._step(run, run_scope, step, index, data)
                if self._aborted:
                    result.passed, result.why = False, "aborted by the operator"
                    break
                t = walker.next(cur, sr.outcome)
                sr.verdict = t.verdict
                if t.verdict == "failed":
                    result.passed, result.why = False, t.why
                elif t.verdict == "diverted":
                    self.say(f"     {t.why}")
                cur = t.cursor
        finally:
            problems = await run.finish()
            await run_scope.close()
            self.ui.detach()
            if problems and result.passed:
                result.passed, result.why = False, "; ".join(problems)
            self.journal("run_end", passed=result.passed, why=result.why, state=run.state)
            rw = rc.release_when
            if (not self.readonly and rw and self.store.get_scenario() is not None
                    and all(run.state.get(k) == v for k, v in rw.items())):
                self.store.release_scenario()
                self.journal("scenario_released", by="release_when", state=run.state)
                self.say(self.style.dim(f"scenario released: the state reached {rw}"))
        await self.hook("run_end", run=run, result=result)
        self._summary(result)
        return result

    async def _observe(self, var: str, scope: Scope) -> Any:
        fn = self.reg.observers[var]
        value = await call(fn, await self.fixtures.kwargs_for(fn, scope))
        if value is not None and value not in self.rc.states[var]["values"]:
            raise FixtureError(f"observer for {var} returned {value!r}, not one of {self.rc.states[var]['values']}")
        return value

    async def _step(self, run: RunContext, run_scope: Scope, step: Step, index: int, data: dict) -> StepResult:
        rc = self.rc
        act = self.reg.actions[step.action]
        params = deep_merge(data.get(rc.params_section) or {}, step.with_)
        step_data = deep_merge(data, {rc.params_section: params})
        log_dir = self.store.path("logs", "x").parent
        ctx = StepContext(step, act, index, log_dir, params)
        run.current = ctx
        started = time.monotonic()
        if not when_holds(step.when, step_data):
            sr = StepResult(step.id, act.name, SKIPPED, "when: does not hold", state=dict(run.state))
            self.say(self.style.dim(f"---- {step.id}: skipped (when)"))
            self.journal("step_skip", step=step.id, action=act.name, why="when")
            run.results.append(sr)
            run.current = None
            return sr
        self.say("\n" + self.style.banner(f"==== [{index}] {step.id}"
                                         + (f" ({act.name})" if act.name != step.id else "") + " ===="))
        self.journal("step_start", step=step.id, action=act.name, params=params)
        await self.hook("step_start", run=run, step=ctx)
        ok, unknown = requires_holds(act.requires, run.state)
        if ok and unknown:
            # Static validation may only warn about an unknown value; running on one would
            # make `requires` a guard that silently lets anything through.
            outcome = Outcome(ERRORED, f"requires {', '.join(unknown)}, whose value is unknown "
                              f"(declare `initial`, or export an observer)")
        elif not ok:
            outcome = Outcome(ERRORED, f"requires {act.requires}, state is "
                              f"{ {k: run.state.get(k) for k in act.requires} }")
        else:
            proc = Proc(rc.root, log_dir, self.ui, self.echo,
                        env={"TR_STEP": step.id, "TR_ACTION": act.name, "TR_RUN_ID": self.run_id,
                             "TR_STATE_DIR": str(self.store.root), "TR_PARAMS": json.dumps(params),
                             "TR_CONFIG_FILE": str(self.run_config.file or ""),
                             "TR_STEP_WITH": json.dumps(step.with_)},
                        prompt_idle=float(rc.input.get("prompt_idle", 20)), color=self.color)
            scope = Scope("step", {"step": ctx, "params": params, "proc": proc,
                                   "state": StateView(run.state)}, parent=run_scope)
            outcome = await self._execute(act, scope, ctx, step_data)
            problems = await ctx.finish()
            try:
                await scope.close()
            except Exception as e:  # noqa: BLE001
                problems.append(f"fixture teardown failed: {e}")
            if problems and outcome.name == PASSED:
                outcome = Outcome(FAILED, "; ".join(problems))
        if outcome.name not in act.all_outcomes:
            outcome = Outcome(ERRORED, f"returned undeclared outcome {outcome.name!r} ({outcome.detail})")
        # state: what the outcome produces, then what reality says
        if outcome.name not in (ERRORED, ABORTED):
            run.state.update(produced(act, outcome.name))
        expect = run.program.expected(step.id)
        if outcome.name not in (ABORTED,):
            for var in set(produced(act, outcome.name)) | set(expect.state):
                if var in self.reg.observers:
                    seen = await self._observe(var, run_scope)
                    if seen != run.state.get(var):
                        self.say(self.style.warn(f"     observed {var}={seen!r} (the model said {run.state.get(var)!r})"))
                    run.state[var] = seen
        if outcome.name == expect.outcome:
            wrong = {k: (v, run.state.get(k)) for k, v in expect.state.items() if run.state.get(k) != v}
            if wrong:
                outcome = Outcome(FAILED, "expected state not reached: " + ", ".join(
                    f"{k}={want!r} (is {have!r})" for k, (want, have) in wrong.items()))
        if not self.readonly:
            self.store.set_state(run.state)
        sr = StepResult(step.id, act.name, outcome.name, outcome.detail,
                        seconds=time.monotonic() - started, state=dict(run.state))
        run.results.append(sr)
        run.current = None
        self.journal("step_end", step=step.id, action=act.name, outcome=outcome.name,
                                  detail=outcome.detail, seconds=round(sr.seconds, 1), state=run.state)
        as_expected = outcome.name == expect.outcome
        mark = "" if as_expected else f"  (expected {expect.outcome})"
        line = (f"---- {step.id}: {outcome.name}{mark} ({sr.seconds:.1f}s)"
                + (f" — {outcome.detail}" if outcome.detail else ""))
        self.say(self.style.ok(line) if as_expected else self.style.bad(line))
        await self.hook("step_end", run=run, step=ctx, result=sr)
        return sr

    async def _execute(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Outcome:
        async def body() -> Any:
            if act.kind == "shell":
                return await self._shell(act, scope, ctx)
            kwargs = await self.fixtures.kwargs_for(act.fn, scope)
            return await call(act.fn, kwargs)

        self._current_task = asyncio.ensure_future(body())
        try:
            value = await self._current_task
        except asyncio.CancelledError:
            if self._aborted:
                return Outcome(ABORTED, "aborted by the operator")
            raise
        except OutcomeError as e:
            return e.outcome
        except FixtureError as e:
            return Outcome(ERRORED, str(e))
        except Exception as e:  # noqa: BLE001 — an action's exception is its failure
            tb = traceback.format_exc()
            (ctx.log_dir / f"{ctx.id}.traceback").write_text(tb)
            return Outcome(FAILED, f"{type(e).__name__}: {e}")
        finally:
            self._current_task = None
        if value is None or value is True:
            return Outcome(PASSED)
        if value is False:
            return Outcome(FAILED)
        if isinstance(value, Outcome):
            return value
        if isinstance(value, str):
            return Outcome(value)
        return Outcome(ERRORED, f"action returned {type(value).__name__}; want None/bool/Outcome")

    async def _shell(self, act: ActionSpec, scope: Scope, ctx: StepContext) -> Outcome:
        spec = act.shell or {}
        params = ctx.params
        fmt = _Format(params)
        run = spec["run"]
        argv = [fmt(w) for w in run] if isinstance(run, list) else ["bash", "-c", fmt(run)]
        env = {k: fmt(str(v)) for k, v in (spec.get("env") or {}).items()}
        cwd = self.rc.root / fmt(spec["cwd"]) if spec.get("cwd") else self.rc.root
        rules = spec.get("rules")
        if isinstance(rules, str):
            rules = self.rc.root / fmt(rules)
        proc: Proc = scope.values["proc"]
        r = await proc.run(argv, cwd=cwd, env=env, log=f"{ctx.id}.log", rules=rules,
                           timeout=spec.get("timeout"))
        if r.unanswered is not None:
            return Outcome(FAILED, f"unanswered prompt: {r.unanswered!r}")
        if r.timed_out:
            return Outcome(FAILED, f"timed out after {spec.get('timeout')}s")
        mapped = spec.get("exit_codes", {}).get(r.returncode)
        if mapped:
            return Outcome(mapped, f"exit {r.returncode}")
        return Outcome(PASSED) if r.returncode == 0 else Outcome(FAILED, f"exit {r.returncode}")

    def _summary(self, result: RunResult) -> None:
        self.say("")
        for sr in result.steps:
            line = f"  {sr.outcome:<8} {sr.id}" + (f"  [{sr.verdict}]" if sr.verdict not in ("", "ok") else "")
            self.say(self.style.bad(line) if sr.verdict == "failed" else line)
        self.say(self.style.ok("==== PASSED ====") if result.passed
                 else self.style.bad(f"==== FAILED: {result.why} ===="))


class _Format:
    """`{name}` in shell argv/env, from the step's params; `{a[b]}` reaches nested ones."""

    def __init__(self, params: dict):
        self.params = params

    def __call__(self, text: str) -> str:
        try:
            return str(text).format_map(self.params)
        except (KeyError, IndexError) as e:
            raise OutcomeError(ERRORED, f"{text!r}: no parameter {e}") from None
