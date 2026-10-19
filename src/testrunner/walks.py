"""Walk actions: an action written as a walk of INNER STEPS, in YAML.

    actions:
      reprovision:
        extends: install                  # its steps, merged by id
        params: {victim: "{role:reprovision_victim}"}
        steps:
          - id: install
            limit: ["{param.victim}"]
          - {id: unlock, action: reboot, with: {limit: ["{param.victim}"]}}

The inner steps are compiled by the planner's own compiler (walk mode), so every inner
step has what a plan step has — a guard (`requires`), an effect (`produces`,
`produces_on`), an expected outcome (`expect`), a transition (`on:`, outcome-only or
rules), `retry`, `when`, `max_visits` — plus `always` and the walk-only reactions `fail`,
`ignore`, `skip`, `{end: <outcome>}`. Nothing about the state machine is new; it applies
one level down.

An inner step is ONE of:
  {action: <name>, with: {...}}   any action (python, shell or another walk); its contract
                                  is that action's, unless the step declares its own
  {run: <argv | string>, env, cwd, rules, timeout, log}     a command (core)
  {refuse: <message>, when: {...}}  fail with that message (core) — a stage on a rig it does not
                                  belong to, refused by name, before anything runs
  {<kind>: <value>, ...}          a kind an extension registers (`@step_kind`: ext.ansible's
                                  `playbook`, ...)
A command step's outcome is classified from evidence, then by its exit code:
  outcomes: {refused: {exit: nonzero, output: ["does not declare", "{param.url}"]}}
  (evidence: exit = a number, a list, `zero` or `nonzero`; output = a regular expression or a
  list of them, ALL must match; timed_out = true|false)

Each inner step becomes a hidden action `<walk>/<id>` (never named by a plan); the walk's
own contract is derived: `requires` = the first inner step's (unless declared), `readonly` =
every inner step is. Its outcome is the outcome that ended it (`fail`: the triggering
outcome when that is built-in or declared by the walk, else failed; `skip`: skipped;
`{end: x}`: x, which the walk declares in `outcomes:`), or passed.
"""
from __future__ import annotations

import copy
import glob
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api import BUILTIN_OUTCOMES
from .config import ConfigError, RunnerConfig, deep_merge, load_file
from .discovery import ActionSpec, Registry
from . import template

log = logging.getLogger(__name__)

WALK_KEYS = {"doc", "requires", "produces", "produces_on", "outcomes", "readonly", "nolog", "tags",
             "extends", "params", "steps"}
CONTRACT_KEYS = {"id", "when", "requires", "produces", "produces_on", "outcomes", "expect", "on", "retry",
                 "max_visits", "always", "doc", "readonly", "nolog"}
CORE_KINDS = {"action": ("with",), "run": ("env", "cwd", "rules", "timeout", "log"), "refuse": ()}
READONLY_CORE = {"refuse"}                  # core kinds whose steps only look
BUILTIN_REFS = ("param.", "env.", "run.", "step.", "state.")
EVIDENCE = {"exit", "output", "timed_out"}


@dataclass
class WalkSpec:
    name: str
    params: dict[str, Any]
    steps: list[dict]                 # normalised inner steps, in order
    source: str = ""
    program: Any = None               # the compiled planner.Program
    doc: str = ""
    raw: dict = field(default_factory=dict)


def _norm_on(d: dict) -> dict:
    """YAML 1.1 reads a bare `on:` key as the boolean true."""
    return {("on" if k is True else k): v for k, v in d.items()}


def load_raw(rc: RunnerConfig) -> dict[str, tuple[dict, str]]:
    """Every walk spec, by name: the runner config's `actions:` entries that have `steps:`,
    then each `action_files:` file's `actions:` (globs, sorted). A name defined twice is
    refused — a silent shadow would make "which one runs?" a question."""
    out: dict[str, tuple[dict, str]] = {}

    def add(name: str, spec: Any, where: str) -> None:
        if name in out:
            raise ConfigError(f"walk action {name!r} defined twice: {out[name][1]} and {where}")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where}: want a mapping (doc, params, steps, ...)")
        out[name] = (_norm_on(spec), where)

    for name, spec in rc.shell_actions.items():
        if isinstance(spec, dict) and "steps" in spec:
            add(name, spec, f"{rc.file.name}:actions.{name}")
    for pattern in rc.action_files:
        files = sorted(glob.glob(str(rc.root / pattern)))
        if not files and not glob.has_magic(pattern):
            raise ConfigError(f"{rc.file.name}: action_files: {pattern} does not exist")
        for f in files:
            data = load_file(Path(f))
            rel = Path(f).relative_to(rc.root) if Path(f).is_relative_to(rc.root) else Path(f)
            unknown = {k for k in data if not str(k).startswith("x-")} - {"actions"}
            if unknown:
                raise ConfigError(f"{rel}: unknown key(s) {', '.join(sorted(map(str, unknown)))} (want actions:)")
            actions = data.get("actions") or {}
            if not isinstance(actions, dict):
                raise ConfigError(f"{rel}: actions: want a mapping of name -> walk action")
            for name, spec in actions.items():
                if isinstance(spec, dict) and "steps" not in spec and "extends" not in spec:
                    raise ConfigError(f"{rel}:actions.{name}: an action file holds walk actions (steps:)")
                add(name, spec, f"{rel}:actions.{name}")
    return out


def _kind_of(step: dict, kinds: set[str], where: str) -> str:
    found = [k for k in step if k in kinds]
    if len(found) != 1:
        raise ConfigError(f"{where}: an inner step is exactly ONE of {', '.join(sorted(kinds))}"
                          + (f"; it has {', '.join(found)}" if found else f"; it has {', '.join(map(str, step))}"))
    return found[0]


def _default_ids(steps: list[dict], kinds: set[str], where: str) -> list[str]:
    ids: list[str] = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise ConfigError(f"{where}.steps[{i}]: want a mapping ({{action: ...}}, {{run: ...}}, ...)")
        if "id" in st:
            sid = str(st["id"])
            if sid in ids:
                raise ConfigError(f"{where}.steps[{i}]: step id {sid!r} is used twice")
        else:
            kind = _kind_of(st, kinds, f"{where}.steps[{i}]")
            base = str(st[kind]) if kind == "action" else kind
            sid, n = base, 2
            while sid in ids:
                sid, n = f"{base}#{n}", n + 1
        if "/" in sid:
            raise ConfigError(f"{where}.steps[{i}]: step id {sid!r} may not contain `/` (it joins a walk's ids)")
        ids.append(sid)
    return ids


def _resolve_extends(raw: dict[str, tuple[dict, str]], kinds: set[str]) -> dict[str, tuple[dict, str]]:
    """`extends: <walk>`: the base's spec with this one over it — params merged, steps merged
    BY ID (a step naming a base step's id is merged into it: maps merged, lists and scalars
    replaced, a different kind key replaces the base's kind; a new id is appended), every
    other key replaced when given."""
    done: dict[str, tuple[dict, str]] = {}

    def get(name: str, chain: tuple[str, ...]) -> dict:
        if name in done:
            return done[name][0]
        if name in chain:
            raise ConfigError(f"walk action {name!r} extends itself: {' -> '.join((*chain, name))}")
        spec, where = raw[name]
        base_name = spec.get("extends")
        if base_name is None:
            done[name] = (spec, where)
            return spec
        if base_name not in raw:
            raise ConfigError(f"{where}: extends {base_name!r}, which is not a walk action")
        base = copy.deepcopy(get(base_name, (*chain, name)))
        merged = {k: v for k, v in base.items() if k not in ("steps", "params")}
        merged.update({k: copy.deepcopy(v) for k, v in spec.items() if k not in ("steps", "params", "extends")})
        merged["params"] = deep_merge(base.get("params") or {}, spec.get("params") or {})
        base_steps = list(base.get("steps") or [])
        base_ids = _default_ids(base_steps, kinds, f"{raw[base_name][1]}")
        steps = [_norm_on(dict(st)) for st in base_steps]
        for i, st in enumerate(spec.get("steps") or []):
            st = _norm_on(st) if isinstance(st, dict) else st
            sid = st.get("id") if isinstance(st, dict) else None
            if sid is not None and str(sid) in base_ids:
                idx = base_ids.index(str(sid))
                old = steps[idx]
                if any(k in kinds for k in st):          # a new kind replaces the base's
                    old = {k: v for k, v in old.items() if k not in kinds}
                steps[idx] = deep_merge(old, st)
                steps[idx]["id"] = str(sid)
                log.debug("walk %s extends %s: step %s merged into the base's (%s given)", name, base_name, sid,
                          sorted(map(str, st)))
            else:
                steps.append(st)
                log.debug("walk %s extends %s: step %s appended", name, base_name, sid if sid else f"#{i}")
        merged["steps"] = steps
        log.debug("walk %s extends %s: steps %s", name, base_name,
                  _default_ids(steps, kinds, where))
        done[name] = (merged, where)
        return merged

    for name in raw:
        get(name, ())
    return done


def build(reg: Registry, rc: RunnerConfig) -> None:
    """Register every walk action and its inner steps in `reg`, compile them, derive their
    contracts. Called by discovery.collect once the python and shell actions are known."""
    raw = load_raw(rc)
    if not raw:
        return
    for name in raw:
        if name in reg.actions:
            raise ConfigError(f"action {name!r} is both a walk ({raw[name][1]}) and {reg.actions[name].kind} "
                              f"({reg.actions[name].source}): one name, one action")
    kinds = set(CORE_KINDS) | set(reg.step_kinds)
    specs = _resolve_extends(raw, kinds)
    walks: dict[str, WalkSpec] = {}
    for name, (spec, where) in specs.items():
        unknown = set(spec) - WALK_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))}")
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f"{where}: a walk action needs `steps:`, a non-empty list")
        params = spec.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"{where}: params: want {{name: default}}")
        w = WalkSpec(name, params, [], where, doc=str(spec.get("doc", "")), raw=spec)
        walks[name] = w
        reg.actions[name] = ActionSpec(
            name=name, kind="walk", requires=dict(spec.get("requires") or {}),
            produces=dict(spec.get("produces") or {}),
            produces_on={k: dict(v) for k, v in (spec.get("produces_on") or {}).items()},
            outcomes=tuple(spec.get("outcomes") or ()), tags=tuple(spec.get("tags") or ()),
            doc=w.doc, readonly=bool(spec.get("readonly", False)), nolog=bool(spec.get("nolog", False)),
            source=where, walk=w)
    for name, w in walks.items():
        _inner_steps(reg, rc, w, kinds)
    _derive(reg, walks)
    from .planner import Compiler
    for name, w in walks.items():
        plan = {"path": [{"action": f"{name}/{st['id']}", "id": st["id"], "when": st.get("when"),
                          "max_visits": st.get("max_visits", 1), "always": bool(st.get("always", False)),
                          **({"retry": st["retry"]} if "retry" in st else {})} for st in w.steps],
                "on": {st["id"]: st["on"] for st in w.steps if "on" in st},
                "expect": {st["id"]: st["expect"] for st in w.steps if "expect" in st}}
        try:
            w.program = Compiler(rc, reg, walk=name).compile(plan, plan_of=f"walk action {name}")
        except ConfigError as e:
            raise ConfigError(f"{w.source}: {e}") from None
        _check_ends(reg.actions[name], w)
        _check_references(reg, w)
    log.info("walk actions: %s", {n: [st["id"] for st in w.steps] for n, w in walks.items()})


def _inner_steps(reg: Registry, rc: RunnerConfig, w: WalkSpec, kinds: set[str]) -> None:
    spec = w.raw
    ids = _default_ids(spec["steps"], kinds, w.source)
    for i, (st, sid) in enumerate(zip(spec["steps"], ids)):
        where = f"{w.source}.steps[{i}] ({sid})"
        st = _norm_on(st)
        kind = _kind_of(st, kinds, where)
        kind_keys = set(CORE_KINDS[kind]) if kind in CORE_KINDS else set(reg.step_kinds[kind][1])
        unknown = set(st) - CONTRACT_KEYS - kind_keys - {kind}
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))} for a "
                              f"{kind} step (it takes {', '.join(sorted(kind_keys)) or 'only its own value'} "
                              f"besides the step keys)")
        expect = st.get("expect")
        if isinstance(expect, str):
            expect = {"outcome": expect}
        norm = {**st, "id": sid}
        if expect is not None:
            norm["expect"] = expect
        w.steps.append(norm)
        name = f"{w.name}/{sid}"
        common = dict(
            requires=dict(st.get("requires") or {}), produces=dict(st.get("produces") or {}),
            produces_on={k: dict(v) for k, v in (st.get("produces_on") or {}).items()},
            doc=str(st.get("doc", "")),
            readonly=bool(st.get("readonly", kind in READONLY_CORE or kind in reg.readonly_kinds)),
            nolog=bool(st.get("nolog", reg.actions[w.name].nolog)), source=where, inner_of=w.name)
        if kind == "action":
            target = st["action"]
            if target not in reg.actions:
                raise ConfigError(f"{where}: unknown action {target!r}")
            if reg.actions[target].inner_of is not None:
                raise ConfigError(f"{where}: {target!r} is an inner step of another walk")
            if "outcomes" in st:
                raise ConfigError(f"{where}: an action step's outcomes are its action's; classify a "
                                  f"command step's outcomes instead")
            with_ = st.get("with") or {}
            if not isinstance(with_, dict):
                raise ConfigError(f"{where}: with: want a mapping of parameters")
            reg.actions[name] = ActionSpec(
                name=name, kind="call", outcomes=(), call=target, call_with=with_,
                own_effects="produces" in st or "produces_on" in st, **common)
            log.debug("walk %s: inner step %s calls %s with %s; own effects %s", w.name, sid, target,
                      with_ or "{}", {k: st[k] for k in ("requires", "produces", "produces_on") if k in st} or "none")
        else:
            classify = st.get("outcomes") or {}
            if not isinstance(classify, dict):
                raise ConfigError(f"{where}: outcomes: want {{<outcome>: {{exit, output, timed_out}}}} "
                                  f"(the evidence that names it)")
            for oname, ev in classify.items():
                if oname in BUILTIN_OUTCOMES and oname not in ("failed",):
                    raise ConfigError(f"{where}: outcomes.{oname}: a built-in outcome is not classified")
                if not isinstance(ev, dict) or not ev or set(ev) - EVIDENCE:
                    raise ConfigError(f"{where}: outcomes.{oname}: want evidence {{{', '.join(sorted(EVIDENCE))}}}")
                ex = ev.get("exit")
                if ex is not None and not (ex in ("zero", "nonzero") or isinstance(ex, int)
                                           or (isinstance(ex, list) and all(isinstance(x, int) for x in ex))):
                    raise ConfigError(f"{where}: outcomes.{oname}.exit: want a number, a list, zero or nonzero")
            body = {k: v for k, v in st.items() if k == kind or k in kind_keys}
            reg.actions[name] = ActionSpec(
                name=name, kind="cmd", outcomes=tuple(o for o in classify if o not in BUILTIN_OUTCOMES),
                inner={"kind": kind, "spec": body, "classify": classify}, **common)
            log.debug("walk %s: inner step %s is a %s step %s; requires %s, produces %s, produces_on %s; "
                      "classified outcomes %s", w.name, sid, kind, "(NOLOG)" if common["nolog"] else body,
                      common["requires"], common["produces"], common["produces_on"], classify or "none")


def _derive(reg: Registry, walks: dict[str, WalkSpec]) -> None:
    """Fill the contracts that come from somewhere else, callees first: an inner step that
    calls an action has that action's guard (plus its own), outcomes, readonly and — unless
    it declares its own — effects; a walk's `requires` is its first inner step's unless
    declared, and it is read-only when every inner step is."""
    state: dict[str, str] = {}

    def derive(name: str, chain: tuple[str, ...]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "busy":
            raise ConfigError(f"walk action {name!r} calls itself: {' -> '.join((*chain, name))}")
        state[name] = "busy"
        w = walks[name]
        act = reg.actions[name]
        for st in w.steps:
            inner = reg.actions[f"{name}/{st['id']}"]
            if inner.kind != "call":
                continue
            if inner.call in walks:
                derive(inner.call, (*chain, name))
            target = reg.actions[inner.call]
            inner.requires = {**target.requires, **inner.requires}
            inner.outcomes = target.outcomes
            inner.readonly = target.readonly
            if not inner.own_effects:
                inner.produces = dict(target.produces)
                inner.produces_on = {k: dict(v) for k, v in target.produces_on.items()}
        first = reg.actions[f"{name}/{w.steps[0]['id']}"]
        if not act.requires:
            act.requires = dict(first.requires)
        if not w.raw.get("readonly"):
            act.readonly = all(reg.actions[f"{name}/{st['id']}"].readonly for st in w.steps)
            if not act.readonly:
                log.debug("walk %s: not read-only — inner step(s) %s change state or run commands", name,
                          [st["id"] for st in w.steps if not reg.actions[f"{name}/{st['id']}"].readonly])
        log.debug("walk %s: requires %s (%s), readonly %s", name, act.requires,
                  "declared" if w.raw.get("requires") else f"its first step {first.name}'s", act.readonly)
        state[name] = "done"

    for name in walks:
        derive(name, ())


def _check_ends(act: ActionSpec, w: WalkSpec) -> None:
    prog = w.program
    reactions = [r for by in prog.reactions.values() for r in by.values()]
    reactions += [r.reaction for rules in prog.rules.values() for r in rules]
    reactions += [r for r, _ in prog.segment_then.values()]
    for r in reactions:
        if r.kind == "end" and r.target not in act.all_outcomes:
            raise ConfigError(f"{w.source}: a step ends the walk {r.target!r}, which the walk does not declare "
                              f"(outcomes: [{r.target}])")


def _check_references(reg: Registry, w: WalkSpec) -> None:
    """Every `{name}` a walk's values reference must be a built-in reference or a resolver
    that is registered — a typo is refused when the walk loads, not forty minutes in."""
    values: list[Any] = [w.params]
    for st in w.steps:
        act = reg.actions[f"{w.name}/{st['id']}"]
        values.append(act.call_with if act.kind == "call" else (act.inner or {}).get("spec"))
        if act.kind == "cmd":
            values.append(act.inner["classify"])
    for v in values:
        try:
            names = template.references(v)
        except template.TemplateError as e:
            raise ConfigError(f"{w.source}: {e}") from None
        for n in names:
            if not n.startswith(BUILTIN_REFS) and n not in reg.resolvers:
                raise ConfigError(f"{w.source}: {{{n}}} is neither a built-in reference "
                                  f"({', '.join(p + '...' for p in BUILTIN_REFS)}) nor a registered resolver "
                                  f"({', '.join(sorted(reg.resolvers)) or 'none registered'})")
