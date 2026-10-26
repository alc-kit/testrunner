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

Walk edits (the run config's `edit:` list, accumulated through `extends:`), applied in order
to the compiled main path, each anchored on a step id:
  {repeat: ping, after: join, id: ping-after-join}   the step again: same action/with/when
  {insert: [steps...], after|before: <id>}           new steps
  {insert: [steps...], between: [<a>, <b>]}          new steps right BEFORE b; refused unless a
                                                     comes earlier on the walk (also on repeat:)
  {skip: <id> | [<id>...]}                           dropped from the walk
  {replace: <id>, by: [steps...]}                    one step out, these in, at its place
  {extend: <id>, steps: [inner steps...]}            that step's WALK action, for this walk only:
                                                     an inner step with an existing id is merged
                                                     into it; a new one is appended, or placed by
                                                     after:/before:/between: among the inner ids.
                                                     The step keeps its id.
  {retry: <id>, times: N, delay: S, if: {...}, unless: {...}}   retry it, on evidence (below)

Retry (a walk edit, or `retry:` on a step in a path): after an attempt that did NOT end as the
plan expected, and was not aborted, the step runs again (at most `times` more, `delay` seconds
apart) when ALL of `if` hold and NONE of `unless` match. Evidence keys: outcome (names;
`if` defaults to [failed, errored]), detail (regex on the outcome detail), output (regex on what
the attempt printed), state (the OBSERVED state after it), probe (a read-only action that must
pass / that passes).

WALK ACTIONS (walks.py) compile their inner steps with this same compiler in walk mode
(`Compiler(rc, reg, walk=<name>)`): the steps are the walk's inner actions, and a reaction may
also END the walk — `fail` (the action ends with the outcome that triggered it), `skip` (the
action ends skipped), `{end: <outcome>}` (a named outcome the walk declares) — or `ignore` the
outcome (recorded, the walk goes on; the next step's `requires` still guards it). A step list
may end in one of those words: `[collect-logs, fail]` runs the steps, then fails. A step may
carry `always: true`: it runs whenever the walk leaves past it, however it ends.

On the command line, `all` (alone) means the run config's plan, exactly as a bare run; when
the plan is empty it means the runner config's `all.fallback` path. `all` is never a step
inside a plan or path: there it would mean the plan itself.
"""
from __future__ import annotations

import copy
import itertools
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .api import PASSED, SKIPPED
from .config import ConfigError, RunnerConfig, deep_merge, lookup
from .discovery import ActionSpec, Registry

log = logging.getLogger(__name__)

MAIN = "main"
REACTION_WORDS = ("continue", "stop")
WALK_WORDS = ("fail", "ignore", "skip")          # reactions only a walk action's steps may have
EDIT_OPS = {"repeat": {"after", "before", "between", "id"}, "insert": {"after", "before", "between"}, "skip": set(), "replace": {"by"},
            "retry": {"times", "delay", "if", "unless"}, "transition": {"rules"}, "extend": {"steps"}}
RETRY_KEYS = {"times", "delay", "if", "unless"}
EVIDENCE_KEYS = {"outcome", "detail", "output", "state", "probe"}
DEFAULT_RETRY_ON = ["failed", "errored"]


def retry_spec(spec: Any, where: str) -> dict[str, Any]:
    """Check a retry spec's shape (names are checked against the registry later)."""
    if not isinstance(spec, dict) or set(spec) - RETRY_KEYS:
        raise ConfigError(f"{where}: retry takes {', '.join(sorted(RETRY_KEYS))}")
    times = spec.get("times", 1)
    if not isinstance(times, int) or isinstance(times, bool) or times < 1:
        raise ConfigError(f"{where}: retry.times: want a whole number >= 1, got {times!r}")
    delay = spec.get("delay", 0)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError(f"{where}: retry.delay: want seconds >= 0, got {delay!r}")
    out = {"times": times, "delay": float(delay)}
    for part in ("if", "unless"):
        ev = spec.get(part) or {}
        if not isinstance(ev, dict) or set(ev) - EVIDENCE_KEYS:
            raise ConfigError(f"{where}: retry.{part} takes {', '.join(sorted(EVIDENCE_KEYS))}")
        ev = dict(ev)
        if "outcome" in ev:
            ev["outcome"] = ev["outcome"] if isinstance(ev["outcome"], list) else [ev["outcome"]]
        for k in ("detail", "output"):
            if k in ev:
                try:
                    re.compile(ev[k])
                except (re.error, TypeError) as e:
                    raise ConfigError(f"{where}: retry.{part}.{k}: not a regular expression: {e}")
        if "state" in ev and not isinstance(ev["state"], dict):
            raise ConfigError(f"{where}: retry.{part}.state: want {{<state var>: value | [values]}}")
        out[part] = ev
    out["if"].setdefault("outcome", list(DEFAULT_RETRY_ON))
    return out
ALL = "all"


@dataclass
class Step:
    id: str
    action: str
    with_: dict[str, Any] = field(default_factory=dict)
    when: dict[str, Any] | None = None
    max_visits: int = 1
    retry: dict[str, Any] | None = None
    always: bool = False            # walks: runs whenever the walk leaves past it


@dataclass
class Reaction:
    kind: str                       # continue | stop | goto | steps | fail | ignore | skip | end
    target: str | None = None       # goto: the step; end: the outcome
    segment: str | None = None      # steps: the segment key


@dataclass
class Rule:
    """One transition rule: when the step ends with one of `outcomes` (None: any) and the
    observed state matches `state`, `reaction` decides what comes next."""
    outcomes: list[str] | None
    state: dict[str, Any]
    reaction: Reaction
    text: str = ""


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
    # where the main path came from, for people: "plan of x.yml", "all = path full: ...", ...
    source: str = ""
    # transition RULES (on: <step>: [ {when, then} ... ]): first match wins, none -> stop, failed
    rules: dict[str, list[Rule]] = field(default_factory=dict)
    # walks: a reaction segment that ends in a word ([collect-logs, fail]): segment key ->
    # (that reaction, the outcome that started the segment, or None for a rule)
    segment_then: dict[str, tuple[Reaction, str | None]] = field(default_factory=dict)
    # THIS run's copies of walk programs, by the walk step's full id ("addnode", "a/b"): what
    # `edit:` retry/skip of an inner step and --from/--resume inside a walk change (T-D9)
    inner: dict[str, "Program"] = field(default_factory=dict)

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

    def release(self, sid: str) -> None:
        self.used.discard(sid)

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
    def __init__(self, rc: RunnerConfig, reg: Registry, walk: str | None = None):
        self.rc, self.reg = rc, reg
        self.walk = walk          # compiling the inner steps of this walk action (walk mode)
        if ALL in reg.actions:
            raise ConfigError("an action may not be named `all`: on the command line `all` means the "
                              "run config's plan")

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
                allowed = {"action", "id", "with", "when", "max_visits", "retry"} | ({"always"} if self.walk else set())
                unknown = set(e) - allowed
                if unknown:
                    raise ConfigError(f"{where}: step {e['action']!r}: unknown key(s) {', '.join(sorted(unknown))}")
                name = e["action"]
                self._known_action(name, where)
                out.append({"action": name, "id": e.get("id"), "with": deep_merge(w, e.get("with") or {}),
                            "when": e.get("when"), "max_visits": int(e.get("max_visits", 1)),
                            "retry": retry_spec(e["retry"], f"{where}: step {name!r}") if "retry" in e else None,
                            "always": bool(e.get("always", False))})
            else:
                raise ConfigError(f"{where}: cannot read step {e!r}")
        return out

    def _known_action(self, name: str, where: str) -> None:
        act = self.reg.actions.get(name)
        if act is None:
            raise ConfigError(f"{where}: unknown action {name!r}")
        owner = getattr(act, "inner_of", None)
        if owner is not None and owner != self.walk:
            raise ConfigError(f"{where}: {name!r} is an inner step of the walk action {owner!r}, not an action "
                              f"a plan can name")

    def _all_inside(self, where: str) -> ConfigError:
        fb = self.rc.all_fallback
        return ConfigError(f"{where}: `all` means the run config's plan itself, so it cannot be a step "
                           f"of a plan or path" + (f"; name the path {fb!r}" if fb else ""))

    def _expand_name(self, name: str, w: dict, depth: int, where: str) -> list[dict]:
        if name == ALL:
            raise self._all_inside(where)
        if ".." in name:
            return self._expand_range(name, w, depth, where)
        if name in self.rc.paths and not self.walk:
            out = self.expand(self.rc.paths[name], w, depth + 1, f"paths.{name}")
            log.debug("%s: path %r -> %s", where, name, [s["id"] or s["action"] for s in out])
            return out
        if name in self.reg.actions:
            self._known_action(name, where)
            return [{"action": name, "id": None, "with": w, "when": None, "max_visits": 1, "retry": None,
                     "always": False}]
        if self.walk:
            raise ConfigError(f"{where}: {name!r} is not an action (a walk's steps are actions, never paths)")
        raise ConfigError(f"{where}: {name!r} is neither an action nor a path")

    def _expand_range(self, spec: str, w: dict, depth: int, where: str) -> list[dict]:
        out = self._expand_range_(spec, w, depth, where)
        log.debug("%s: range %r -> %s", where, spec, [s["id"] or s["action"] for s in out])
        return out

    def _expand_range_(self, spec: str, w: dict, depth: int, where: str) -> list[dict]:
        path_name, _, rng = spec.rpartition(":")
        path_name = path_name or self.rc.default_path
        if path_name == ALL:
            raise self._all_inside(where)
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
                cli_with: dict | None = None, plan_of: str = "the run config",
                edits: list | None = None, cli_edits: list | None = None,
                start_at: str | None = None) -> Program:
        """`plan_of` names where the plan comes from (RunConfig.plan_of), for Program.source;
        `edits` is the run config's `edit:` list (strict unless the steps are ad hoc);
        `cli_edits` are this run's command-line edits (always notes, never errors);
        `start_at` cuts the walk to begin at that step (--from / --resume)."""
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
        cli_steps = list(cli_steps or [])
        if ALL in cli_steps and len(cli_steps) > 1:
            raise ConfigError("`all` stands alone: it means the whole plan; give it without other steps")
        if cli_steps == [ALL]:
            # `all` is a bare run of the plan (strict, as written); an empty plan falls back
            if self.expand(plan.get("path") or [], where="plan.path"):
                adhoc, path, source = False, plan.get("path"), f"all = plan of {plan_of}"
            elif self.rc.all_fallback:
                fb = self.rc.all_fallback
                adhoc, path, source = True, [fb], f"all = path {fb}: no plan in {plan_of}"
            else:
                raise ConfigError(f"`all`: {plan_of} has no plan, and the runner config names no fallback "
                                  f"path (all: {{fallback: <path>}})")
        elif cli_steps:
            adhoc, path = True, cli_steps
            source = (f"path {path[0]} (command line)" if len(path) == 1 and path[0] in self.rc.paths
                      else f"command line: {' '.join(path)}")
        else:
            adhoc, path, source = False, plan.get("path"), f"plan of {plan_of}"
        log.info("compiling the steps: %s: %s", source, path)
        if path is None:
            path = []
        params = deep_merge(plan.get("with") or {}, cli_with or {})
        ids = _Ids()

        def make(entries: list[dict]) -> list[Step]:
            out = []
            for e in entries:
                sid = ids.take(e["id"] or e["action"], explicit=bool(e["id"]))
                out.append(Step(sid, e["action"], e["with"], e["when"], e["max_visits"], e["retry"],
                                e.get("always", False)))
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
            log.info("%s: %s spliced %s %s", where, [s.id for s in new], "after" if "after" in d else "before",
                     anchor)
        transitions: dict[str, tuple[str, Any]] = {}       # step id -> (where, rules), from `edit:`
        inner: dict[str, Program] = {}
        done = self._apply_edits(edits, main, make, ids, adhoc, notes, transitions=transitions, inner=inner)
        done += self._apply_edits(cli_edits, main, make, ids, True, notes, label="--edit", transitions=transitions,
                                  inner=inner)
        if done:
            source += f"; edits: {', '.join(done)}"
        if start_at is not None:
            outer = start_at.split("/")[0]
            idx = next((n for n, s in enumerate(main) if s.id == outer), None)
            if idx is None:
                raise ConfigError(f"--from {start_at}: not a step on the walk "
                                  f"(on it: {', '.join(s.id for s in main) or 'nothing'})")
            if idx:
                log.info("--from %s: the walk starts there; %s left out", start_at, [s.id for s in main[:idx]])
            del main[:idx]
            if "/" in start_at:
                self._start_inside(main, start_at, inner, notes)
            source += f"; from {start_at}"
        segments = {MAIN: main}
        reactions: dict[str, dict[str, Reaction]] = {}
        rules: dict[str, list[Rule]] = {}
        segment_then: dict[str, tuple[Reaction, str | None]] = {}

        def word_of(r: Any, where: str) -> Reaction | None:
            """A reaction written as a word or {goto}/{end}; None when `r` is a step list."""
            if r in REACTION_WORDS:
                if self.walk and r == "stop":
                    raise ConfigError(f"{where}: inside a walk action a step ends the walk with fail, "
                                      f"skip or {{end: <outcome>}}, not stop")
                return Reaction(r)
            if r in WALK_WORDS or (isinstance(r, dict) and set(r) == {"end"}):
                if not self.walk:
                    raise ConfigError(f"{where}: {r!r} is a reaction of a walk action's inner steps; "
                                      f"a plan reacts with continue, stop, {{goto: <step>}} or a step list")
                return Reaction("end", target=str(r["end"])) if isinstance(r, dict) else Reaction(r)
            if isinstance(r, dict) and set(r) == {"goto"}:
                return Reaction("goto", target=r["goto"])
            return None

        def reaction_of(r: Any, where: str, key: str, outcome: str | None = None) -> Reaction:
            word = word_of(r, where)
            if word is not None:
                return word
            if isinstance(r, list) and r and self.walk:
                last = word_of(r[-1], f"{where}[-1]") if (isinstance(r[-1], (str, dict))) else None
                if last is not None and last.kind not in ("continue",):
                    if len(r) == 1:
                        return last
                    segments[key] = make(self.expand(r[:-1], where=where))
                    segment_then[key] = (last, outcome)
                    return Reaction("steps", segment=key)
            if isinstance(r, (list, str)):
                segments[key] = make(self.expand(r, where=where))
                return Reaction("steps", segment=key)
            raise ConfigError(f"{where}: want " + ("continue, fail, ignore, skip, {end: <outcome>}, {goto: <step>}, "
                                                   "or a step list (that may end in one of those words)"
                                                   if self.walk else "continue, stop, {goto: <step>} or a step list"))

        def rules_of(sid: str, given: Any, where: str) -> list[Rule]:
            if not isinstance(given, list) or not given:
                raise ConfigError(f"{where}: want a list of {{when: {{outcome, state}}, then: ...}} rules")
            out = []
            for i, rule in enumerate(given):
                w = f"{where}[{i}]"
                if not isinstance(rule, dict) or set(rule) != {"when", "then"} and set(rule) != {"then"}:
                    raise ConfigError(f"{w}: a rule is {{when: {{outcome: ..., state: {{...}}}}, then: ...}}")
                cond = rule.get("when") or {}
                if not isinstance(cond, dict) or set(cond) - {"outcome", "state"}:
                    raise ConfigError(f"{w}.when: takes outcome and state")
                outs = cond.get("outcome")
                outs = None if outs is None else (outs if isinstance(outs, list) else [outs])
                state = cond.get("state") or {}
                if not isinstance(state, dict):
                    raise ConfigError(f"{w}.when.state: want {{<state var>: value | [values]}}")
                reaction = reaction_of(rule["then"], f"{w}.then", f"{sid}!rule{i}")
                text = (" ".join(filter(None, [f"outcome {'|'.join(outs)}" if outs else "",
                                               ", ".join(f"{k}={'|'.join(map(str, v)) if isinstance(v, list) else v}"
                                                         for k, v in state.items())])) or "otherwise")
                out.append(Rule(outs, state, reaction, text))
            return out

        for sid, by_outcome in (plan.get("on") or {}).items():
            where = f"plan.on.{sid}"
            if isinstance(by_outcome, list):                 # transition rules (outcome + state)
                rules[sid] = rules_of(sid, by_outcome, where)
                continue
            if not isinstance(by_outcome, dict):
                raise ConfigError(f"{where}: want {{<outcome>: <reaction>}} or a list of transition rules")
            for outcome, r in by_outcome.items():
                reactions.setdefault(sid, {})[outcome] = reaction_of(r, f"{where}.{outcome}", f"{sid}!{outcome}",
                                                                     outcome)
        for sid, (where, given) in transitions.items():      # edits re-route a step: they replace
            reactions.pop(sid, None)
            rules[sid] = rules_of(sid, given, where)
        expect: dict[str, Expect] = {}
        for sid, e in (plan.get("expect") or {}).items():
            if not isinstance(e, dict) or set(e) - {"outcome", "state"}:
                raise ConfigError(f"plan.expect.{sid}: want {{outcome: ..., state: {{...}}}}")
            expect[sid] = Expect(e.get("outcome", PASSED), dict(e.get("state") or {}))
        prog = Program(segments, reactions, expect, params, notes, source, rules, segment_then, inner)
        self._check_references(prog, adhoc or start_at is not None)
        log.info("compiled: main %s", [f"{s.id}" + (f"(={s.action})" if s.action != s.id else "") for s in prog.main])
        for s in prog.steps():
            if s.with_ or s.when or s.max_visits != 1:
                log.debug("  step %s: with %s, when %s, max_visits %d", s.id, s.with_, s.when, s.max_visits)
        for sid, by in prog.reactions.items():
            log.info("  on %s: %s", sid, {o: (r.kind, r.target or r.segment) for o, r in by.items()})
        for sid, e in prog.expect.items():
            log.info("  expect %s: outcome %s, state %s", sid, e.outcome, e.state)
        if params:
            log.info("  plan parameters (plan with + --with): %s", params)
        for n in notes:
            log.info("  note: %s", n)
        return prog

    def _inner_step(self, main: list[Step], sid: str, inner: dict, where: str) -> tuple["Program", str, str] | None:
        """`walk/step[/step...]`: this run's copy of the program holding the last part (made, and
        kept in `inner` by the walk step's full id), that part's local id, and the copy's key.
        None when the first part is not on the walk."""
        parts = sid.split("/")
        step = next((s for s in main if s.id == parts[0]), None)
        if step is None:
            return None
        key = parts[0]
        for i in range(1, len(parts)):
            w = walk_of(self.reg.actions[step.action], self.reg)
            if w is None:
                raise ConfigError(f"{where}: {key} is not a walk action, so {sid!r} names no inner step")
            if key not in inner:
                inner[key] = copy.deepcopy(w.program)
                log.debug("%s: this run's own copy of walk %s's program (for %s)", where, w.name, key)
            prog = inner[key]
            nxt = next((s for s in prog.main if s.id == parts[i]), None)
            if nxt is None:
                raise ConfigError(f"{where}: walk {w.name} ({key}) has no step {parts[i]!r} "
                                  f"(it has {', '.join(s.id for s in prog.main)})")
            if i == len(parts) - 1:
                return prog, parts[i], key
            key, step = f"{key}/{parts[i]}", nxt
        return None

    def _recheck_inner(self, prog: "Program", key: str, notes: list[str]) -> None:
        owner = next((a.inner_of for s in prog.main for a in [self.reg.actions[s.action]] if a.inner_of), None)
        before = len(prog.notes)
        Compiler(self.rc, self.reg, walk=owner)._check_references(prog, True)
        notes += [f"{key}: {n}" for n in prog.notes[before:]]

    def _start_inside(self, main: list[Step], start_at: str, inner: dict, notes: list[str]) -> None:
        """--from/--resume at an inner step: every level of the walk starts there (T-D9)."""
        parts = start_at.split("/")
        for i in range(2, len(parts) + 1):
            found = self._inner_step(main, "/".join(parts[:i]), inner, f"--from {start_at}")
            if found is None:
                return
            prog, local, key = found
            idx = next(n for n, s in enumerate(prog.main) if s.id == local)
            if idx:
                log.info("--from %s: walk %s starts at %s; %s left out", start_at, key, local,
                         [s.id for s in prog.main[:idx]])
            del prog.main[:idx]
            self._recheck_inner(prog, key, notes)

    def _apply_edits(self, edits: Any, main: list[Step], make, ids: _Ids, adhoc: bool,
                     notes: list[str], label: str = "edit", transitions: dict | None = None,
                     inner: dict | None = None) -> list[str]:
        """Apply the walk edits to `main` in place; return what was done, for the banner. An
        inner step (`addnode/install`) may be retried or skipped — nothing else (T-D9)."""
        if edits is None:
            return []
        if not isinstance(edits, list):
            raise ConfigError("edit: want a list of edits (repeat/insert/skip/replace)")
        done: list[str] = []
        for i, ed in enumerate(edits):
            where = f"{label}[{i}]"
            if not isinstance(ed, dict):
                raise ConfigError(f"{where}: want a mapping with one of {', '.join(EDIT_OPS)}")
            ops = [k for k in ed if k in EDIT_OPS]
            if len(ops) != 1:
                raise ConfigError(f"{where}: want exactly one of {', '.join(EDIT_OPS)}, got {', '.join(map(str, ed))}")
            op = ops[0]
            extra = set(ed) - {op} - EDIT_OPS[op]
            if extra:
                raise ConfigError(f"{where}: {op} takes {', '.join(sorted(EDIT_OPS[op])) or 'no other key'}, "
                                  f"not {', '.join(sorted(map(str, extra)))}")
            if op in ("repeat", "insert") and len({"after", "before", "between"} & set(ed)) != 1:
                raise ConfigError(f"{where}: {op} needs exactly one of after:, before: or between: "
                                  f"(<step id>, <step id>, [<step id>, <step id>])")
            if "between" in ed and not (isinstance(ed["between"], list) and len(ed["between"]) == 2
                                        and all(isinstance(x, str) and x for x in ed["between"])):
                raise ConfigError(f"{where}: between: want [<step id>, <step id>], got {ed['between']!r}")
            if op == "replace" and "by" not in ed:
                raise ConfigError(f"{where}: replace needs by: [steps...]")

            targets = [ed[op]] if op != "skip" or not isinstance(ed[op], list) else ed[op]
            if any(isinstance(t, str) and "/" in t for t in targets) or any(
                    isinstance(ed.get(k), str) and "/" in ed[k] for k in ("after", "before")) or any(
                    "/" in x for x in (ed.get("between") or [])):
                if op not in ("retry", "skip"):
                    raise ConfigError(f"{where}: {op} cannot reach inside a walk action; an inner step "
                                      f"(walk/step) can only be retried or skipped")
                for t in targets:
                    if "/" not in str(t):
                        raise ConfigError(f"{where}: skip mixes inner and outer steps; give them separately")
                    found = self._inner_step(main, str(t), inner if inner is not None else {}, where)
                    if found is None:
                        if not adhoc:
                            raise ConfigError(f"{where}: {op}: {t!r}: {str(t).split('/')[0]!r} is not a step on "
                                              f"the walk (on it: {', '.join(s.id for s in main) or 'nothing'})")
                        notes.append(f"{where} ({op} {t!r}) dropped: not in this run")
                        continue
                    prog, local, key = found
                    if op == "retry":
                        st = next(s for s in prog.main if s.id == local)
                        st.retry = retry_spec({k: v for k, v in ed.items() if k != "retry"}, where)
                        Compiler(self.rc, self.reg)._check_retry(st)
                        done.append(f"retry {t} x{st.retry['times']}")
                    else:
                        prog.main[:] = [s for s in prog.main if s.id != local]
                        self._recheck_inner(prog, key, notes)
                        done.append(f"-{t}")
                    log.info("%s: %s %s inside walk step %s", where, op, local, key)
                continue

            def at(sid: Any, what: str) -> int | None:
                idx = next((n for n, s in enumerate(main) if s.id == sid), None)
                if idx is None:
                    if not adhoc:
                        raise ConfigError(f"{where}: {op}: {what} {sid!r} is not a step on the walk "
                                          f"(on it: {', '.join(s.id for s in main) or 'nothing'})")
                    notes.append(f"{where} ({op} {what} {sid!r}) dropped: not in this run")
                return idx

            if op in ("repeat", "insert"):
                # between: [a, b] — placed right before b, and only where a really precedes b:
                # the edit states its intent, and refuses when the walk no longer matches it
                lo = ed["between"][0] if "between" in ed else None
                anchor = ed["between"][1] if "between" in ed else (ed.get("after") or ed.get("before"))
                if lo is not None:
                    ia, ib = at(lo, "anchor"), at(anchor, "anchor")
                    if ia is None or ib is None:
                        continue
                    if ia >= ib:
                        msg = (f"{where}: {op} between [{lo}, {anchor}]: {lo!r} does not come before {anchor!r} "
                               f"on the walk ({', '.join(s.id for s in main)})")
                        if not adhoc:
                            raise ConfigError(msg)
                        notes.append(msg + " — dropped")
                        continue
                if op == "repeat":
                    src = at(ed["repeat"], "step")
                    if src is None or at(anchor, "anchor") is None:
                        continue
                    s = main[src]
                    new = [Step(ids.take(ed.get("id") or s.id.split("#")[0], explicit=bool(ed.get("id"))), s.action,
                                copy.deepcopy(s.with_), copy.deepcopy(s.when), s.max_visits, copy.deepcopy(s.retry))]
                else:
                    if at(anchor, "anchor") is None:
                        continue
                    new = make(self.expand(ed["insert"], where=where))
                idx = at(anchor, "anchor")       # where it is NOW (after any earlier edit)
                pos = idx + 1 if "after" in ed else idx
                main[pos:pos] = new
                rel = "after" if "after" in ed else ("between " + lo + " and" if lo is not None else "before")
                done += [f"+{n.id}" + (f" (again {ed['repeat']})" if op == "repeat" else "") + f" {rel} {anchor}"
                         for n in new]
            elif op == "extend":
                if "steps" not in ed:
                    raise ConfigError(f"{where}: extend needs steps: [inner steps...]")
                idx = at(ed["extend"], "step")
                if idx is None:
                    continue
                st = main[idx]
                from . import walks
                variant = f"{st.action}+{st.id}"
                walks.derive_extended(self.reg, self.rc, st.action, variant, ed["steps"], where)
                st.action = variant
                done.append(f"extended {st.id} ({len(ed['steps'])} inner step(s))")
            elif op == "transition":
                if "rules" not in ed:
                    raise ConfigError(f"{where}: transition needs rules: [{{when: ..., then: ...}}, ...]")
                if at(ed["transition"], "step") is not None:
                    if transitions is not None:
                        transitions[ed["transition"]] = (f"{where}.rules", ed["rules"])
                    done.append(f"transitions of {ed['transition']}")
            elif op == "retry":
                idx = at(ed["retry"], "step")
                if idx is not None:
                    main[idx].retry = retry_spec({k: v for k, v in ed.items() if k != "retry"}, where)
                    done.append(f"retry {ed['retry']} x{main[idx].retry['times']}")
            elif op == "skip":
                for sid in ed["skip"] if isinstance(ed["skip"], list) else [ed["skip"]]:
                    idx = at(sid, "step")
                    if idx is not None:
                        del main[idx]
                        done.append(f"-{sid}")
            else:
                idx = at(ed["replace"], "step")
                if idx is None:
                    continue
                old = main.pop(idx)
                ids.release(old.id)          # the replacement may take the old id
                new = make(self.expand(ed["by"], where=where))
                main[idx:idx] = new
                done.append(f"{old.id} -> {', '.join(n.id for n in new)}")
            log.info("%s: %s -> walk %s", where, op, [s.id for s in main])
        return done

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
            for outcome, r in list(by_outcome.items()):
                if outcome not in act.all_outcomes:
                    raise ConfigError(f"plan.on.{sid}: action {act.name!r} never ends {outcome!r} "
                                      f"(it declares {', '.join(sorted(act.all_outcomes))})")
                if r.kind == "goto" and r.target not in main_ids:
                    if adhoc:
                        del by_outcome[outcome]
                        prog.notes.append(f"plan.on.{sid}.{outcome} (goto {r.target!r}) dropped: not in this run")
                        continue
                    raise ConfigError(f"plan.on.{sid}.{outcome}: goto {r.target!r} is not a step on the main path")
        for sid in list(prog.rules):
            if sid not in all_ids:
                if adhoc:
                    del prog.rules[sid]
                    prog.notes.append(f"transition rules of {sid} dropped: not in this run")
                    continue
                raise ConfigError(f"transition rules: no step {sid!r} in the plan")
            act = self.reg.actions[prog.step(sid).action]
            for i, r in enumerate(list(prog.rules[sid])):
                where = f"transition rule {i} of {sid}"
                bad = [o for o in r.outcomes or [] if o not in act.all_outcomes]
                if bad:
                    raise ConfigError(f"{where}: action {act.name!r} never ends {bad} "
                                      f"(it declares {', '.join(sorted(act.all_outcomes))})")
                for var, val in r.state.items():
                    vals = val if isinstance(val, list) else [val]
                    if var not in self.rc.states or any(v not in self.rc.states[var]["values"] for v in vals):
                        raise ConfigError(f"{where}: state {var}={val!r} is not a declared state value")
                if r.reaction.kind == "goto" and r.reaction.target not in main_ids:
                    if adhoc:
                        prog.rules[sid].remove(r)
                        prog.notes.append(f"{where} (goto {r.reaction.target!r}) dropped: not in this run")
                        continue
                    raise ConfigError(f"{where}: goto {r.reaction.target!r} is not a step on the main path")
        for s in prog.steps():
            if s.retry:
                self._check_retry(s)
        for sid, e in prog.expect.items():
            act = self.reg.actions[prog.step(sid).action]
            if e.outcome not in act.all_outcomes:
                raise ConfigError(f"plan.expect.{sid}: action {act.name!r} never ends {e.outcome!r}")
            for var, val in e.state.items():
                if var not in self.rc.states or val not in self.rc.states[var]["values"]:
                    raise ConfigError(f"plan.expect.{sid}: {var}={val!r} is not a declared state value")


    def _check_retry(self, s: Step) -> None:
        act = self.reg.actions[s.action]
        for part in ("if", "unless"):
            ev = s.retry[part]
            bad = [o for o in ev.get("outcome", []) if o not in act.all_outcomes]
            if bad:
                raise ConfigError(f"retry of {s.id}: {part}.outcome {bad}: action {act.name!r} never ends so "
                                  f"(it declares {', '.join(sorted(act.all_outcomes))})")
            if "aborted" in ev.get("outcome", []) and part == "if":
                raise ConfigError(f"retry of {s.id}: an aborted step is never retried (the operator stopped it)")
            for var, val in (ev.get("state") or {}).items():
                vals = val if isinstance(val, list) else [val]
                if var not in self.rc.states or any(v not in self.rc.states[var]["values"] for v in vals):
                    raise ConfigError(f"retry of {s.id}: {part}.state {var}={val!r} is not a declared state value")
            probe = ev.get("probe")
            if probe is not None:
                if probe not in self.reg.actions:
                    raise ConfigError(f"retry of {s.id}: {part}.probe {probe!r} is not an action")
                if not self.reg.actions[probe].readonly:
                    raise ConfigError(f"retry of {s.id}: {part}.probe {probe!r} must be a read-only action "
                                      f"(evidence is gathered, not made)")


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


def when_explained(when: dict | None, data: dict) -> list[str]:
    """`when` key by key — what the config holds against what the step wants (for the trace)."""
    out = []
    for key, want in (when or {}).items():
        have = lookup(data, key)
        ok = when_holds({key: want}, data)
        out.append(f"{key}: have {have!r}, want {want!r} -> {'yes' if ok else 'NO'}")
    return out


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


def _effect(spec: dict, state: dict) -> dict:
    out = {}
    for var, val in spec.items():
        if isinstance(val, dict):
            if state.get(var) in val:
                out[var] = val[state.get(var)]
        else:
            out[var] = val
    return out


def walk_of(act: ActionSpec, reg: Registry | None) -> Any:
    """The WalkSpec an action runs: its own (a walk), or its target's (an inner step that
    calls a walk); None otherwise."""
    if act.kind == "walk":
        return act.walk
    if act.kind == "call" and reg is not None:
        target = reg.actions.get(act.call or "")
        return target.walk if target is not None and target.kind == "walk" else None
    return None


def produced(act: ActionSpec, outcome: str, state: dict, reg: Registry | None = None,
             compose: bool = False) -> dict:
    """The state values an outcome sets. A value is either the new value, or a TRANSITION
    MAP {current: next}: a current value the map does not list stays as it is.

    compose=True (the static check): a walk that PASSES has the effect of its inner steps'
    expected walk, in order, then its own `produces`; an inner step that calls an action
    without declaring effects of its own has that action's effect. At run time the inner
    steps apply their own effects as they end, so the walk's step applies only its own."""
    if compose and reg is not None:
        if act.kind == "call" and not act.own_effects and act.call in reg.actions:
            return produced(reg.actions[act.call], outcome, state, reg, True)
        if act.kind == "walk" and outcome == PASSED:
            s = dict(state)
            prog = act.walk.program
            for st in prog.main:
                s.update(produced(reg.actions[st.action], prog.expected(st.id).outcome, s, reg, True))
            s.update(_effect(act.produces, s))
            return {k: v for k, v in s.items() if state.get(k) != v or k in act.produces}
    return _effect(act.produces if outcome == PASSED else act.produces_on.get(outcome, {}), state)


def reaction_text(prog: Program, r: Reaction) -> str:
    if r.kind == "goto":
        return f"goto {r.target}"
    if r.kind == "end":
        return f"end the walk {r.target}"
    if r.kind == "fail":
        return "fail the walk"
    if r.kind == "skip":
        return "end the walk skipped"
    if r.kind == "ignore":
        return "ignore it, go on"
    if r.kind == "steps":
        then = prog.segment_then.get(r.segment or "")
        return (f"run {', '.join(s.id for s in prog.segments.get(r.segment or '', []))}, then "
                + (reaction_text(prog, then[0]) if then else "go on"))
    return r.kind


def matrix(prog: Program, reg: Registry, prefix: str = "", depth: int = 0,
           inner_progs: dict | None = None) -> list[dict[str, str]]:
    """The state/event matrix of the main walk: per step and outcome (the event), the state it
    needs, the state it leaves, and what comes next. `*` marks the expected outcome. A walk
    action's inner steps follow its rows, indented, as `<step>/<inner step>`."""
    rows = []
    for s in prog.main:
        act = reg.actions[s.action]
        exp = prog.expected(s.id).outcome
        named = sorted(o for o in act.all_outcomes if o not in (PASSED, "failed", "errored", "aborted", SKIPPED))
        rules = prog.rules.get(s.id)
        for n, o in enumerate([PASSED, *named, "failed", "errored", "aborted"]):
            spec = act.produces if o == PASSED else act.produces_on.get(o, {})
            after = ", ".join(f"{k}: " + (" ".join(f"{a}->{b}" for a, b in v.items()) if isinstance(v, dict) else str(v))
                              for k, v in spec.items()) or "(unchanged)"
            if o == "aborted":
                nxt = "stop (aborted)"
            elif rules is not None:
                hits = [r for r in rules if r.outcomes is None or o in r.outcomes]
                nxt = "; ".join(f"{r.text} -> {reaction_text(prog, r.reaction)}" for r in hits)
                if not any(not r.state for r in hits):
                    nxt += ("; " if nxt else "") + "otherwise -> stop (failed)"
            else:
                r = prog.reactions.get(s.id, {}).get(o)
                nxt = reaction_text(prog, r) if r else ("continue" if o == exp else "stop (failed)")
            req = ", ".join(f"{k}: {'|'.join(map(str, v)) if isinstance(v, list) else v}"
                            for k, v in act.requires.items()) or "-"
            rows.append({"step": "  " * depth + prefix + s.id if n == 0 else "", "requires": req if n == 0 else "",
                         "outcome": o + ("*" if o == exp else ""), "after": after, "next": nxt})
        inner = walk_of(act, reg)
        if inner is not None and depth < 6:
            ip = prog.inner if inner_progs is None else inner_progs
            rows += matrix(ip.get(f"{prefix}{s.id}") or inner.program, reg, f"{prefix}{s.id}/", depth + 1, ip)
    return rows


def matrix_text(rows: list[dict[str, str]]) -> list[str]:
    cols = [("step", "step"), ("requires", "requires (state in)"), ("outcome", "outcome (event)"),
            ("after", "state after"), ("next", "next")]
    w = {k: max([len(h)] + [len(r[k]) for r in rows]) for k, h in cols}
    line = lambda r: "  " + " | ".join(r[k].ljust(w[k]) for k, _ in cols).rstrip()  # noqa: E731
    return [line({k: h for k, h in cols}), "  " + "-+-".join("-" * w[k] for k, _ in cols)] + [line(r) for r in rows]


def state_matches(want: dict, state: dict) -> bool:
    return all(state.get(k) in (v if isinstance(v, list) else [v]) for k, v in want.items())


@dataclass
class Transition:
    cursor: Cursor | None           # None: the run (or walk) ends
    # ok | diverted | failed | ignored | ended — what this outcome means for the run. `ended`
    # (walks only): the walk stops here and its action ends with `ends`; `failed` in a walk:
    # the action ends with `ends` (the outcome that failed it) when that is set
    verdict: str
    why: str = ""
    ends: str | None = None


class Walker:
    def __init__(self, prog: Program):
        self.prog = prog

    def start(self) -> Cursor | None:
        return ((MAIN, 0),) if self.prog.main else None

    def current(self, cur: Cursor) -> Step:
        seg, i = cur[-1]
        return self.prog.segments[seg][i]

    def _go_on(self, cur: Cursor, verdict: str, why: str = "") -> Transition:
        """The step after `cur`; leaving a reaction segment that ends in a word (walks:
        [collect-logs, fail]) applies that word, as the step that started the segment."""
        frames = list(cur)
        while frames:
            seg, i = frames.pop()
            if i + 1 < len(self.prog.segments[seg]):
                frames.append((seg, i + 1))
                return Transition(tuple(frames), verdict, why)
            then = self.prog.segment_then.get(seg)
            if then is not None and frames:
                parent = tuple(frames)
                reaction, outcome = then
                return self._react(parent, self.current(parent), reaction, outcome or "failed",
                                   matched=False, because=f" (after {', '.join(s.id for s in self.prog.segments[seg])})")
        return Transition(None, verdict, why)

    def next(self, cur: Cursor, outcome: str, state: dict | None = None) -> Transition:
        step = self.current(cur)
        expected = self.prog.expected(step.id).outcome
        matched = outcome == expected or (outcome == SKIPPED and expected == PASSED)
        rules = self.prog.rules.get(step.id)
        if rules is not None and outcome != SKIPPED:
            for r in rules:
                if (r.outcomes is None or outcome in r.outcomes) and state_matches(r.state, state or {}):
                    return self._react(cur, step, r.reaction, outcome, matched, f" (rule: {r.text})")
            return Transition(None, "failed", f"{step.id} ended {outcome} with state {state or {}}: "
                                              f"no transition rule matches", ends=outcome)
        r = self.prog.reactions.get(step.id, {}).get(outcome)
        if r is None:
            if matched:
                return self._go_on(cur, "ok")
            return Transition(None, "failed", f"{step.id} ended {outcome}, expected {expected}", ends=outcome)
        return self._react(cur, step, r, outcome, matched)

    def _react(self, cur: Cursor, step: Step, r: Reaction, outcome: str, matched: bool,
               because: str = "") -> Transition:
        verdict = "ok" if matched else "diverted"
        if r.kind == "continue":
            return self._go_on(cur, verdict)
        if r.kind == "fail":
            return Transition(None, "failed", f"{step.id} ended {outcome}: the walk says fail{because}", ends=outcome)
        if r.kind == "skip":
            return Transition(None, "ended", f"{step.id} ended {outcome}: the walk ends skipped{because}",
                              ends=SKIPPED)
        if r.kind == "end":
            return Transition(None, "ended", f"{step.id} ended {outcome}: the walk ends {r.target}{because}",
                              ends=r.target)
        if r.kind == "ignore":
            return self._go_on(cur, "ok" if matched else "ignored",
                               "" if matched else f"{step.id} ended {outcome}: ignored, the walk goes on{because}")
        if r.kind == "stop":
            return Transition(None, "ok" if matched else "failed",
                              f"{step.id} ended {outcome}: plan says stop{because}")
        if r.kind == "goto":
            idx = next(n for n, s in enumerate(self.prog.main) if s.id == r.target)
            return Transition(((MAIN, idx),), verdict, f"{step.id} ended {outcome}: goto {r.target}{because}")
        # steps: run the reaction segment, then carry on after this step
        return Transition(cur + ((r.segment, 0),), verdict, f"{step.id} ended {outcome}: reaction{because}")


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
             data: dict, budget: int = 20000, _memo: dict | None = None,
             _prefix: str = "", _inner: dict | None = None) -> Validation:
    """Explore every reachable walk: the expected outcome of every step, plus every outcome
    the plan reacts to. Check requires before each step and expect.state after it. A walk
    action is explored INSIDE too, from every state the outer walk reaches it in: its inner
    steps' guards, reactions (an `ignore` whose skipped effect a later step needs is an
    error here) and expectations, reported as `<step>/<inner step>: ...`."""
    memo = {} if _memo is None else _memo
    inner_progs = prog.inner if _inner is None else _inner
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
            state.update(produced(reg.actions[step.action], outcome, state, reg, compose=True))
        cur = walker.next(cur, outcome, state).cursor

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
            stack.append((walker.next(cur, SKIPPED, state).cursor, state, visits, trail))
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
        inner = walk_of(act, reg)
        if inner is not None:
            iprog = inner_progs.get(f"{_prefix}{step.id}") or inner.program
            mkey = (id(iprog), tuple(sorted(state.items())))
            if mkey not in memo:
                memo[mkey] = None                       # a walk that (indirectly) contains itself
                memo[mkey] = simulate(rc, reg, iprog, state, data, budget, memo, f"{_prefix}{step.id}/",
                                      inner_progs)
            sub = memo[mkey]
            if sub is not None:
                for e in sub.errors:
                    errs[f"{step.id}/{e}"] = None
                for w in sub.warnings:
                    warns[f"{step.id}/{w}"] = None
        exp = prog.expected(step.id)
        rules = prog.rules.get(step.id) or []
        outcomes = {exp.outcome} | set(prog.reactions.get(step.id, {})) | {o for r in rules for o in r.outcomes or []}
        for outcome in sorted(outcomes):
            new = {**state, **produced(act, outcome, state, reg, compose=True)}
            if outcome == exp.outcome:
                for var, val in exp.state.items():
                    if new.get(var) is not None and new[var] != val:
                        errs[f"{step.id}: expect.state says {var}={val!r}, but ending {outcome} "
                             f"leaves {var}={new[var]!r}"] = None
                    new[var] = val
            # the model's own end state, and every state a rule for this outcome names: an
            # observer may report it, so its branch is judged too (goto form after a join that
            # left the rig only installed)
            ends = [new] + [{**new, **dict(combo)} for r in rules
                            if r.state and (r.outcomes is None or outcome in r.outcomes)
                            for combo in itertools.product(*[[(k, x) for x in (v if isinstance(v, list) else [v])]
                                                             for k, v in r.state.items()])]
            for end in ends:
                t = walker.next(cur, outcome, end)
                stack.append((t.cursor, end, visits, trail))
    v.errors = list(errs)
    v.warnings = list(warns)
    return v


def initial_state(rc: RunnerConfig, stored: dict | None) -> dict:
    state = {var: spec.get("initial") for var, spec in rc.states.items()}
    for var, val in (stored or {}).items():
        if var in state:
            state[var] = val
    return state

