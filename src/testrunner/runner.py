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
import re
import string
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TextIO

from .api import (ABORTED, BUILTIN_OUTCOMES, ERRORED, FAILED, PASSED, SKIPPED, Outcome, OutcomeError)
from .config import RunConfig, RunnerConfig, deep_merge, lookup
from .discovery import ActionSpec, Registry
from .fixtures import FixtureError, Fixtures, Scope, accepted, call
from .planner import Program, Step, Walker, matrix, produced, requires_holds, when_explained, when_holds
from .proc import DEFAULT_PROMPT_PATTERN, Proc
from . import report
from .log import STEP
from .nolog import Secrets, values_at
from .store import Store
from . import template
from .term import Style, color_enabled, forward_colour
from .ui import InputBroker

log = logging.getLogger(__name__)
_ABSENT = object()

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
class WalkEnd:
    """How one walk ended: the run's plan, or a walk action's inner steps (then `outcome` and
    `detail` are what the action itself ends with)."""
    passed: bool
    why: str = ""
    outcome: str = PASSED
    detail: str = ""


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
        self.forwarded: tuple[str, ...] = ()     # the arguments after `--`
        self.secrets = Secrets(values_at(run_config.data, run_config.nolog_paths))
        self.state: dict[str, Any] = {}
        self.results: list[StepResult] = []
        self.current: StepContext | None = None
        self.last_output = ""        # what the last executed step printed (retry evidence)

    def nolog(self, value: Any) -> Any:
        """Mark a value NOLOG from here on (e.g. a password an action generated); returns it."""
        log.debug("a value was marked NOLOG at run time")
        return self.secrets.add(value)


@dataclass
class WalkFrame:
    """A walk action being walked: its spec, the parameters its step was given, its step."""
    walk: Any
    params: dict
    ctx: "StepContext"


class StepContext(Background):
    """The `step` fixture."""

    def __init__(self, step: Step, action: ActionSpec, index: int | str, log_dir: Path, params: dict,
                 sid: str | None = None):
        super().__init__(sid or step.id)
        self.id, self.action, self.index = sid or step.id, action.name, index
        self.spec, self.log_dir, self.params = step, log_dir, params
        self.started = time.monotonic()
        self.metrics: dict[str, Any] = {}
        self.parent: str | None = None      # an inner step: the id of the walk action's step

    @property
    def file_id(self) -> str:
        """The id as a file name: an inner step's `walk/step` becomes `walk.step`."""
        return file_id(self.id)

    def metric(self, name: str, value: Any) -> None:
        """Record a number (or short text) for this step: journalled, shown in the report."""
        log.debug("metric %s=%r", name, value)
        self.metrics[name] = value


class StateView(dict):
    """The `state` fixture: the current state values (a copy; actions report by outcome)."""


class Runner:
    def __init__(self, rc: RunnerConfig, reg: Registry, run_config: RunConfig, store: Store,
                 ui: InputBroker | None = None, echo: TextIO | None = None, color: str = "auto",
                 run_id: str = "", readonly: bool = False, report_formats: tuple[str, ...] = (),
                 show_commands: bool = False, forwarded: tuple[str, ...] = ()):
        self.forwarded = tuple(forwarded)          # the arguments after `--`, for the steps' tools
        self.show_commands = show_commands         # --show-commands: each command, copy-pasteable
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
        self._frames: list[WalkFrame] = []      # the walk actions being walked, innermost last
        self._run_ctx: RunContext | None = None
        self._run_scope: Scope | None = None
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
        run.forwarded = self.forwarded
        run_scope = Scope("run", {"run": run, "config": data, "store": self.store, "ui": self.ui,
                                  "registry": self.reg})
        self._run_ctx, self._run_scope = run, run_scope
        self.ui.attach()
        self.ui.command("status", self._cmd_status(run))
        self.ui.command("abort", self._cmd_abort)
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
        self.journal("run_start", config=str(self.run_config.file or ""), source=program.source,
                     matrix=matrix(program, self.reg),
                                  steps=[s.id for s in program.main], state=state, pid=os.getpid(),
                     readonly=self.readonly, **({"forwarded": list(self.forwarded)} if self.forwarded else {}))
        try:
            # an observer that cannot look is a warning here (as in the CLI's pre-plan look),
            # never a crash: the run then starts from the stored state for that variable
            await self._reobserve(run, run_scope, "(observe)")
            try:
                await self.hook("run_start", run=run)
            except OutcomeError as e:
                log.error("a run_start hook refused the run: %s", e.outcome.detail or e)
                # a run_start hook may refuse the run (an invalid config, a missing tool):
                # nothing has run yet, and the run fails with the hook's reason
                result.passed, result.why = False, f"refused before the first step: {e.outcome.detail or e}"
                self.say(self.style.bad(f"refused before the first step: {e.outcome.detail or e}"))
                return result
            end = await self._walk(run, run_scope, program, data)
            result.passed, result.why = end.passed, end.why
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
                # judged on what IS, never on the model alone: re-read every observed
                # variable release_when names; one that cannot be read keeps the scenario
                blind = await self._reobserve(run, run_scope, "(release)", only=set(rw))
                holds = not blind and all(run.state.get(k) == v for k, v in rw.items())
                log.info("scenario.release_when %s against state %s: %s", rw, run.state,
                         f"cannot observe {sorted(blind)} -> scenario stays" if blind else
                         "holds -> releasing the scenario" if holds else "does not hold -> scenario stays")
                if blind:
                    self.say(self.style.warn(f"scenario kept: could not observe {', '.join(sorted(blind))}"))
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

    async def _walk(self, run: RunContext, run_scope: Scope, program: Program, data: dict,
                    prefix: str = "", label: str = "") -> WalkEnd:
        """Walk one program to its end: the run's plan, or (prefix "<step>/") a walk action's
        inner steps. Step ids in a program are LOCAL; what people and the journal see is
        prefix + id, so a walk inside a walk reads `addnode/install/install`."""
        walker = Walker(program)
        cur = walker.start()
        visits: dict[str, int] = {}
        index = 0
        end = WalkEnd(True)
        last_main = -1                    # the main-path position reached (walks: `always` steps after it)
        early = False                     # the walk ended before its last step (fail / skip / end)
        while cur is not None:
            step = walker.current(cur)
            last_main = max(last_main, cur[0][1])
            index += 1
            num = f"{label}{index}"
            full = prefix + step.id
            visits[step.id] = visits.get(step.id, 0) + 1
            log.debug("cursor %s -> step %s (action %s), visit %d of max %d", _cursor(cur), full,
                      step.action, visits[step.id], step.max_visits)
            if visits[step.id] > step.max_visits:
                sr = StepResult(full, step.action, ERRORED,
                                f"visited more than max_visits={step.max_visits} times")
                log.error("%s: %s — the %s stops", full, sr.detail, "walk" if prefix else "run")
                run.results.append(sr)
                end, early = WalkEnd(False, sr.detail, ERRORED, f"{full}: {sr.detail}"), True
                break
            token = STEP.set(full)
            try:
                sr = await self._step(run, run_scope, step, num, data, program=program, prefix=prefix,
                                      attempt=f"1/{1 + step.retry['times']}" if step.retry else "")
                attempt = 1
                while step.retry and not self._aborted:
                    again, why = await self._retry_wanted(run, run_scope, step, sr, attempt, num, data,
                                                          program, prefix)
                    if not again:
                        if why:
                            log.info("%s: %s", full, why)
                            self.journal("step_no_retry", step=full, attempt=attempt, why=why)
                            self.say(self.style.dim(f"     {why}"))
                        break
                    attempt += 1
                    total = 1 + step.retry["times"]
                    log.info("%s: %s", full, why)
                    self.journal("step_retry", step=full, attempt=attempt, of=total, why=why,
                                 delay=step.retry["delay"])
                    self.say(self.style.warn(f"     {why}"
                                             + (f"; in {step.retry['delay']:g}s" if step.retry["delay"] else "")))
                    if step.retry["delay"]:
                        await asyncio.sleep(step.retry["delay"])
                    sr = await self._step(run, run_scope, step, num, data, program=program, prefix=prefix,
                                          attempt=f"{attempt}/{total}")
            finally:
                STEP.reset(token)
            if self._aborted:
                log.warning("aborted by the operator after %s", full)
                always = [s.id for s in program.main[last_main + 1:] if s.always]
                if always:
                    # an abort hands the system to the operator, as it is: nothing more runs
                    log.warning("aborted: the always step(s) %s do NOT run", [prefix + a for a in always])
                    self.journal("always_not_run", steps=[prefix + a for a in always], why="aborted")
                return WalkEnd(False, "aborted by the operator", ABORTED, "aborted by the operator")
            t = walker.next(cur, sr.outcome, run.state)
            sr.verdict = t.verdict
            log.info("after %s ended %s: verdict %s%s; next %s", full, sr.outcome, t.verdict,
                     f" ({t.why})" if t.why else "",
                     prefix + walker.current(t.cursor).id if t.cursor is not None
                     else "(end of walk)" if prefix else "(end of run)")
            if t.verdict == "failed":
                end = WalkEnd(False, t.why, t.ends or sr.outcome, prefix + t.why
                              + (f" — {sr.detail}" if sr.detail else ""))
                early = True
            elif t.verdict == "ended":         # walks: skip / {end: x}
                end, early = WalkEnd(True, t.why, t.ends or PASSED, t.why), True
                self.say(self.style.dim(f"     {t.why}"))
            elif t.verdict == "ignored":       # walks: recorded, and the walk goes on
                self.journal("step_ignored", step=full, outcome=sr.outcome, why=t.why)
                self.say(self.style.warn(f"     {t.why}"))
            elif t.verdict == "diverted":
                self.say(f"     {t.why}")
            elif t.why:                       # a routing decision on the expected outcome (a rule, a goto)
                self.say(self.style.dim(f"     {t.why}"))
            cur = t.cursor
        if early and prefix:
            end = await self._always(run, run_scope, program, last_main, data, prefix, label, index, end)
        return end

    async def _always(self, run: RunContext, run_scope: Scope, program: Program, last_main: int, data: dict,
                      prefix: str, label: str, index: int, end: WalkEnd) -> WalkEnd:
        """The `always` steps after the position the walk left at, in order, whatever happened.
        Their failure never hides the walk's own: both are recorded; a walk that was ending
        well (skip / end) fails."""
        for step in program.main[last_main + 1:]:
            if not step.always:
                continue
            index += 1
            full = prefix + step.id
            log.info("%s: always — the walk left at %s (%s), this still runs", full,
                     prefix + program.main[last_main].id if last_main >= 0 else "its start", end.why)
            token = STEP.set(full)
            try:
                sr = await self._step(run, run_scope, step, f"{label}{index}", data, program=program, prefix=prefix,
                                      always=True)
            finally:
                STEP.reset(token)
            sr.verdict = "always"
            if sr.outcome != program.expected(step.id).outcome and sr.outcome != SKIPPED:
                what = f"always step {full} ended {sr.outcome}" + (f" — {sr.detail}" if sr.detail else "")
                log.error("%s", what)
                if end.passed:
                    end = WalkEnd(False, what, sr.outcome if sr.outcome in BUILTIN_OUTCOMES else FAILED, what)
                else:
                    end = WalkEnd(False, end.why, end.outcome, f"{end.detail}; {what}")
            if self._aborted:
                break
        return end

    async def _reobserve(self, run: RunContext, scope: Scope, where: str,
                         only: set[str] | None = None) -> set[str]:
        """Read every observed variable (or those in `only`) into run.state; reality wins
        over the model. Returns the variables that could NOT be read — their model value
        stays, and the caller decides what an unread value may not be trusted for."""
        blind: set[str] = set()
        token = STEP.set(where) if where.startswith("(") else None
        try:
            for var in self.reg.observers:
                if only is not None and var not in only:
                    continue
                try:
                    seen = await self._observe(var, scope)
                except Exception as e:  # noqa: BLE001 — an observer that cannot look is reported, not fatal
                    log.warning("%s: could not observe %s (%s: %s); keeping the model's %r", where, var,
                                type(e).__name__, e, run.state.get(var))
                    blind.add(var)
                    continue
                if seen != run.state.get(var):
                    log.warning("%s: observed %s=%r, the model said %r — reality wins", where, var, seen,
                                run.state.get(var))
                    self.say(self.style.warn(f"     observed {var}={seen!r} (the model said {run.state.get(var)!r})"))
                run.state[var] = seen
        finally:
            if token is not None:
                STEP.reset(token)
        return blind

    async def _observe(self, var: str, scope: Scope) -> Any:
        fn = self.reg.observers[var]
        log.debug("observer %s: calling %s", var, _fn_name(fn))
        t = time.monotonic()
        value = await call(fn, await self.fixtures.kwargs_for(fn, scope))
        log.info("observer %s says %r (%.2fs)", var, value, time.monotonic() - t)
        if value is not None and value not in self.rc.states[var]["values"]:
            raise FixtureError(f"observer for {var} returned {value!r}, not one of {self.rc.states[var]['values']}")
        return value

    async def _retry_wanted(self, run: RunContext, run_scope: Scope, step: Step, sr: StepResult, attempt: int,
                            index: str, data: dict, program: Program, prefix: str = "") -> tuple[bool, str]:
        """(retry?, why): the retry spec's evidence, judged on the attempt that just ended."""
        spec = step.retry
        total = 1 + spec["times"]
        if sr.outcome == SKIPPED or sr.outcome == program.expected(step.id).outcome:
            return False, ""                    # skipped by `when`, or it ended as the plan expected
        if sr.outcome == ABORTED:
            return False, f"no retry: attempt {attempt}/{total} was aborted by the operator"
        if attempt >= total:
            return False, f"no retry: attempt {attempt}/{total} was the last ({sr.outcome})"

        async def holds(ev: dict, part: str) -> tuple[bool, list[str]]:
            """if: ALL keys hold; unless: (returns) which keys matched. Cheap evidence first."""
            seen: list[str] = []
            for key in ("outcome", "detail", "output", "state", "probe"):
                if key not in ev:
                    continue
                want = ev[key]
                if key == "outcome":
                    hit, txt = sr.outcome in want, f"outcome {sr.outcome}" + ("" if sr.outcome in want else f" not in {want}")
                elif key in ("detail", "output"):
                    text = sr.detail if key == "detail" else run.last_output
                    hit = re.search(want, text or "", re.M) is not None
                    txt = f"{key} {'matched' if hit else 'did not match'} {want!r}"
                elif key == "state":
                    bad = {k: run.state.get(k) for k, v in want.items()
                           if run.state.get(k) not in (v if isinstance(v, list) else [v])}
                    hit, txt = not bad, f"observed state {'is' if not bad else 'is not'} {want}" + (f" ({bad})" if bad else "")
                else:
                    probe = Step(f"{step.id}~probe{attempt}{'' if part == 'if' else '-unless'}", want)
                    psr = await self._step(run, run_scope, probe, index, data, program=program, prefix=prefix)
                    hit, txt = psr.outcome == PASSED, f"probe {want} {psr.outcome}"
                seen.append(txt)
                if part == "if" and not hit:
                    return False, seen
                if part == "unless" and hit:
                    return True, seen
            return part == "if", seen

        ok, why_if = await holds(spec["if"], "if")
        if not ok:
            return False, f"no retry after attempt {attempt}/{total} ({sr.outcome}): {'; '.join(why_if)}"
        vetoed, why_unless = await holds(spec["unless"], "unless") if spec["unless"] else (False, [])
        if vetoed:
            return False, f"no retry after attempt {attempt}/{total}: unless {why_unless[-1]}"
        return True, f"retry {attempt + 1}/{total}: {'; '.join(why_if)}"

    async def _step(self, run: RunContext, run_scope: Scope, step: Step, index: int | str, data: dict,
                    attempt: str = "", program: Program | None = None, prefix: str = "",
                    always: bool = False) -> StepResult:
        rc = self.rc
        program = program or run.program
        act = self.reg.actions[step.action]
        sid = prefix + step.id            # the id people and the journal see; step.id is local to `program`
        params = deep_merge(data.get(rc.params_section) or {}, step.with_)
        step_data = deep_merge(data, {rc.params_section: params})
        log_dir = self.store.path("logs", "x").parent
        ctx = StepContext(step, act, index, log_dir, params, sid)
        if prefix and self._frames:
            ctx.parent = self._frames[-1].ctx.id
        run.current = ctx
        started = time.monotonic()
        log.info("step %s (#%s): action %s [%s, from %s]; with %s", sid, index, act.name, act.kind,
                 act.source, step.with_ or "{}")
        log.debug("%s: params %s", sid, params)
        if step.when:
            log.info("%s: when — %s", sid, "; ".join(when_explained(step.when, step_data)))
            absent = [k for k in step.when if lookup(step_data, k, _ABSENT) is _ABSENT]
            if absent:
                # a typo here skips the step forever while the run passes: say so loudly
                log.warning("%s: when names %s, which the run config does not contain at all — compared "
                            "as None (a typo, or a key the config never sets?)", sid, absent)
        if not when_holds(step.when, step_data):
            log.info("%s: skipped — when does not hold", sid)
            sr = StepResult(sid, act.name, SKIPPED, "when: does not hold", state=dict(run.state))
            self.say(self.style.dim(f"---- {sid}: skipped (when)"))
            self.journal("step_skip", step=sid, action=act.name, why="when")
            run.results.append(sr)
            run.current = None
            return sr
        self.say("\n" + self.style.banner(f"==== [{index}] {sid}"
                                         + (f" ({act.name})" if act.name != sid else "")
                                         + (f" attempt {attempt}" if attempt else "") + " ===="))
        self.journal("step_start", step=sid, action=act.name, params=params,
                     **({"attempt": attempt} if attempt else {}), **({"always": True} if always else {}))
        run.last_output = ""
        await self.hook("step_start", run=run, step=ctx)
        ok, unknown = requires_holds(act.requires, run.state)
        log.info("%s: requires %s against state %s -> %s", sid, act.requires or "{}", run.state,
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
                        env={"TR_STEP": sid, "TR_ACTION": act.name, "TR_RUN_ID": self.run_id,
                             "TR_STATE_DIR": str(self.store.root), "TR_PARAMS": json.dumps(params),
                             "TR_CONFIG_FILE": str(self.run_config.file or ""),
                             "TR_WITH": json.dumps(deep_merge(run.program.params, step.with_)),
                             "TR_FORWARDED": json.dumps(list(self.forwarded))},
                        prompt_idle=float(rc.input.get("prompt_idle", 20)), color=self.color, force_color=self.force_color,
                        prompt_pattern=rc.input.get("prompt_pattern", DEFAULT_PROMPT_PATTERN),
                        secrets=self.secrets, nolog=act.nolog, forwarded=self.forwarded,
                        show_command=(lambda line: self.say(self.style.dim(line))) if self.show_commands else None)
            scope = Scope("step", {"step": ctx, "params": params, "proc": proc,
                                   "state": StateView(run.state)}, parent=run_scope)
            outcome = await self._execute(act, scope, ctx, step_data)
            run.last_output = "\n".join(proc.transcript)
            log.info("%s: the action ended %s%s (%.1fs)", sid, outcome.name,
                     f" — {outcome.detail}" if outcome.detail else "", time.monotonic() - started)
            problems = await ctx.finish()
            try:
                log.debug("%s: closing the step scope (step fixtures tear down in reverse order)", sid)
                await scope.close()
            except Exception as e:  # noqa: BLE001
                log.error("%s: fixture teardown failed: %s", sid, e, exc_info=True)
                problems.append(f"fixture teardown failed: {e}")
            if problems and outcome.name == PASSED:
                log.warning("%s: passed, but %s -> failed", sid, "; ".join(problems))
                outcome = Outcome(FAILED, "; ".join(problems))
        if outcome.name not in act.all_outcomes:
            log.error("%s: outcome %r is not declared by %s (declares %s) -> errored", sid, outcome.name,
                      act.name, sorted(act.all_outcomes))
            outcome = Outcome(ERRORED, f"returned undeclared outcome {outcome.name!r} ({outcome.detail})")
        # state: what the outcome produces, then what reality says
        before = dict(run.state)
        changed = produced(act, outcome.name, run.state) if outcome.name not in (ERRORED, ABORTED) else {}
        run.state.update(changed)
        log.info("%s: outcome %s produces %s (%s) -> state %s", sid, outcome.name, changed or "nothing",
                 "errored/aborted produce nothing" if outcome.name in (ERRORED, ABORTED)
                 else f"declared: {act.produces if outcome.name == PASSED else act.produces_on.get(outcome.name, {})}",
                 run.state)
        expect = program.expected(step.id)
        # Reality after EVERY step, whatever its outcome: a step that failed (or was
        # aborted) half-way can still have changed the system — measured 2026-10-11: an
        # `up` that built every VM and then failed left the model at `absent`, and
        # release_when then released the scenario over running VMs. A read-only action
        # only looks (it may produce no state), so there is nothing to re-read after it.
        if act.readonly:
            log.debug("%s: read-only action: reality not re-read after it", sid)
        else:
            await self._reobserve(run, run_scope, sid)
        if outcome.name == expect.outcome:
            wrong = {k: (v, run.state.get(k)) for k, v in expect.state.items() if run.state.get(k) != v}
            log.debug("%s: expected outcome %s reached; expect.state %s -> %s", sid, expect.outcome,
                      expect.state or "{}", f"wrong {wrong}" if wrong else "ok")
            if wrong:
                outcome = Outcome(FAILED, "expected state not reached: " + ", ".join(
                    f"{k}={want!r} (is {have!r})" for k, (want, have) in wrong.items()))
        else:
            log.info("%s: ended %s, the plan expected %s", sid, outcome.name, expect.outcome)
        if not self.readonly:
            if run.state != before:
                log.info("%s: state %s -> %s (written to the store)", sid, before, run.state)
            self.store.set_state(run.state)
        outcome = Outcome(outcome.name, self.secrets.text(outcome.detail))
        sr = StepResult(sid, act.name, outcome.name, outcome.detail,
                        seconds=time.monotonic() - started, state=dict(run.state))
        run.results.append(sr)
        run.current = None
        # step_end hooks run BEFORE the journal line, so the metrics they record land in it
        await self.hook("step_end", run=run, step=ctx, result=sr)
        self.journal("step_end", step=sid, action=act.name, outcome=outcome.name,
                     detail=outcome.detail, seconds=round(sr.seconds, 1), state=run.state,
                     metrics=ctx.metrics, **({"attempt": attempt} if attempt else {}))
        as_expected = outcome.name == expect.outcome
        mark = "" if as_expected else f"  (expected {expect.outcome})"
        line = (f"---- {sid}{f' attempt {attempt}' if attempt else ''}: {outcome.name}{mark} ({sr.seconds:.1f}s)"
                + (f" — {outcome.detail}" if outcome.detail else ""))
        self.say(self.style.ok(line) if as_expected else self.style.bad(line))
        return sr

    async def _execute(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Outcome:
        async def body() -> Any:
            return await self._body(act, scope, ctx, step_data)

        # a walk action's inner steps set their own current task: put this one back after them
        outer_task = self._current_task
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
        except template.TemplateError as e:
            log.error("%s: a template could not be rendered: %s -> errored", ctx.id, e)
            return Outcome(ERRORED, self.secrets.text(str(e)))
        except Exception as e:  # noqa: BLE001 — an action's exception is its failure
            tb = traceback.format_exc()
            (ctx.log_dir / f"{ctx.file_id}.traceback").write_text(self.secrets.text(tb))
            log.error("%s: the action raised %s: %s -> failed (traceback also in %s.traceback)",
                      ctx.id, type(e).__name__, e, ctx.file_id, exc_info=True)
            return Outcome(FAILED, self.secrets.text(f"{type(e).__name__}: {e}"))
        finally:
            self._current_task = outer_task
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

    async def _body(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Any:
        if act.kind == "shell":
            return await self._shell(act, scope, ctx)
        if act.kind == "walk":
            return await self._run_walk(act, scope, ctx, step_data)
        if act.kind == "call":
            return await self._call(act, scope, ctx, step_data)
        if act.kind == "cmd":
            return await self._cmd(act, scope, ctx)
        kwargs = await self.fixtures.kwargs_for(act.fn, scope)
        log.debug("%s: calling %s(%s)", ctx.id, _fn_name(act.fn), ", ".join(kwargs))
        return await call(act.fn, kwargs)

    # ── walk actions ──
    async def _run_walk(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Outcome:
        """Walk the action's inner steps (a program of their own, ids `<step>/<inner>`), and
        end with the outcome the walk ended with."""
        w = act.walk
        log.info("%s: walk action %s — inner steps %s; params %s (defaults %s)", ctx.id, act.name,
                 [s.id for s in w.program.main], ctx.params or "{}", w.params or "{}")
        program = self._run_ctx.program.inner.get(ctx.id) or w.program
        if program is not w.program:
            log.info("%s: this run's own copy of the walk (edits / a start inside it): %s", ctx.id,
                     [s.id for s in program.main])
        self._frames.append(WalkFrame(w, ctx.params, ctx))
        try:
            end = await self._walk(self._run_ctx, self._run_scope, program, step_data,
                                   prefix=f"{ctx.id}/", label=f"{ctx.index}.")
        finally:
            self._frames.pop()
        if end.passed and end.outcome == PASSED:
            log.info("%s: the walk went through: passed", ctx.id)
            return Outcome(PASSED)
        if end.passed:                                    # ended by `skip` / `{end: x}`
            log.info("%s: the walk ended %s (%s)", ctx.id, end.outcome, end.why)
            return Outcome(end.outcome, end.detail)
        o = end.outcome
        # a walk that FAILED never ends passed/skipped: an inner step that passed where the walk
        # expected a refusal is exactly the failure (and an undeclared named outcome is failed)
        mapped = o if o in (FAILED, ERRORED, ABORTED) or o in act.outcomes else FAILED
        log.info("%s: the walk failed: %s%s", ctx.id, end.detail,
                 "" if mapped == o else f" -> {FAILED} (the walk does not declare {o!r})")
        return Outcome(mapped, end.detail)

    def _resolver(self, scope: Scope, ctx: StepContext) -> Callable:
        """`resolve(name, arg)` for the templates of the inner step `ctx` (template.py)."""
        frame = self._frames[-1]
        run = self._run_ctx

        async def resolve(name: str, arg: str, depth: int = 0) -> Any:
            if name.startswith("param."):
                key = name[6:]
                v = lookup(frame.params, key, _ABSENT)
                if v is _ABSENT:
                    v = lookup(frame.walk.params, key, _ABSENT)
                    if isinstance(v, (str, list, dict)) and depth < 4:
                        # a default may itself reference: params: {victim: "{role:victim}"}
                        v = await template.render(v, lambda n, a: resolve(n, a, depth + 1))
                if v is _ABSENT:
                    raise template.TemplateError(f"{{{name}}}: no parameter {key!r} (the walk {frame.walk.name} "
                                                 f"declares {sorted(frame.walk.params) or 'none'})")
                if v is None:
                    raise template.TemplateError(f"{{{name}}}: the parameter is null (unset)")
                return v
            if name.startswith("env."):
                if name[4:] not in os.environ:
                    raise template.TemplateError(f"{{{name}}}: not in the environment")
                return os.environ[name[4:]]
            if name.startswith("state."):
                return run.state.get(name[6:])
            facts = {"run.root": str(self.rc.root), "run.id": self.run_id, "run.state_dir": str(self.store.root),
                     "run.config": str(self.run_config.file or ""), "step.id": ctx.id, "step.parent": frame.ctx.id}
            if name in facts:
                return facts[name]
            if name in self.reg.resolvers:
                fn, source = self.reg.resolvers[name]
                kwargs = await self.fixtures.kwargs_for(fn, Scope("resolve", {"arg": arg}, parent=scope))
                value = await call(fn, kwargs)
                log.info("%s: {%s%s} -> %r (resolver %s)", ctx.id, name, f":{arg}" if arg else "", value, source)
                return value
            raise template.TemplateError(f"{{{name}}}: no such reference")
        return resolve

    async def _call(self, act: ActionSpec, scope: Scope, ctx: StepContext, step_data: dict) -> Any:
        """An inner step that runs another action, with its `with:` rendered NOW."""
        target = self.reg.actions[act.call]
        resolve = self._resolver(scope, ctx)
        with_ = await template.render(act.call_with, resolve)
        params = deep_merge(ctx.params, with_)
        log.info("%s: calls %s [%s] with %s", ctx.id, target.name, target.kind, with_ or "{}")
        ctx.params = params
        scope.values["params"] = params
        return await self._body(target, scope, ctx, deep_merge(step_data, {self.rc.params_section: params}))

    async def _cmd(self, act: ActionSpec, scope: Scope, ctx: StepContext) -> Any:
        """An inner command step: render it, run it by its kind, classify what happened."""
        kind, spec, classify = act.inner["kind"], act.inner["spec"], act.inner["classify"]
        resolve = self._resolver(scope, ctx)
        spec = await template.render(spec, resolve)
        evidence = {}
        for name, ev in classify.items():
            evidence[name] = {**ev, **({"output": await template.render(ev["output"], resolve, escape=re.escape)}
                                       if "output" in ev else {})}
        if act.nolog:
            log.info("%s: %s step (NOLOG: its values are not logged)", ctx.id, kind)
        else:
            log.info("%s: %s step %s", ctx.id, kind, spec)
        if kind == "refuse":
            log.info("%s: refuses: %s", ctx.id, spec["refuse"])
            return Outcome(FAILED, str(spec["refuse"]))
        if kind == "run":
            value = await self._run_kind(spec, scope, ctx)
        else:
            fn, _, source = self.reg.step_kinds[kind]
            kwargs = await self.fixtures.kwargs_for(fn, Scope("kind", {"spec": spec}, parent=scope))
            log.debug("%s: kind %s -> %s(%s)", ctx.id, kind, source, ", ".join(kwargs))
            value = await call(fn, kwargs)
        return self._judge(value, evidence, ctx)

    async def _run_kind(self, spec: dict, scope: Scope, ctx: StepContext) -> Any:
        run = spec["run"]
        argv = [str(w) for w in run] if isinstance(run, list) else ["bash", "-c", str(run)]
        env = {k: None if v is None else str(v) for k, v in (spec.get("env") or {}).items()}
        cwd = self.rc.root / str(spec["cwd"]) if spec.get("cwd") else self.rc.root
        rules = spec.get("rules")
        if isinstance(rules, str):
            rules = self.rc.root / rules
        proc: Proc = scope.values["proc"]
        return await proc.run(argv, cwd=cwd, env=env, log=str(spec.get("log") or f"{ctx.file_id}.log"),
                              rules=rules, timeout=spec.get("timeout"))

    def _judge(self, value: Any, evidence: dict, ctx: StepContext) -> Any:
        """A command step's outcome: what its kind said, else the first `outcomes:` evidence
        that holds, else unanswered prompt / timeout / exit code."""
        if not hasattr(value, "returncode"):         # an Outcome, None, a bool: the kind decided
            return value
        r = value
        if r.unanswered is not None:
            log.info("%s: an unanswered prompt -> failed", ctx.id)
            return Outcome(FAILED, f"unanswered prompt: {r.unanswered!r}")
        text = r.text
        for name, ev in evidence.items():
            held, why = _evidence_holds(ev, r.returncode, text, r.timed_out)
            log.info("%s: outcome %s %s: %s", ctx.id, name, "HOLDS" if held else "does not hold", "; ".join(why))
            if held:
                return Outcome(name, "; ".join(why))
        if r.timed_out:
            return Outcome(FAILED, "timed out")
        log.info("%s: exit %d -> %s", ctx.id, r.returncode, PASSED if r.returncode == 0 else FAILED)
        return Outcome(PASSED) if r.returncode == 0 else Outcome(FAILED, f"exit {r.returncode}")

    async def _shell(self, act: ActionSpec, scope: Scope, ctx: StepContext) -> Outcome:
        spec = act.shell or {}
        params = ctx.params
        fmt = _Format(params)
        run = spec["run"]
        argv = [fmt(w) for w in run] if isinstance(run, list) else ["bash", "-c", fmt(run)]
        # a null value UNSETS the variable (as in Proc.run) — never the string "None"
        env = {k: None if v is None else fmt(str(v)) for k, v in (spec.get("env") or {}).items()}
        cwd = self.rc.root / fmt(spec["cwd"]) if spec.get("cwd") else self.rc.root
        rules = spec.get("rules")
        if isinstance(rules, str):
            rules = self.rc.root / fmt(rules)
        proc: Proc = scope.values["proc"]
        log.debug("%s: shell action, argv formatted from params: %s", ctx.id,
                  [argv[0], "<NOLOG action: arguments not logged>"] if act.nolog else argv)
        r = await proc.run(argv, cwd=cwd, env=env, log=f"{ctx.file_id}.log", rules=rules,
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


def file_id(step_id: str) -> str:
    """A step id as a file name: an inner step's `walk/step` becomes `walk.step`."""
    return step_id.replace("/", ".")


def _evidence_holds(ev: dict, returncode: int, text: str, timed_out: bool) -> tuple[bool, list[str]]:
    """Does a classification's evidence hold? ALL keys must; (held, what was seen)."""
    seen = []
    ok = True
    if "exit" in ev:
        want = ev["exit"]
        hit = (returncode == 0 if want == "zero" else returncode != 0 if want == "nonzero"
               else returncode in want if isinstance(want, list) else returncode == want)
        seen.append(f"exit {returncode} {'is' if hit else 'is not'} {want}")
        ok &= hit
    if "timed_out" in ev:
        hit = bool(timed_out) == bool(ev["timed_out"])
        seen.append(f"timed_out {timed_out}")
        ok &= hit
    if "output" in ev:
        pats = ev["output"] if isinstance(ev["output"], list) else [ev["output"]]
        for pat in pats:
            hit = re.search(pat, text, re.M) is not None
            seen.append(f"output {'matched' if hit else 'did not match'} {pat!r}")
            ok &= hit
    return ok, seen


def _fn_name(fn: Callable) -> str:
    mod = getattr(fn, "__module__", "") or ""
    mod = mod.rsplit(".", 1)[-1]
    return f"{mod}.{getattr(fn, '__qualname__', fn)}" if mod else str(getattr(fn, "__qualname__", fn))


def _cursor(cur: Any) -> str:
    return " / ".join(f"{seg}[{i}]" for seg, i in cur)


def _where(e: BaseException) -> str:
    """The last frames of an exception's traceback: where an outcome was decided."""
    return "".join(traceback.format_tb(e.__traceback__)[-3:]).rstrip()


class _NullParameter(Exception):
    pass


class _Formatter(string.Formatter):
    def format_field(self, value: Any, format_spec: str) -> Any:
        if value is None:
            raise _NullParameter()
        return super().format_field(value, format_spec)


class _Format:
    """`{name}` in shell argv/env, from the step's params; `{a[b]}` reaches nested ones.
    A parameter that is null (unset in the config) is REFUSED: it would reach the command
    as the word "None", which no command means."""

    def __init__(self, params: dict):
        self.params = params

    def __call__(self, text: str) -> str:
        try:
            return _Formatter().vformat(str(text), (), self.params)
        except (KeyError, IndexError) as e:
            raise OutcomeError(ERRORED, f"{text!r}: no parameter {e}") from None
        except _NullParameter:
            raise OutcomeError(ERRORED, f"{text!r}: a parameter it names is null (unset in the run "
                                        f"config) — refusing to pass the word 'None'") from None
