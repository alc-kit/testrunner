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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError

ANY = "*"                       # a transition from any value (a plain `produces: {var: value}`)
VAR_KEYS = {"values", "initial", "validate", "transitions", "observe", "model_only"}
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
    model_only = spec.get("model_only", False)
    if not isinstance(model_only, bool):
        raise ConfigError(f"{where}.model_only: want true or false")
    if model_only and order is not None:
        raise ConfigError(f"{where}: model_only and observe exclude each other — declare one")
    return StateVar(var, values, initial, criteria, validators, transitions, order, model_only)


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
    problems = []
    for t in sorted(set(claimed) - declared):
        problems.append(f"  {t.text()}: claimed by the action ({claimed[t]}), "
                        f"NOT declared under states.{t.var}.transitions")
    for t in sorted(declared - set(claimed)):
        act = reg.actions.get(t.by)
        if act is None:
            problems.append(f"  {t.text()}: declared, but there is no action {t.by!r}")
        else:
            problems.append(f"  {t.text()}: declared under states.{t.var}.transitions, NOT claimed by "
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
