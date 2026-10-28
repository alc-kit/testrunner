"""The declared state machine: every state variable's values, what each value MEANS (its
criterion), how a claimed value is CONFIRMED (its validator), and every TRANSITION — which
action may change the variable, on which outcome, from which value to which.

The runner config declares it (`states:`); the actions declare their effects (`produces`,
`produces_on`). The two must AGREE, line for line: an action that claims a change the runner
config does not declare, or a declared change no action claims, makes the plan UNDECIDED, and
it is refused before anything runs. Undefined behaviour is a failure, never a default.

    states:
      rig:
        initial: absent
        validate: {outcome: true}        # every value's validator, unless the value names its own
        values:
          absent:    {criterion: "no machine of the scenario exists"}
          up:        {criterion: "every machine runs", validate: {playbook: verify/up.yml}}
          installed: {}
        transitions:
          - {from: absent, to: up,        by: up,      on: passed}
          - {from: up,     to: installed, by: install, on: passed}
          - {from: "*",    to: absent,    by: destroy, on: passed}
        observe: {order: [installed, up, absent]}   # or: model_only: true, or an @observer

The transitions are the COMPLETE list for the variable: an action/outcome pair that is not in
it changes nothing, and an action may not claim otherwise.

The same machine can be written as a `machine:` block instead of `initial` + `transitions`
(D8, user 2026-10-28): Mermaid stateDiagram-v2 lines with UML labels, which render on GitHub
unchanged when pasted into a ```mermaid fence:

    states:
      rig:
        values: [absent, up, installed]
        validate: {outcome: true}
        machine: |
          [*]    --> absent
          absent --> up        : up [passed]
          up     --> installed : install [passed]

The accepted subset is exactly: an optional `stateDiagram-v2` first line, `[*] --> <value>` (the
initial value, at most once), `<value> --> <value> : <action> [<outcome>]`, and `%%` comments on
their own line. Anything else is refused with its line and the reason. The YAML records stay
valid during the migration window; a variable uses one form, not both.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError

ANY = "*"                       # a transition from any value (a plain `produces: {var: value}`)
VAR_KEYS = {"values", "initial", "validate", "transitions", "machine", "observe", "model_only"}
# the machine: notation (D8): Mermaid state ids, UML `action [outcome]` labels
_STATE_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_ACTION = r"[A-Za-z0-9_][A-Za-z0-9_./-]*"
_OUTCOME = r"[A-Za-z_][A-Za-z0-9_-]*"
_INITIAL_RE = re.compile(rf"^\[\*\]\s*-->\s*({_STATE_ID})$")
_LINE_RE = re.compile(rf"^({_STATE_ID})\s*-->\s*({_STATE_ID})\s*:\s*({_ACTION})\s*\[\s*({_OUTCOME})\s*\]$")
VALUE_KEYS = {"criterion", "validate"}
TRANSITION_KEYS = {"from", "to", "by", "on"}
OBSERVE_KEYS = {"order"}


@dataclass(frozen=True, order=True)
class Transition:
    var: str
    by: str                     # the action that makes the change (an inner step: <walk>/<id>)
    on: str                     # the outcome it makes it on
    frm: str                    # a value, or ANY
    to: str

    def text(self) -> str:
        return f"{self.var}: {self.frm} -> {self.to} by {self.by} on {self.on}"


@dataclass
class StateVar:
    name: str
    values: list[str]
    initial: str | None = None
    criteria: dict[str, str] = field(default_factory=dict)
    validators: dict[str, dict] = field(default_factory=dict)   # value -> validator spec
    transitions: list[Transition] = field(default_factory=list)
    order: list[str] | None = None          # observe: {order}: an observer derived from validators
    model_only: bool = False                # nothing re-reads it: its value is the model's claim
    form: str = "transitions"               # how the runner config declares it: `transitions` or `machine`

    # the dict shape the rest of testrunner reads (rc.states[var]["values"], .get("initial"))
    def __getitem__(self, key: str) -> Any:
        if key == "values":
            return self.values
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return {"values": self.values, "initial": self.initial}.get(key, default)

    def transitions_by(self, by: str) -> list[Transition]:
        return [t for t in self.transitions if t.by == by]


def parse_states(path: Path, data: Any) -> dict[str, StateVar]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: states: want {{<variable>: {{values, ...}}}}")
    return {str(var): _parse_var(path, str(var), spec) for var, spec in data.items()}


def _parse_var(path: Path, var: str, spec: Any) -> StateVar:
    where = f"{path}: states.{var}"
    if isinstance(spec, list):
        spec = {"values": spec}
    if not isinstance(spec, dict):
        raise ConfigError(f"{where}: want {{values, initial, validate, transitions, observe | model_only}}")
    unknown = set(spec) - VAR_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))} "
                          f"(it takes {', '.join(sorted(VAR_KEYS))})")
    raw_values = spec.get("values")
    criteria: dict[str, str] = {}
    own_validators: dict[str, dict] = {}
    if isinstance(raw_values, list) and raw_values and all(isinstance(v, str) for v in raw_values):
        values = list(raw_values)
    elif isinstance(raw_values, dict) and raw_values:
        values = []
        for value, vspec in raw_values.items():
            value = str(value)
            values.append(value)
            vspec = vspec or {}
            if not isinstance(vspec, dict) or set(vspec) - VALUE_KEYS:
                raise ConfigError(f"{where}.values.{value}: want {{criterion: <text>, validate: <validator>}}")
            if "criterion" in vspec:
                if not isinstance(vspec["criterion"], str) or not vspec["criterion"].strip():
                    raise ConfigError(f"{where}.values.{value}.criterion: want a sentence saying what the value means")
                criteria[value] = vspec["criterion"].strip()
            if "validate" in vspec:
                own_validators[value] = _validator_shape(f"{where}.values.{value}.validate", vspec["validate"])
    else:
        raise ConfigError(f"{where} needs `values`: a non-empty list of names, or a mapping "
                          f"{{<value>: {{criterion, validate}}}}")
    if len(set(values)) != len(values):
        raise ConfigError(f"{where}: a value is listed twice")
    if ANY in values:
        raise ConfigError(f"{where}: {ANY!r} is not a value: it means 'any value' in a transition")
    initial = spec.get("initial")
    if initial is not None and initial not in values:
        raise ConfigError(f"{where}.initial {initial!r} is not one of its values")
    validators = {}
    if "validate" in spec:
        default = _validator_shape(f"{where}.validate", spec["validate"])
        validators = {v: default for v in values}
    validators.update(own_validators)
    if "machine" in spec:
        for other in ("transitions", "initial"):
            if other in spec:
                raise ConfigError(f"{where}: `machine` and `{other}` exclude each other — the machine block "
                                  f"declares {'its transitions' if other == 'transitions' else 'the initial value ([*] --> <value>)'}; "
                                  f"declare one form")
        initial, transitions = parse_machine(where, var, values, spec["machine"])
    else:
        transitions = _parse_transitions(where, var, values, spec.get("transitions"))
    order = None
    if "observe" in spec:
        obs = spec["observe"]
        if not isinstance(obs, dict) or set(obs) != OBSERVE_KEYS:
            raise ConfigError(f"{where}.observe: want {{order: [<every value, the one to ask first first>]}}")
        order = obs["order"]
        if not isinstance(order, list) or sorted(map(str, order)) != sorted(values):
            raise ConfigError(f"{where}.observe.order must list every value exactly once (values: {values}); "
                              f"an observer that cannot say a value is undefined")
        order = [str(v) for v in order]
    _one_end_result(where, transitions)
    model_only = spec.get("model_only", False)
    if not isinstance(model_only, bool):
        raise ConfigError(f"{where}.model_only: want true or false")
    if model_only and order is not None:
        raise ConfigError(f"{where}: model_only and observe exclude each other — declare one")
    return StateVar(var, values, initial, criteria, validators, transitions, order, model_only,
                    form="machine" if "machine" in spec else "transitions")


def _parse_transitions(where: str, var: str, values: list[str], raw: Any) -> list[Transition]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{where}.transitions: want a list of {{from, to, by, on}}")
    out: list[Transition] = []
    for i, t in enumerate(raw):
        w = f"{where}.transitions[{i}]"
        if not isinstance(t, dict):
            raise ConfigError(f"{w}: want {{from, to, by, on}}")
        # YAML 1.1 reads a bare `on:` key as the boolean true
        t = {("on" if k is True else k): v for k, v in t.items()}
        missing = TRANSITION_KEYS - set(t)
        unknown = set(t) - TRANSITION_KEYS
        if missing or unknown:
            raise ConfigError(f"{w}: " + "; ".join(
                ([f"missing {', '.join(sorted(missing))}"] if missing else [])
                + ([f"unknown {', '.join(sorted(map(str, unknown)))}"] if unknown else []))
                + " (a transition is {from, to, by, on}, every one stated)")
        frm, to, by, on = (str(t[k]) for k in ("from", "to", "by", "on"))
        if frm != ANY and frm not in values:
            raise ConfigError(f"{w}: from {frm!r} is not one of {values} (or {ANY!r})")
        if to not in values:
            raise ConfigError(f"{w}: to {to!r} is not one of {values}")
        if frm == to:
            raise ConfigError(f"{w}: from and to are both {to!r}: that is not a change")
        tr = Transition(var, by, on, frm, to)
        if tr in out:
            raise ConfigError(f"{w}: declared twice ({tr.text()})")
        out.append(tr)
    return out



def parse_machine(where: str, var: str, values: list[str], text: Any) -> tuple[str | None, list[Transition]]:
    """A `machine:` block (D8): Mermaid stateDiagram-v2 lines with UML `action [outcome]` labels.
    Returns (initial value or None, transitions). Every line outside the accepted subset is refused
    with its line number and the reason: explicit rather than implicit, undefined is a failure."""
    w = f"{where}.machine"
    if not isinstance(text, str) or not text.strip():
        raise ConfigError(f"{w}: want a block of lines (`machine: |`), e.g. `[*] --> absent` and "
                          f"`absent --> up : up [passed]`")
    initial: str | None = None
    out: list[Transition] = []
    seen_body = False
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        at = f"{w} line {n} ({line!r})"
        if not line or line.startswith("%%"):
            continue
        if line == "stateDiagram-v2":
            if seen_body:
                raise ConfigError(f"{at}: `stateDiagram-v2` may only be the first line")
            seen_body = True
            continue
        seen_body = True
        if "%%" in line:
            raise ConfigError(f"{at}: a `%%` comment must be on its own line (Mermaid does not accept one "
                              f"after a statement, so the block would not render)")
        m = _INITIAL_RE.match(line)
        if m:
            if initial is not None:
                raise ConfigError(f"{at}: a second `[*] -->` line; the initial value is declared once "
                                  f"(already {initial!r})")
            if m.group(1) not in values:
                raise ConfigError(f"{at}: {m.group(1)!r} is not one of {values}")
            initial = m.group(1)
            continue
        if line.startswith("[*]"):
            raise ConfigError(f"{at}: `[*]` marks the initial value only: `[*] --> <value>`, with no label")
        if re.search(r"-->\s*\[\*\]", line):
            raise ConfigError(f"{at}: a transition to `[*]` (an end state) has no meaning here; a value is "
                              f"never left for 'nothing'")
        if "/" in line.split(":", 1)[-1] and "[" in line and line.rsplit("]", 1)[-1].strip().startswith("/"):
            raise ConfigError(f"{at}: a UML effect (`/ ...`) is not accepted yet; it is reserved for "
                              f"updates of declared data (`/ expected += ...`, WP step B4)")
        m = _LINE_RE.match(line)
        if not m:
            raise ConfigError(f"{at}: not a machine line. Accepted: `[*] --> <value>`, "
                              f"`<value> --> <value> : <action> [<outcome>]`, `%%` comments on their own line "
                              f"(and an optional first line `stateDiagram-v2`)")
        frm, to, by, on = m.groups()
        for v in (frm, to):
            if v not in values:
                raise ConfigError(f"{at}: {v!r} is not one of {values}")
        if frm == to:
            raise ConfigError(f"{at}: from and to are both {to!r}: a self-loop is not accepted yet (it arrives "
                              f"with every outcome written out, WP step B3)")
        tr = Transition(var, by, on, frm, to)
        if tr in out:
            raise ConfigError(f"{at}: declared twice ({tr.text()})")
        out.append(tr)
    return initial, out


def _one_end_result(where: str, transitions: list[Transition]) -> None:
    """One value, one action, one outcome: ONE declared end result. Two would leave the runner to
    pick one (it used to take the first), which is undefined. Several failure modes for one
    outcome, told apart by their validators and routed by the plan, arrive with WP step B1c."""
    seen: dict[tuple[str, str, str], set[str]] = {}
    for t in transitions:
        seen.setdefault((t.frm, t.by, t.on), set()).add(t.to)
    for (frm, by, on), tos in seen.items():
        also = seen.get((ANY, by, on), set()) if frm != ANY else set()
        targets = tos | also
        if len(targets) > 1:
            raise ConfigError(f"{where}: {by} [{on}] from {frm!r} has {len(targets)} end results "
                              f"({', '.join(sorted(targets))}){' (with the * line)' if also else ''}: one value, one "
                              f"action and one outcome may declare ONE. Several failure modes told apart by their "
                              f"validators are not supported yet (WP step B1c).")


def state_event_table(sv: "StateVar") -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    """The machine as a state x event table (D8): (the actions, in declaration order; {value:
    {action: ["-> <to> [<outcome>]", ...]}}). A transition from any value (`*`, the YAML form) is
    expanded into every row and marked "(any)". An empty cell is a pair that claims nothing."""
    actions: list[str] = []
    for t in sv.transitions:
        if t.by not in actions:
            actions.append(t.by)
    rows: dict[str, dict[str, list[str]]] = {v: {a: [] for a in actions} for v in sv.values}
    for t in sv.transitions:
        for v in (sv.values if t.frm == ANY else [t.frm]):
            rows[v][t.by].append(f"-> {t.to} [{t.on}]" + (" (any)" if t.frm == ANY else ""))
    return actions, rows


def table_text(sv: "StateVar", indent: str = "    ") -> list[str]:
    """The state x event table as text lines (for --list). '·' = claims nothing."""
    actions, rows = state_event_table(sv)
    if not actions:
        return []
    cells = {v: {a: ", ".join(rows[v][a]) or "·" for a in actions} for v in sv.values}
    w0 = max(len(v) for v in sv.values)
    ws = {a: max([len(a)] + [len(cells[v][a]) for v in sv.values]) for a in actions}
    head = indent + " " * w0 + " | " + " | ".join(a.ljust(ws[a]) for a in actions)
    out = [head, indent + "-" * w0 + "-+-" + "-+-".join("-" * ws[a] for a in actions)]
    out += [indent + v.ljust(w0) + " | " + " | ".join(cells[v][a].ljust(ws[a]) for a in actions)
            for v in sv.values]
    out.append(indent + "· = this action claims nothing from this value")
    return out

def _validator_shape(where: str, spec: Any) -> dict:
    """Only the shape here (one kind key); the kind itself is checked once every extension's
    validator kinds are known (validate.check_kinds)."""
    if not isinstance(spec, dict) or not spec:
        raise ConfigError(f"{where}: want {{<kind>: <value>, ...}}, e.g. {{outcome: true}}, "
                          f"{{run: [script, arg]}}, {{playbook: verify/x.yml}}")
    return dict(spec)


# ── the agreement check ──────────────────────────────────────────────────────

def claimer(act: Any) -> str:
    """Whose claim an action's effects are: an inner step that calls an action without
    declaring effects of its own carries that action's claims, so they are that action's."""
    if act.kind == "call" and not act.own_effects and act.call:
        return act.call
    return act.name


def claims_of(act: Any) -> set[Transition]:
    """The transitions an action's own produces/produces_on claim."""
    out: set[Transition] = set()
    for outcome, spec in [("passed", act.produces), *act.produces_on.items()]:
        for var, val in spec.items():
            if isinstance(val, dict):
                for frm, to in val.items():
                    out.add(Transition(var, act.name, outcome, str(frm), str(to)))
            else:
                out.add(Transition(var, act.name, outcome, ANY, str(val)))
    return out


def check_agreement(reg: Any, states: dict[str, StateVar]) -> None:
    """Refuse an UNDECIDED machine: every transition an action claims must be declared in the
    runner config for that action and outcome, and every declared transition must be claimed.
    Every mismatch is listed, with the side it is missing from."""
    claimed: dict[Transition, str] = {}
    for act in reg.actions.values():
        if claimer(act) != act.name:
            continue                    # the called action's claims, checked as that action
        for t in claims_of(act):
            if t.frm == t.to:
                continue                # {x: x} in a transition map: no change, nothing to agree on
            claimed[t] = act.source
    declared = {t for sv in states.values() for t in sv.transitions}

    def where(var: str) -> str:
        sv = states.get(var)
        return f"states.{var}.{sv.form if sv else 'transitions'}"
    problems = []
    for t in sorted(set(claimed) - declared):
        problems.append(f"  {t.text()}: claimed by the action ({claimed[t]}), "
                        f"NOT declared under {where(t.var)}")
    for t in sorted(declared - set(claimed)):
        act = reg.actions.get(t.by)
        if act is None:
            problems.append(f"  {t.text()}: declared, but there is no action {t.by!r}")
        else:
            problems.append(f"  {t.text()}: declared under {where(t.var)}, NOT claimed by "
                            f"the action ({act.source}: its produces/produces_on)")
    if problems:
        raise ConfigError("the plan is UNDECIDED: the runner config's transitions and the actions' claims "
                          "do not agree (undefined behaviour is a failure):\n" + "\n".join(problems))


def check_declarations(reg: Any, states: dict[str, StateVar]) -> None:
    """Every value a transition touches has a validator (D5); every variable a transition
    changes is either read by an observer (hand-written, or derived from its validators in a
    declared order) or declared model_only."""
    problems = []
    for sv in states.values():
        touched = {t.to for t in sv.transitions} | {t.frm for t in sv.transitions if t.frm != "*"}
        if any(t.frm == "*" for t in sv.transitions):
            touched |= set(sv.values)   # from any value: any value is checked after a failure
        for value in sorted(touched - set(sv.validators), key=sv.values.index):
            problems.append(f"  states.{sv.name}: value {value!r} has no validator — a transition touches it, "
                            f"so a claim of it could not be confirmed (declare validate: on the value or "
                            f"the variable; {{outcome: true}} or {{const: true}} are valid choices)")
        observed = sv.name in reg.observers
        if observed and sv.order is not None:
            problems.append(f"  states.{sv.name}: has an @observer AND observe.order — declare one")
        if observed and sv.model_only:
            problems.append(f"  states.{sv.name}: has an @observer but is declared model_only")
        # a variable no transition changes is a constant (its `initial`): nothing to re-read
        if sv.transitions and not observed and sv.order is None and not sv.model_only:
            problems.append(f"  states.{sv.name}: nothing reads it — export an @observer, declare "
                            f"observe: {{order: [...]}}, or declare model_only: true")
    if problems:
        raise ConfigError("the state machine is not fully declared (undefined behaviour is a failure):\n"
                          + "\n".join(problems))


def describe(states: dict[str, StateVar], reg: Any) -> list[dict]:
    """The declared machine as plain data, for the journal (run_start) and the reports."""
    from .validate import spec_text
    out = []
    for sv in states.values():
        read = (f"observed in order {', '.join(sv.order)}" if sv.order else
                "observed" if sv.name in reg.observers else "model only" if sv.model_only else
                "constant" if not sv.transitions else "not read back")
        out.append({
            "var": sv.name, "initial": sv.initial, "read": read,
            "values": [{"value": v, "criterion": sv.criteria.get(v, ""),
                        "validator": spec_text(sv.validators[v]) if v in sv.validators else ""}
                       for v in sv.values],
            "transitions": [{"from": t.frm, "to": t.to, "by": t.by, "on": t.on} for t in sv.transitions],
            "table": dict(zip(("actions", "rows"), state_event_table(sv))),
        })
    return out


class DerivedObserver:
    """The observer of a variable that declares `observe: {order: [...]}`: its validators are
    asked in that order, and the first value that HOLDS is the answer. A value that cannot
    tell, or no value holding, means it cannot look (the runner keeps the model's value and
    says so). The runner calls it (runner._observe); it is registered like an @observer."""

    def __init__(self, sv: StateVar):
        self.sv = sv
        self.__name__ = f"observe-in-order({sv.name}: {', '.join(sv.order or [])})"
