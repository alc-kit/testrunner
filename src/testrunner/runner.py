"""The runner: walk a compiled program, one step at a time, on one asyncio loop.

For each step: evaluate `when`, check `requires` against the CURRENT state, set up the
step's fixtures, run the action as a task (so the operator can abort it), map its result
to an outcome, apply what that outcome produces, let observers read reality, check the
plan's expectation, tear down, journal it, and ask the walker what comes next.
"""
from __future__ import annotations

import asyncio
import json
import logging
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
from .proc import DEFAULT_PROMPT_PATTERN, Proc
from . import report
from .log import STEP
from .nolog import Secrets, values_at
from .store import Store
from .term import Style, color_enabled, forward_colour
from .ui import InputBroker

log = logging.getLogger(__name__)

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
        log.debug("background task %s started (wait=%s, fatal=%s)", task.get_name(), wait, fatal)
        return task

    async def finish(self) -> list[str]:
        """Await `wait` tasks, cancel the rest; return the failures of `fatal` ones."""
        problems = []
        for task, wait, fatal in self.tasks:
            if not wait and not task.done():
                log.debug("background task %s: cancelled at the end of %s", task.get_name(), self.label)
                task.cancel()
        for task, wait, fatal in self.tasks:
            try:
                await task
                log.debug("background task %s: finished", task.get_name())
            except asyncio.CancelledError:
                if wait and fatal:
                    problems.append(f"background {task.get_name()} was cancelled")
            except Exception as e:  # noqa: BLE001 — reported, not swallowed
                log.warning("background task %s failed (fatal=%s): %s", task.get_name(), fatal, e,
                            exc_info=True)
                if fatal:
                    problems.append(f"background {task.get_name()} failed: {e}")
        return problems


class RunContext(Background):
    """The `run` fixture."""

    def __init__(self, rc: RunnerConfig, run_config: RunConfig, program: Program, store: Store):
        super().__init__("run")
        self.rc, self.run_config, self.program, self.store = rc, run_config, program, store
        self.root = rc.root
        self.secrets = Secrets(values_at(run_config.data, run_config.nolog_paths))
        self.state: dict[str, Any] = {}
        self.results: list[StepResult] = []
        self.current: StepContext | None = None

    def nolog(self, value: Any) -> Any:
        """Mark a value NOLOG from here on (e.g. a password an action generated); returns it."""
        log.debug("a value was marked NOLOG at run time")
        return self.secrets.add(value)


class StepContext(Background):
    """The `step` fixture."""

    def __init__(self, step: Step, action: ActionSpec, index: int, log_dir: Path, params: dict):
        super().__init__(step.id)
        self.id, self.action, self.index = step.id, action.name, index
        self.spec, self.log_dir, self.params = step, log_dir, params
        self.started = time.monotonic()
        self.metrics: dict[str, Any] = {}

    def metric(self, name: str, value: Any) -> None:
        """Record a number (or short text) for this step: journalled, shown in the report."""
        log.debug("metric %s=%r", name, value)
        self.metrics[name] = value


class StateView(dict):
    """The `state` fixture: the current state values (a copy; actions report by outcome)."""


class Runner:
    def __init__(self, rc: RunnerConfig, reg: Registry, run_config: RunConfig, store: Store,
                 ui: InputBroker | None = None, echo: TextIO | None = None, color: str = "auto",
                 run_id: str = "", readonly: bool = False, report_formats: tuple[str, ...] = ()):
        self.report_formats = report_formats      # empty: no report (only --report asks for one)
        self.run_id = run_id or uuid.uuid4().hex[:8]
        # A run of read-only steps takes no writer lock and persists no state: it may run
        # beside a state-changing run in the same directory.
        self.readonly = readonly
        self.rc, self.reg, self.run_config, self.store = rc, reg, run_config, store
        self.ui = ui or InputBroker(interactive=False)
        self.echo = echo if echo is not None else sys.stdout
        self.style = Style(color_enabled(color, self.echo))   # the runner's own lines
        self.color = forward_colour(color)                     # the children's colour
        self.force_color = color == "always"
        self.fixtures = Fixtures(reg.fixtures, BUILTIN_RUN | BUILTIN_STEP)
        self._current_task: asyncio.Task | None = None
        self._aborted = False
        # one Secrets for the whole run: config-marked values now, run.nolog() ones later
        self.secrets = Secrets(values_at(run_config.data, run_config.nolog_paths))

    # ── reporting ──
    def say(self, text: str) -> None:
        self.echo.write(self.secrets.text(text) + "\n")
        self.echo.flush()

    async def hook(self, name: str, **available: Any) -> None:
        for fn in self.reg.hooks.get(name, []):
            kwargs = accepted(fn, available)
            log.debug("hook %s: calling %s(%s)", name, _fn_name(fn), ", ".join(kwargs))
            t = time.monotonic()
            try:
                await call(fn, kwargs)
            except BaseException as e:
                log.debug("hook %s: %s raised %s: %s", name, _fn_name(fn), type(e).__name__, e)
                raise
            log.debug("hook %s: %s done (%.2fs)", name, _fn_name(fn), time.monotonic() - t)

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
        log.warning("the operator asked to abort")
        self._aborted = True
        self.ui.say("aborting: cancelling the current step; the system is left as it is")
        if self._current_task is not None:
            self._current_task.cancel()

    # ── reality before the plan is judged ──
    async def observe(self, program: Program, state: dict) -> dict:
        """The state the observers report right now, over `state` (the stored one). Run BEFORE
        the static plan check, so a system that exists but that this store never saw (built
        by other tooling, or the store was cleared) is judged by what it IS."""
        if not self.reg.observers:
            return state
        data = deep_merge(self.run_config.data, {self.rc.params_section: program.params})
        run = RunContext(self.rc, self.run_config, program, self.store)
        run.state = dict(state)
        scope = Scope("run", {"run": run, "config": data, "store": self.store, "ui": self.ui,
                              "registry": self.reg})
        out = dict(state)
        token = STEP.set("(observe)")
        try:
            for var in self.reg.observers:
                out[var] = await self._observe(var, scope)
        finally:
            await scope.close()
            STEP.reset(token)
        return out

    # ── the run ──
    async def run(self, program: Program) -> RunResult:
        if self.readonly:
            log.info("read-only run: no writer lock taken")
            return await self._run(program)
        log.debug("taking the writer lock %s/.lock", self.store.root)
        with self.store.lock():
            log.info("writer lock held: %s/.lock", self.store.root)
            return await self._run(program)

    def journal(self, event: str, **fields: Any) -> None:
        self.store.journal.append(event, run=self.run_id, **self.secrets.obj(fields))

    async def _run(self, program: Program) -> RunResult:
        rc = self.rc
        data = deep_merge(self.run_config.data, {rc.params_section: program.params})
        run = RunContext(rc, self.run_config, program, self.store)
        run.secrets = self.secrets
        run_scope = Scope("run", {"run": run, "config": data, "store": self.store, "ui": self.ui,
                                  "registry": self.reg})
        self.ui.attach()
        self.ui.command("status", self._cmd_status(run))
        self.ui.command("abort", self._cmd_abort)
        walker = Walker(program)
        state = {var: spec.get("initial") for var, spec in rc.states.items()}
        stored = self.store.get_state()
        state.update({k: v for k, v in stored.items() if k in state})
        ignored = sorted(set(stored) - set(state))
        if ignored:
            log.info("the store holds state variable(s) the runner config no longer declares, ignored: %s",
                     ignored)
        log.info("run %s starts: state %s (initial %s, stored %s); main path %s", self.run_id, state,
                 {v: spec.get("initial") for v, spec in rc.states.items()}, stored,
                 [s.id for s in program.main])
        run.state = state
        result = RunResult(True, run.results)
        self.journal("run_start", config=str(self.run_config.file or ""),
                                  steps=[s.id for s in program.main], state=state, pid=os.getpid(),
                     readonly=self.readonly)
        try:
            token = STEP.set("(observe)")
            try:
                for var in self.reg.observers:
                    state[var] = await self._observe(var, run_scope)
            finally:
                STEP.reset(token)
            try:
                await self.hook("run_start", run=run)
            except OutcomeError as e:
                log.error("a run_start hook refused the run: %s", e.outcome.detail or e)
                # a run_start hook may refuse the run (an invalid config, a missing tool):
                # nothing has run yet, and the run fails with the hook's reason
                result.passed, result.why = False, f"refused before the first step: {e.outcome.detail or e}"
                self.say(self.style.bad(f"refused before the first step: {e.outcome.detail or e}"))
                return result
            cur = walker.start()
            visits: dict[str, int] = {}
            index = 0
            while cur is not None:
                step = walker.current(cur)
                index += 1
                visits[step.id] = visits.get(step.id, 0) + 1
                log.debug("cursor %s -> step %s (action %s), visit %d of max %d", _cursor(cur), step.id,
                          step.action, visits[step.id], step.max_visits)
                if visits[step.id] > step.max_visits:
                    sr = StepResult(step.id, step.action, ERRORED,
                                    f"visited more than max_visits={step.max_visits} times")
                    log.error("%s: %s — the run stops", step.id, sr.detail)
                    run.results.append(sr)
                    result.passed, result.why = False, sr.detail
                    break
                token = STEP.set(step.id)
                try:
                    sr = await self._step(run, run_scope, step, index, data)
                finally:
                    STEP.reset(token)
                if self._aborted:
                    log.warning("aborted by the operator after %s", step.id)
                    result.passed, result.why = False, "aborted by the operator"
                    break
                t = walker.next(cur, sr.outcome)
                sr.verdict = t.verdict
                log.info("after %s ended %s: verdict %s%s; next %s", step.id, sr.outcome, t.verdict,
                         f" ({t.why})" if t.why else "",
                         walker.current(t.cursor).id if t.cursor is not None else "(end of run)")
                if t.verdict == "failed":
                    result.passed, result.why = False, t.why
                elif t.verdict == "diverted":
                    self.say(f"     {t.why}")
                cur = t.cursor
        finally:
            problems = await run.finish()
            log.debug("closing the run scope (run fixtures tear down in reverse order)")
            await run_scope.close()
            self.ui.detach()
            if problems and result.passed:
                log.error("run-scope background problems fail the run: %s", problems)
                result.passed, result.why = False, "; ".join(problems)
            self.journal("run_end", passed=result.passed, why=result.why, state=run.state)
            log.info("run %s ends %s%s; state %s", self.run_id, "PASSED" if result.passed else "FAILED",
                     f" ({result.why})" if result.why else "", run.state)
            rw = rc.release_when
            if not self.readonly and rw and self.store.get_scenario() is not None:
                holds = all(run.state.get(k) == v for k, v in rw.items())
                log.info("scenario.release_when %s against state %s: %s", rw, run.state,
                         "holds -> releasing the scenario" if holds else "does not hold -> scenario stays")
                if holds:
                    self.store.release_scenario()
                    self.journal("scenario_released", by="release_when", state=run.state)
                    self.say(self.style.dim(f"scenario released: the state reached {rw}"))
        await self.hook("run_end", run=run, result=result)
        self._summary(result)
        if self.report_formats:
            try:   # a report never changes a run's result
                for path in report.write(self.store, self.run_id, self.report_formats):
                    self.say(self.style.dim(f"report: {path}"))
            except Exception as e:  # noqa: BLE001
                self.say(self.style.warn(f"no report: {e}"))
        return result

    async def _observe(self, var: str, scope: Scope) -> Any:
        fn = self.reg.observers[var]
        log.debug("observer %s: calling %s", var, _fn_name(fn))
        t = time.monotonic()
        value = await call(fn, await self.fixtures.kwargs_for(fn, scope))
        log.info("observer %s says %r (%.2fs)", var, value, time.monotonic() - t)
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
        log.info("step %s (#%d): action %s [%s, from %s]; with %s", step.id, index, act.name, act.kind,
                 act.source, step.with_ or "{}")
        log.debug("%s: params %s", step.id, params)
        if not when_holds(step.when, step_data):
            log.info("%s: skipped — when %s does not hold", step.id, step.when)
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
        log.info("%s: requires %s against state %s -> %s", step.id, act.requires or "{}", run.state,
                 "unknown value(s) " + ", ".join(unknown) if ok and unknown else "holds" if ok else "DOES NOT HOLD")
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
                             "TR_WITH": json.dumps(deep_merge(run.program.params, step.with_))},
                        prompt_idle=float(rc.input.get("prompt_idle", 20)), color=self.color, force_color=self.force_color,
                        prompt_pattern=rc.input.get("prompt_pattern", DEFAULT_PROMPT_PATTERN),
                        secrets=self.secrets, nolog=act.nolog)
            scope = Scope("step", {"step": ctx, "params": params, "proc": proc,
                                   "state": StateView(run.state)}, parent=run_scope)
            outcome = await self._execute(act, scope, ctx, step_data)
            log.info("%s: the action ended %s%s (%.1fs)", step.id, outcome.name,
                     f" — {outcome.detail}" if outcome.detail else "", time.monotonic() - started)
            problems = await ctx.finish()
            try:
                log.debug("%s: closing the step scope (step fixtures tear down in reverse order)", step.id)
                await scope.close()
            except Exception as e:  # noqa: BLE001
                log.error("%s: fixture teardown failed: %s", step.id, e, exc_info=True)
                problems.append(f"fixture teardown failed: {e}")
            if problems and outcome.name == PASSED:
                log.warning("%s: passed, but %s -> failed", step.id, "; ".join(problems))
                outcome = Outcome(FAILED, "; ".join(problems))
        if outcome.name not in act.all_outcomes:
            log.error("%s: outcome %r is not declared by %s (declares %s) -> errored", step.id, outcome.name,
                      act.name, sorted(act.all_outcomes))
            outcome = Outcome(ERRORED, f"returned undeclared outcome {outcome.name!r} ({outcome.detail})")
        # state: what the outcome produces, then what reality says
        before = dict(run.state)
        changed = produced(act, outcome.name, run.state) if outcome.name not in (ERRORED, ABORTED) else {}
        run.state.update(changed)
        log.info("%s: outcome %s produces %s (%s) -> state %s", step.id, outcome.name, changed or "nothing",
                 "errored/aborted produce nothing" if outcome.name in (ERRORED, ABORTED)
                 else f"declared: {act.produces if outcome.name == PASSED else act.produces_on.get(outcome.name, {})}",
                 run.state)
        expect = run.program.expected(step.id)
        if outcome.name not in (ABORTED,):
            for var in set(changed) | set(expect.state):
                if var in self.reg.observers:
                    seen = await self._observe(var, run_scope)
                    if seen != run.state.get(var):
                        log.warning("%s: observed %s=%r, the model said %r — reality wins", step.id, var, seen,
                                    run.state.get(var))
                        self.say(self.style.warn(f"     observed {var}={seen!r} (the model said {run.state.get(var)!r})"))
                    run.state[var] = seen
        if outcome.name == expect.outcome:
            wrong = {k: (v, run.state.get(k)) for k, v in expect.state.items() if run.state.get(k) != v}
            log.debug("%s: expected outcome %s reached; expect.state %s -> %s", step.id, expect.outcome,
                      expect.state or "{}", f"wrong {wrong}" if wrong else "ok")
            if wrong:
                outcome = Outcome(FAILED, "expected state not reached: " + ", ".join(
                    f"{k}={want!r} (is {have!r})" for k, (want, have) in wrong.items()))
        else:
            log.info("%s: ended %s, the plan expected %s", step.id, outcome.name, expect.outcome)
        if not self.readonly:
            if run.state != before:
                log.info("%s: state %s -> %s (written to the store)", step.id, before, run.state)
            self.store.set_state(run.state)
        outcome = Outcome(outcome.name, self.secrets.text(outcome.detail))
        sr = StepResult(step.id, act.name, outcome.name, outcome.detail,
                        seconds=time.monotonic() - started, state=dict(run.state))
        run.results.append(sr)
        run.current = None
        # step_end hooks run BEFORE the journal line, so the metrics they record land in it
        await self.hook("step_end", run=run, step=ctx, result=sr)
        self.journal("step_end", step=step.id, action=act.name, outcome=outcome.name,
                     detail=outcome.detail, seconds=round(sr.seconds, 1), state=run.state,
                     metrics=ctx.metrics)
        as_expected = outcome.name == expect.outcome
        mark = "" if as_expected else f"  (expected {expect.outcome})"
        line = (f"---- {step.id}: {outcome.name}{mark} ({sr.seconds:.1f}s)"
                + (f" — {outcome.detail}" if outcome.detail else ""))
        self.say(self.style.ok(line) if as_expected else self.style.bad(line))
        return sr

    async def _execute(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Outcome:
        async def body() -> Any:
            if act.kind == "shell":
                return await self._shell(act, scope, ctx)
            kwargs = await self.fixtures.kwargs_for(act.fn, scope)
            log.debug("%s: calling %s(%s)", ctx.id, _fn_name(act.fn), ", ".join(kwargs))
            return await call(act.fn, kwargs)

        self._current_task = asyncio.ensure_future(body())
        try:
            value = await self._current_task
        except asyncio.CancelledError:
            if self._aborted:
                log.warning("%s: cancelled by the operator's abort", ctx.id)
                return Outcome(ABORTED, "aborted by the operator")
            log.warning("%s: cancelled (not by the operator)", ctx.id)
            raise
        except OutcomeError as e:
            # where it was raised: the frame that decided the outcome
            log.info("%s: raised %s -> outcome %s%s", ctx.id, type(e).__name__, e.outcome.name,
                     f" ({e.outcome.detail})" if e.outcome.detail else "")
            log.debug("%s: raised at:\n%s", ctx.id, _where(e))
            return e.outcome
        except FixtureError as e:
            log.error("%s: a fixture could not be provided: %s -> errored", ctx.id, e, exc_info=True)
            return Outcome(ERRORED, str(e))
        except Exception as e:  # noqa: BLE001 — an action's exception is its failure
            tb = traceback.format_exc()
            (ctx.log_dir / f"{ctx.id}.traceback").write_text(self.secrets.text(tb))
            log.error("%s: the action raised %s: %s -> failed (traceback also in %s.traceback)",
                      ctx.id, type(e).__name__, e, ctx.id, exc_info=True)
            return Outcome(FAILED, self.secrets.text(f"{type(e).__name__}: {e}"))
        finally:
            self._current_task = None
        log.debug("%s: the action returned %r", ctx.id, value)
        if value is None or value is True:
            return Outcome(PASSED)
        if value is False:
            return Outcome(FAILED)
        if isinstance(value, Outcome):
            return value
        if isinstance(value, str):
            return Outcome(value)
        log.error("%s: the action returned a %s, which is not an outcome", ctx.id, type(value).__name__)
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
        log.debug("%s: shell action, argv formatted from params: %s", ctx.id,
                  [argv[0], "<NOLOG action: arguments not logged>"] if act.nolog else argv)
        r = await proc.run(argv, cwd=cwd, env=env, log=f"{ctx.id}.log", rules=rules,
                           timeout=spec.get("timeout"))
        if r.unanswered is not None:
            return Outcome(FAILED, f"unanswered prompt: {r.unanswered!r}")
        if r.timed_out:
            return Outcome(FAILED, f"timed out after {spec.get('timeout')}s")
        mapped = spec.get("exit_codes", {}).get(r.returncode)
        log.info("%s: exit %d -> %s", ctx.id, r.returncode,
                 f"{mapped} (exit_codes)" if mapped else PASSED if r.returncode == 0 else FAILED)
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


def _fn_name(fn: Callable) -> str:
    mod = getattr(fn, "__module__", "") or ""
    mod = mod.rsplit(".", 1)[-1]
    return f"{mod}.{getattr(fn, '__qualname__', fn)}" if mod else str(getattr(fn, "__qualname__", fn))


def _cursor(cur: Any) -> str:
    return " / ".join(f"{seg}[{i}]" for seg, i in cur)


def _where(e: BaseException) -> str:
    """The last frames of an exception's traceback: where an outcome was decided."""
    return "".join(traceback.format_tb(e.__traceback__)[-3:]).rstrip()


class _Format:
    """`{name}` in shell argv/env, from the step's params; `{a[b]}` reaches nested ones."""

    def __init__(self, params: dict):
        self.params = params

    def __call__(self, text: str) -> str:
        try:
            return str(text).format_map(self.params)
        except (KeyError, IndexError) as e:
            raise OutcomeError(ERRORED, f"{text!r}: no parameter {e}") from None
