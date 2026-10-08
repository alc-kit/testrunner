"""Plans: compile a plan into a program, walk it, and validate it before anything runs.

A PROGRAM is a main sequence of steps (the path, with detours spliced in) plus reaction
segments (steps run when a step ends with a given outcome). A CURSOR is a stack of
(segment, index) frames. `Walker.next()` is the single definition of "what runs after
this step ended with that outcome"; the runtime and the static simulation both use it,
so the plan that was validated is the plan that runs.

Step entry forms (in paths, plan paths, detours and reactions):
  "install"                 an action
  "lifecycle"               a named path, expanded in place
  "up..join", "lifecycle:up..join", "..reboot", "up.."   a range over a path
  {action: install, id: install-2, with: {...}, when: {...}, max_visits: 2}
  {stages: [up, ping], with: {...}}    a group sharing `with` (run-config list form)
  [a, b]                    a nested list, spliced in place
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .api import PASSED, SKIPPED
from .config import ConfigError, RunnerConfig, deep_merge, lookup
from .discovery import ActionSpec, Registry

MAIN = "main"
REACTION_WORDS = ("continue", "stop")


@dataclass
class Step:
    id: str
    action: str
    with_: dict[str, Any] = field(default_factory=dict)
    when: dict[str, Any] | None = None
    max_visits: int = 1


@dataclass
class Reaction:
    kind: str                       # continue | stop | goto | steps
    target: str | None = None       # goto
    segment: str | None = None      # steps: the segment key


@dataclass
class Expect:
    outcome: str = PASSED
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class Program:
    segments: dict[str, list[Step]]
    reactions: dict[str, dict[str, Reaction]]
    expect: dict[str, Expect]
    params: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def main(self) -> list[Step]:
        return self.segments[MAIN]

    def steps(self) -> Iterable[Step]:
        for seg in self.segments.values():
            yield from seg

    def step(self, step_id: str) -> Step:
        for s in self.steps():
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def expected(self, step_id: str) -> Expect:
        return self.expect.get(step_id) or Expect()


# ── compiling ────────────────────────────────────────────────────────────────

class _Ids:
    def __init__(self) -> None:
        self.used: set[str] = set()

    def take(self, base: str, explicit: bool) -> str:
        if explicit:
            if base in self.used:
                raise ConfigError(f"step id {base!r} is used twice")
            self.used.add(base)
            return base
        if base not in self.used:
            self.used.add(base)
            return base
        n = 2
        while f"{base}#{n}" in self.used:
            n += 1
        self.used.add(f"{base}#{n}")
        return f"{base}#{n}"


class Compiler:
    def __init__(self, rc: RunnerConfig, reg: Registry):
        self.rc, self.reg = rc, reg

    # entries -> (action, with, when, id, max_visits) tuples, ids assigned later
    def expand(self, entries: Any, inherited_with: dict | None = None, depth: int = 0,
               where: str = "plan") -> list[dict]:
        if depth > 20:
            raise ConfigError(f"{where}: paths nest more than 20 deep (a path that includes itself?)")
        if isinstance(entries, (str, dict)):
            entries = [entries]
        if not isinstance(entries, list):
            raise ConfigError(f"{where}: want a list of steps, got {type(entries).__name__}")
        out: list[dict] = []
        for e in entries:
            w = dict(inherited_with or {})
            if isinstance(e, str):
                out += self._expand_name(e, w, depth, where)
            elif isinstance(e, list):          # a nested list is spliced in place
                out += self.expand(e, w, depth + 1, where)
            elif isinstance(e, dict) and "stages" in e:
                unknown = set(e) - {"stages", "with"}
                if unknown:
                    raise ConfigError(f"{where}: a stages group takes only stages/with, not {', '.join(sorted(unknown))}")
                out += self.expand(e["stages"], deep_merge(w, e.get("with") or {}), depth + 1, where)
            elif isinstance(e, dict) and "action" in e:
                unknown = set(e) - {"action", "id", "with", "when", "max_visits"}
                if unknown:
                    raise ConfigError(f"{where}: step {e['action']!r}: unknown key(s) {', '.join(sorted(unknown))}")
                name = e["action"]
                if name not in self.reg.actions:
                    raise ConfigError(f"{where}: unknown action {name!r}")
                out.append({"action": name, "id": e.get("id"), "with": deep_merge(w, e.get("with") or {}),
                            "when": e.get("when"), "max_visits": int(e.get("max_visits", 1))})
            else:
                raise ConfigError(f"{where}: cannot read step {e!r}")
        return out

    def _expand_name(self, name: str, w: dict, depth: int, where: str) -> list[dict]:
        if ".." in name:
            return self._expand_range(name, w, depth, where)
        if name in self.rc.paths:
            return self.expand(self.rc.paths[name], w, depth + 1, f"paths.{name}")
        if name in self.reg.actions:
            return [{"action": name, "id": None, "with": w, "when": None, "max_visits": 1}]
        raise ConfigError(f"{where}: {name!r} is neither an action nor a path")

    def _expand_range(self, spec: str, w: dict, depth: int, where: str) -> list[dict]:
        path_name, _, rng = spec.rpartition(":")
        path_name = path_name or self.rc.default_path
        if not path_name:
            raise ConfigError(f"{where}: range {spec!r} needs a path (no default_path is set)")
        if path_name not in self.rc.paths:
            raise ConfigError(f"{where}: range {spec!r}: no path {path_name!r}")
        full = self.expand(self.rc.paths[path_name], w, depth + 1, f"paths.{path_name}")
        names = [s["id"] or s["action"] for s in full]
        a, _, b = rng.partition("..")
        for x, label in ((a, "start"), (b, "end")):
            if x and x not in names:
                raise ConfigError(f"{where}: range {spec!r}: {label} {x!r} is not on path {path_name!r}")
        ia = names.index(a) if a else 0
        ib = names.index(b) if b else len(names) - 1
        if ia > ib:
            raise ConfigError(f"{where}: range {spec!r} runs backwards ({a} comes after {b})")
        return full[ia:ib + 1]

    def compile(self, plan: Any, cli_steps: list[str] | None = None,
                cli_with: dict | None = None) -> Program:
        notes: list[str] = []
        if plan is None:
            plan = {}
        if isinstance(plan, list):          # list form: the steps, nothing else
            plan = {"path": plan}
        if not isinstance(plan, dict):
            raise ConfigError("plan: want a mapping (path/with/detours/on/expect) or a list of steps")
        if True in plan:   # YAML 1.1 reads a bare `on:` key as the boolean true
            plan = {("on" if k is True else k): v for k, v in plan.items()}
        unknown = {str(k) for k in plan} - {"path", "with", "detours", "on", "expect"}
        if unknown:
            raise ConfigError(f"plan: unknown key(s) {', '.join(sorted(unknown))}")
        adhoc = bool(cli_steps)
        path = cli_steps if adhoc else plan.get("path")
        if path is None:
            path = []
        params = deep_merge(plan.get("with") or {}, cli_with or {})
        ids = _Ids()

        def make(entries: list[dict]) -> list[Step]:
            out = []
            for e in entries:
                sid = ids.take(e["id"] or e["action"], explicit=bool(e["id"]))
                out.append(Step(sid, e["action"], e["with"], e["when"], e["max_visits"]))
            return out

        main = make(self.expand(path, where="plan.path"))
        # detours: spliced into main, in the order given
        for i, d in enumerate(plan.get("detours") or []):
            where = f"plan.detours[{i}]"
            if not isinstance(d, dict) or "steps" not in d or len({"after", "before"} & set(d)) != 1:
                raise ConfigError(f"{where}: want {{after: <step>, steps: [...]}} or {{before: ...}}")
            anchor = d.get("after") or d.get("before")
            idx = next((n for n, s in enumerate(main) if s.id == anchor), None)
            if idx is None:
                if adhoc:
                    notes.append(f"detour at {anchor!r} dropped: not in this run")
                    continue
                raise ConfigError(f"{where}: no step {anchor!r} on the path")
            new = make(self.expand(d["steps"], where=where))
            pos = idx + 1 if "after" in d else idx
            main[pos:pos] = new
        segments = {MAIN: main}
        reactions: dict[str, dict[str, Reaction]] = {}
        for sid, by_outcome in (plan.get("on") or {}).items():
            where = f"plan.on.{sid}"
            if not isinstance(by_outcome, dict):
                raise ConfigError(f"{where}: want {{<outcome>: <reaction>}}")
            for outcome, r in by_outcome.items():
                key = f"{sid}!{outcome}"
                if r in REACTION_WORDS:
                    reaction = Reaction(r)
                elif isinstance(r, dict) and set(r) == {"goto"}:
                    reaction = Reaction("goto", target=r["goto"])
                elif isinstance(r, (list, str)):
                    segments[key] = make(self.expand(r, where=where))
                    reaction = Reaction("steps", segment=key)
                else:
                    raise ConfigError(f"{where}.{outcome}: want continue, stop, {{goto: <step>}} or a step list")
                reactions.setdefault(sid, {})[outcome] = reaction
        expect: dict[str, Expect] = {}
        for sid, e in (plan.get("expect") or {}).items():
            if not isinstance(e, dict) or set(e) - {"outcome", "state"}:
                raise ConfigError(f"plan.expect.{sid}: want {{outcome: ..., state: {{...}}}}")
            expect[sid] = Expect(e.get("outcome", PASSED), dict(e.get("state") or {}))
        prog = Program(segments, reactions, expect, params, notes)
        self._check_references(prog, adhoc)
        return prog

    def _check_references(self, prog: Program, adhoc: bool) -> None:
        all_ids = {s.id for s in prog.steps()}
        main_ids = {s.id for s in prog.main}
        for table, label in ((prog.reactions, "on"), (prog.expect, "expect")):
            for sid in list(table):
                if sid not in all_ids:
                    if adhoc:
                        del table[sid]
                        prog.notes.append(f"plan.{label}.{sid} dropped: not in this run")
                        continue
                    raise ConfigError(f"plan.{label}: no step {sid!r} in the plan")
        for sid, by_outcome in prog.reactions.items():
            act = self.reg.actions[prog.step(sid).action]
            for outcome, r in by_outcome.items():
                if outcome not in act.all_outcomes:
                    raise ConfigError(f"plan.on.{sid}: action {act.name!r} never ends {outcome!r} "
                                      f"(it declares {', '.join(sorted(act.all_outcomes))})")
                if r.kind == "goto" and r.target not in main_ids:
                    raise ConfigError(f"plan.on.{sid}.{outcome}: goto {r.target!r} is not a step on the main path")
        for sid, e in prog.expect.items():
            act = self.reg.actions[prog.step(sid).action]
            if e.outcome not in act.all_outcomes:
                raise ConfigError(f"plan.expect.{sid}: action {act.name!r} never ends {e.outcome!r}")
            for var, val in e.state.items():
                if var not in self.rc.states or val not in self.rc.states[var]["values"]:
                    raise ConfigError(f"plan.expect.{sid}: {var}={val!r} is not a declared state value")


# ── walking ──────────────────────────────────────────────────────────────────

Cursor = tuple[tuple[str, int], ...]


def when_holds(when: dict | None, data: dict) -> bool:
    """`when: {dotted.key: value | [values] | {not: value}}` against the run's config data."""
    for key, want in (when or {}).items():
        have = lookup(data, key)
        if isinstance(want, dict) and set(want) == {"not"}:
            bad = want["not"] if isinstance(want["not"], list) else [want["not"]]
            if have in bad:
                return False
        elif isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def requires_holds(requires: dict, state: dict) -> tuple[bool, list[str]]:
    """(ok, unknown vars). An unknown value cannot be judged statically; it is not a failure."""
    unknown = []
    for var, want in requires.items():
        have = state.get(var)
        if have is None:
            unknown.append(var)
            continue
        if have not in (want if isinstance(want, list) else [want]):
            return False, unknown
    return True, unknown


def produced(act: ActionSpec, outcome: str, state: dict) -> dict:
    """The state values an outcome sets. A value is either the new value, or a TRANSITION
    MAP {current: next}: a current value the map does not list stays as it is."""
    spec = act.produces if outcome == PASSED else act.produces_on.get(outcome, {})
    out = {}
    for var, val in spec.items():
        if isinstance(val, dict):
            if state.get(var) in val:
                out[var] = val[state.get(var)]
        else:
            out[var] = val
    return out


@dataclass
class Transition:
    cursor: Cursor | None           # None: the run ends
    verdict: str                    # ok | diverted | failed — what this outcome means for the run
    why: str = ""


class Walker:
    def __init__(self, prog: Program):
        self.prog = prog

    def start(self) -> Cursor | None:
        return ((MAIN, 0),) if self.prog.main else None

    def current(self, cur: Cursor) -> Step:
        seg, i = cur[-1]
        return self.prog.segments[seg][i]

    def _advance(self, cur: Cursor) -> Cursor | None:
        frames = list(cur)
        while frames:
            seg, i = frames.pop()
            if i + 1 < len(self.prog.segments[seg]):
                frames.append((seg, i + 1))
                return tuple(frames)
        return None

    def next(self, cur: Cursor, outcome: str) -> Transition:
        step = self.current(cur)
        expected = self.prog.expected(step.id).outcome
        matched = outcome == expected or (outcome == SKIPPED and expected == PASSED)
        r = self.prog.reactions.get(step.id, {}).get(outcome)
        if r is None:
            if matched:
                return Transition(self._advance(cur), "ok")
            return Transition(None, "failed", f"{step.id} ended {outcome}, expected {expected}")
        verdict = "ok" if matched else "diverted"
        if r.kind == "continue":
            return Transition(self._advance(cur), verdict)
        if r.kind == "stop":
            return Transition(None, "ok" if matched else "failed",
                              f"{step.id} ended {outcome}: plan says stop")
        if r.kind == "goto":
            idx = next(n for n, s in enumerate(self.prog.main) if s.id == r.target)
            return Transition(((MAIN, idx),), verdict, f"{step.id} ended {outcome}: goto {r.target}")
        # steps: run the reaction segment, then carry on after this step
        return Transition(cur + ((r.segment, 0),), verdict, f"{step.id} ended {outcome}: reaction")


# ── static validation ────────────────────────────────────────────────────────

@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    walk: list[str] = field(default_factory=list)   # the expected (all-as-expected) walk

    @property
    def ok(self) -> bool:
        return not self.errors


def simulate(rc: RunnerConfig, reg: Registry, prog: Program, state0: dict,
             data: dict, budget: int = 20000) -> Validation:
    """Explore every reachable walk: the expected outcome of every step, plus every outcome
    the plan reacts to. Check requires before each step and expect.state after it."""
    v = Validation()
    walker = Walker(prog)
    seen: set = set()
    errs: dict[str, None] = {}
    warns: dict[str, None] = {}
    nodes = 0

    def key(cur, state, visits):
        return (cur, tuple(sorted(state.items())), tuple(sorted(visits.items())))

    # the expected walk, for display
    cur, state, visits = walker.start(), dict(state0), {}
    while cur is not None and len(v.walk) < 1000:
        step = walker.current(cur)
        visits[step.id] = visits.get(step.id, 0) + 1
        if visits[step.id] > step.max_visits:
            break
        run = when_holds(step.when, deep_merge(data, {rc.params_section: step.with_}))
        exp = prog.expected(step.id).outcome
        outcome = exp if run else SKIPPED
        v.walk.append(step.id if run else f"({step.id}: skipped, when)")
        if run:
            state.update(produced(reg.actions[step.action], outcome, state))
        cur = walker.next(cur, outcome).cursor

    stack = [(walker.start(), dict(state0), {}, ())]
    while stack:
        cur, state, visits, trail = stack.pop()
        if cur is None:
            continue
        k = key(cur, state, visits)
        if k in seen:
            continue
        seen.add(k)
        nodes += 1
        if nodes > budget:
            warns[f"stopped exploring after {budget} states: the plan branches too much to check fully"] = None
            break
        step = walker.current(cur)
        act = reg.actions[step.action]
        visits = {**visits, step.id: visits.get(step.id, 0) + 1}
        trail = (*trail, step.id)
        if visits[step.id] > step.max_visits:
            warns[f"{step.id} can be reached more than max_visits={step.max_visits} times "
                  f"(via {' > '.join(trail[-6:])}); the run would stop there"] = None
            continue
        if not when_holds(step.when, deep_merge(data, {rc.params_section: step.with_})):
            stack.append((walker.next(cur, SKIPPED).cursor, state, visits, trail))
            continue
        ok, unknown = requires_holds(act.requires, state)
        for var in unknown:
            warns[f"{step.id}: requires {var}, whose value is unknown before it runs "
                  f"(declare `initial`, or let an observer read it)"] = None
        if not ok:
            have = {k2: state.get(k2) for k2 in act.requires}
            before = " > ".join(trail[:-1][-6:]) or "the starting state"
            errs[f"{step.id}: requires {act.requires}, but {before} leaves {have}"] = None
            continue
        exp = prog.expected(step.id)
        outcomes = {exp.outcome} | set(prog.reactions.get(step.id, {}))
        for outcome in sorted(outcomes):
            new = {**state, **produced(act, outcome, state)}
            if outcome == exp.outcome:
                for var, val in exp.state.items():
                    if new.get(var) is not None and new[var] != val:
                        errs[f"{step.id}: expect.state says {var}={val!r}, but ending {outcome} "
                             f"leaves {var}={new[var]!r}"] = None
                    new[var] = val
            t = walker.next(cur, outcome)
            stack.append((t.cursor, new, visits, trail))
    v.errors = list(errs)
    v.warnings = list(warns)
    return v


def initial_state(rc: RunnerConfig, stored: dict | None) -> dict:
    state = {var: spec.get("initial") for var, spec in rc.states.items()}
    for var, val in (stored or {}).items():
        if var in state:
            state[var] = val
    return state

