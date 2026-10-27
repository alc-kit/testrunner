"""Validators: does `var=value` hold now? Three answers — holds, does not hold, cannot tell.

A validator is declared per value in the runner config (`states.<var>.validate`, or per value).
How strong it is, is the subscriber's to decide and to describe; the runner promises only that
one is declared, that it runs, and that every record says which one confirmed what.

    {outcome: true}            the claiming step ended on the outcome that claims the change
    {outcome: [build, fetch]}  ... or: these inner steps of the claiming walk passed
    {const: true}              always holds — the claim needs no check (and after a failure,
                               cannot tell the value before from the value after)
    {run: [argv] | "shell"}    exit 0 holds, 1 does not hold, anything else cannot tell
                               (keys: env, cwd, timeout)
    {python: <name>}           an @validator function
    {<kind>: ...}              a kind an extension exports (@validator_kind): ext.ansible's
                               `playbook:` and `adhoc:`, always run with --check

A validator's values are templates like a walk step's ({param.node}, {run.root}, {env.X}).
Validators only LOOK; `run:` and `python:` ones cannot be forced to, so the subscriber who
writes one promises it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import template
from .api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS, PASSED
from .config import ConfigError
from .fixtures import Scope, call

log = logging.getLogger(__name__)

CORE_KINDS = {"outcome": (), "const": (), "run": ("env", "cwd", "timeout"), "python": ()}
# kinds that look at the system: only these can answer outside the step that made a claim
PROBES_CORE = {"run", "python"}
ANSWERS = (HOLDS, DOES_NOT_HOLD, CANNOT_TELL)

# what a validator is asked
CLAIMED = "claimed"     # the value the step's outcome claims
BEFORE = "before"       # the value the variable had before the step
OBSERVE = "observe"     # an observer derived from validators asks it, outside any claim


@dataclass
class Claim:
    """What a validator is asked about. `claimed`: the step ended on an outcome on which it
    declares a change of this variable from the value it had. `inner`: a walk's inner steps'
    outcomes, by inner id."""
    var: str
    value: str
    role: str
    step: str | None = None
    outcome: str | None = None
    claimed: bool = False
    inner: dict[str, str] = field(default_factory=dict)
    log: str = "validate.log"


@dataclass
class Verdict:
    var: str
    value: str
    role: str
    kind: str
    spec: dict
    answer: str
    why: str = ""
    seconds: float = 0.0

    @property
    def holds(self) -> bool:
        return self.answer == HOLDS

    def text(self) -> str:
        return (f"{self.var}={self.value} ({self.role}): {self.answer} — {self.kind} {self.spec_text()}"
                + (f": {self.why}" if self.why else ""))

    def spec_text(self) -> str:
        return ", ".join(f"{k}: {v}" for k, v in self.spec.items())

    def record(self) -> dict:
        return {"var": self.var, "value": self.value, "role": self.role, "kind": self.kind,
                "validator": self.spec_text(), "answer": self.answer, "why": self.why,
                "seconds": round(self.seconds, 2)}


def kind_of(spec: dict, reg: Any) -> str:
    kinds = [k for k in spec if k in CORE_KINDS or k in reg.validator_kinds]
    if len(kinds) != 1:
        raise ConfigError(f"want exactly one validator kind, got {kinds or 'none'} in {spec} "
                          f"(kinds: {', '.join(sorted({*CORE_KINDS, *reg.validator_kinds}))})")
    return kinds[0]


def is_probe(spec: dict, reg: Any) -> bool:
    """A validator that looks at the system, so it can answer outside the step that claimed."""
    kind = kind_of(spec, reg)
    return kind in PROBES_CORE or kind in reg.validator_kinds


def check_validators(reg: Any, states: dict) -> None:
    """Every declared validator is well-formed, its kind exists, and what it names exists."""
    problems = []
    for sv in states.values():
        for value, spec in sv.validators.items():
            where = f"states.{sv.name}: value {value!r}: validate {spec}"
            try:
                kind = kind_of(spec, reg)
            except ConfigError as e:
                problems.append(f"  {where}: {e}")
                continue
            keys = CORE_KINDS[kind] if kind in CORE_KINDS else reg.validator_kinds[kind][1]
            extra = set(spec) - {kind} - set(keys)
            if extra:
                problems.append(f"  {where}: unknown key(s) {', '.join(sorted(map(str, extra)))} for a {kind} "
                                f"validator (it takes {', '.join(keys) or 'only its own value'})")
            v = spec[kind]
            if kind == "const" and v is not True:
                problems.append(f"  {where}: const takes only true")
            elif kind == "outcome":
                if v is True:
                    pass
                elif isinstance(v, list) and v and all(isinstance(i, str) for i in v):
                    claimers = {t.by for t in sv.transitions if value in (t.to, t.frm)}
                    for by in sorted(claimers):
                        act = reg.actions.get(by)
                        ids = {st["id"] for st in act.walk.steps} if act is not None and act.walk else None
                        if ids is None:
                            problems.append(f"  {where}: names inner steps, but {by!r} (a transition to or "
                                            f"from it) is not a walk action")
                        elif set(v) - ids:
                            problems.append(f"  {where}: {by!r} has no inner step(s) "
                                            f"{', '.join(sorted(set(v) - ids))} (it has {', '.join(sorted(ids))})")
                else:
                    problems.append(f"  {where}: outcome takes true, or a list of the claiming walk's inner step ids")
            elif kind == "python" and v not in reg.validators:
                problems.append(f"  {where}: no @validator named {v!r} (exported: "
                                f"{', '.join(sorted(reg.validators)) or 'none'})")
            elif kind == "run" and not (isinstance(v, str) or (isinstance(v, list) and v)):
                problems.append(f"  {where}: run takes a command line or a non-empty argv list")
        if sv.order is not None:
            for value in sv.order:
                spec = sv.validators.get(value)
                if spec is None:
                    problems.append(f"  states.{sv.name}: observe.order asks {value!r}, which has no validator")
                elif kind_of(spec, reg) not in PROBES_CORE and kind_of(spec, reg) not in reg.validator_kinds:
                    problems.append(f"  states.{sv.name}: observe.order asks {value!r}, whose validator "
                                    f"({kind_of(spec, reg)}) cannot look at the system — an observer cannot be "
                                    f"derived from it (a probe: run, python, or an extension's kind)")
    if problems:
        raise ConfigError("validators (undefined behaviour is a failure):\n" + "\n".join(problems))


async def judge(reg: Any, sv: Any, claim: Claim, *, fixtures: Any, scope: Scope, proc: Any,
                resolve: Callable | None, root: Any) -> Verdict:
    """Ask the value's validator. Never raises: a validator that cannot answer says so."""
    spec = dict(sv.validators[claim.value])
    kind = kind_of(spec, reg)
    started = time.monotonic()
    try:
        if resolve is not None and kind not in ("outcome", "const"):
            spec = await template.render(spec, resolve)
        answer, why = await _ask(reg, kind, spec, claim, fixtures, scope, proc, root)
    except Exception as e:  # noqa: BLE001 — a validator that breaks cannot tell
        log.warning("validator for %s=%s (%s) raised %s: %s", claim.var, claim.value, kind, type(e).__name__, e,
                    exc_info=True)
        answer, why = CANNOT_TELL, f"it raised {type(e).__name__}: {e}"
    if answer not in ANSWERS:
        answer, why = CANNOT_TELL, f"it answered {answer!r}, not one of {', '.join(ANSWERS)}"
    v = Verdict(claim.var, claim.value, claim.role, kind, spec, answer, why, time.monotonic() - started)
    log.info("validate %s", v.text())
    return v


async def _ask(reg: Any, kind: str, spec: dict, claim: Claim, fixtures: Any, scope: Scope, proc: Any,
               root: Any) -> tuple[str, str]:
    v = spec[kind]
    if kind == "const":
        return HOLDS, "declared always true"
    if kind == "outcome":
        if claim.role == OBSERVE:
            return CANNOT_TELL, "an outcome validator only answers right after the step that claims"
        if v is True:
            reached = claim.claimed
            why = (f"{claim.step} ended {claim.outcome}, "
                   + ("an outcome that claims the change" if reached else "no outcome that claims the change"))
        else:
            missing = [i for i in v if claim.inner.get(i) != PASSED]
            reached = not missing
            why = ("inner step(s) " + ", ".join(v) + " passed" if reached else
                   "inner step(s) " + ", ".join(f"{i} ({claim.inner.get(i, 'did not run')})" for i in missing)
                   + " did not pass")
        holds = reached if claim.role == CLAIMED else not reached
        return (HOLDS if holds else DOES_NOT_HOLD), why
    if kind == "run":
        argv = [str(w) for w in v] if isinstance(v, list) else ["bash", "-c", str(v)]
        env = {k: None if x is None else str(x) for k, x in (spec.get("env") or {}).items()}
        cwd = root / str(spec["cwd"]) if spec.get("cwd") else root
        r = await proc.run(argv, cwd=cwd, env=env, log=claim.log, timeout=spec.get("timeout"), echo=False,
                           ask_operator=False)
        tail = " | ".join(r.text.strip().splitlines()[-3:])
        if r.timed_out:
            return CANNOT_TELL, "timed out" + (f"; last output: {tail}" if tail else "")
        if r.unanswered is not None:
            return CANNOT_TELL, f"it asked {r.unanswered!r}"
        answer = {0: HOLDS, 1: DOES_NOT_HOLD}.get(r.returncode, CANNOT_TELL)
        return answer, f"exit {r.returncode}" + (f"; last output: {tail}" if tail else "")
    if kind == "python":
        fn, _ = reg.validators[v]
        kwargs = await fixtures.kwargs_for(fn, Scope("validate", {"claim": claim}, parent=scope))
        got = await call(fn, kwargs)
        why = ""
        if isinstance(got, tuple) and len(got) == 2:
            got, why = got
        if got is True:
            return HOLDS, str(why)
        if got is False:
            return DOES_NOT_HOLD, str(why)
        if got is None:
            return CANNOT_TELL, str(why) or "it could not tell"
        return CANNOT_TELL, f"it returned {got!r}, not True/False/None"
    fn, _, _ = reg.validator_kinds[kind]
    kwargs = await fixtures.kwargs_for(fn, Scope("validate", {"spec": spec, "claim": claim}, parent=scope))
    got = await call(fn, kwargs)
    if not (isinstance(got, tuple) and len(got) == 2):
        return CANNOT_TELL, f"the {kind} validator kind returned {got!r}, not (answer, why)"
    return got[0], str(got[1])
