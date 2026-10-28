"""The decorators and result types a subscriber imports.

Like pytest markers, the decorators only attach metadata to the function; nothing is
registered globally. Discovery (discovery.py) scans the subscriber's modules for marked
objects, so importing a module twice, or two runners in one process, cannot collide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

MARK = "__testrunner__"

# Outcomes every action can have. Anything else must be declared by the action.
PASSED, FAILED, SKIPPED, ERRORED, ABORTED = "passed", "failed", "skipped", "errored", "aborted"
BUILTIN_OUTCOMES = frozenset({PASSED, FAILED, SKIPPED, ERRORED, ABORTED})

HOOKS = frozenset({
    "run_config",  # (config, how, file, root) — SYNCHRONOUS; as a run config loads, before any plan
    "run_start",   # (run)
    "step_start",  # (run, step)
    "step_end",    # (run, step, result)
    "run_end",     # (run, result)
})


@dataclass
class Outcome:
    """Returned by an action to report a named outcome instead of passed/failed."""
    name: str
    detail: str = ""


class OutcomeError(Exception):
    """Raised by an action (or a fixture it uses) to end with a named outcome."""

    def __init__(self, name: str, detail: str = ""):
        super().__init__(f"{name}: {detail}" if detail else name)
        self.outcome = Outcome(name, detail)


class Skip(OutcomeError):
    def __init__(self, detail: str = ""):
        super().__init__(SKIPPED, detail)


@dataclass
class ActionMark:
    name: str
    requires: dict[str, Any] = field(default_factory=dict)
    produces: dict[str, Any] = field(default_factory=dict)
    produces_on: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    doc: str = ""
    readonly: bool = False
    nolog: bool = False
    dry: Callable | None = None          # --dry-run: describes what the action would do


@dataclass
class FixtureMark:
    name: str
    scope: str = "step"


@dataclass
class ObserverMark:
    var: str


@dataclass
class HookMark:
    name: str


@dataclass
class StepKindMark:
    name: str
    keys: tuple[str, ...] = ()
    readonly: bool = False
    describe: Callable | None = None     # --dry-run: the command(s) a step would run, as lines


@dataclass
class ResolverMark:
    name: str


@dataclass
class ValidatorMark:
    name: str


@dataclass
class ValidatorKindMark:
    name: str
    keys: tuple[str, ...] = ()


# A validator's three answers about one `var=value` (validate.py)
HOLDS, DOES_NOT_HOLD, CANNOT_TELL = "holds", "does not hold", "cannot tell"


def action(name: str | None = None, *, requires: Mapping[str, Any] | None = None,
           produces: Mapping[str, Any] | None = None,
           produces_on: Mapping[str, Mapping[str, Any]] | None = None,
           outcomes: Iterable[str] = (), tags: Iterable[str] = (),
           readonly: bool = False, nolog: bool = False, doc: str | None = None,
           dry: Callable | None = None) -> Callable:
    """Export a function as an action.

    requires     state the action must start from: {var: value} or {var: [values]}
    produces     state it leaves behind when it PASSES: {var: value}, or a transition
                 map {var: {current: next}} (a current value not listed is kept)
    produces_on  state it leaves behind on another outcome: {outcome: {var: value}}
    outcomes     named outcomes it may return besides the built-in ones
    readonly     it only looks: it produces no state, and a run of read-only steps may
                 run beside a state-changing run in the same directory
    nolog        NOLOG: the output of the commands it runs is neither logged nor shown
    doc          the one-line description `--list` shows (default: the docstring's first line)
    dry          --dry-run: a function (fixtures by name, like the action) that DESCRIBES what the
                 action would do instead of doing it — returns lines (a list of str), an Outcome or
                 None. Without it a dry run does not run the action: it is marked "not run" and
                 assumed to end with its expected outcome
    """
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ActionMark(
            name=name or fn.__name__.replace("_", "-"),
            requires=dict(requires or {}), produces=dict(produces or {}),
            produces_on={k: dict(v) for k, v in (produces_on or {}).items()},
            outcomes=tuple(outcomes), tags=tuple(tags), readonly=readonly, nolog=nolog, dry=dry,
            doc=doc if doc is not None else
            ((fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "")))
        return fn
    return mark


def fixture(fn: Callable | None = None, *, name: str | None = None, scope: str = "step"):
    """Export a fixture: injected into actions (and other fixtures) by parameter name.

    A generator (sync or async) yields its value; the code after the yield is the
    teardown, run when the scope ends — also when the step failed.

    scope "look": set up once per LOOK — one act of judging a state variable: a derived
    observer asking the values in order, or the check after a step (the claimed value, the
    value before, the values the step could have moved it to). Every validator in that look
    gets the same value, so a probe can read the system once and judge every value from that
    one reading. Asked for outside a look (by an action, say), it is refused.
    """
    if scope not in ("run", "step", "look"):
        raise ValueError(f"fixture scope must be 'run', 'step' or 'look', not {scope!r}")

    def mark(f: Callable) -> Callable:
        setattr(f, MARK, FixtureMark(name=name or f.__name__, scope=scope))
        return f
    return mark(fn) if fn is not None else mark


def observer(var: str) -> Callable:
    """Export a function that reads the REAL value of a state variable. It returns one of the
    variable's values, or CANNOT_TELL when it did not read one: the variable then keeps the value it
    had (an unknown stays unknown). Never a guess: whatever it returns is stored as reality."""
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ObserverMark(var=var))
        return fn
    return mark


def hook(name: str) -> Callable:
    """Export a hook implementation; it receives only the keyword arguments it names."""
    if name not in HOOKS:
        raise ValueError(f"unknown hook {name!r}; known: {', '.join(sorted(HOOKS))}")

    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, HookMark(name=name))
        return fn
    return mark


def step_kind(name: str, *, keys: Iterable[str] = (), readonly: bool = False,
              describe: Callable | None = None) -> Callable:
    """Export an inner-step kind for walk actions (`{<name>: value, <keys>...}` in a walk).

    The function receives the step's RENDERED spec as `spec` (the kind's own value under
    `spec[name]`) plus any fixtures it names, and returns a proc Result (classified by the
    step's `outcomes:` evidence, then passed on exit 0), an Outcome, or None (passed).
    keys      the other keys the kind takes besides its own name (checked when walks load)
    readonly  its steps only LOOK (a guard): read-only unless a step says otherwise, so reality
              is not re-read after them and a run of them takes no writer lock; --dry-run RUNS them
    describe  --dry-run: a function (spec + fixtures by name, like the kind) that returns the
              command(s) the step would run, as lines, WITHOUT running anything
    """
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, StepKindMark(name=name, keys=tuple(keys), readonly=readonly, describe=describe))
        return fn
    return mark


def resolver(name: str) -> Callable:
    """Export a template resolver for walk actions: `{<name>}` or `{<name>:<arg>}` in a walk's
    values. The function receives `arg` (a string, "" when none is given) plus any fixtures
    it names, and returns the value. A string that is exactly one reference becomes the
    value itself (a list stays a list)."""
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ResolverMark(name=name))
        return fn
    return mark


def validator(name: str | None = None) -> Callable:
    """Export a Python validator, named in the runner config as `validate: {python: <name>}`
    (default: the function's name, `_` -> `-`). It receives `claim` (validate.Claim: var, value,
    role, step, outcome) plus any fixtures it names, and answers: True (holds), False (does not
    hold), None (cannot tell) — or a tuple (answer, why). An exception is "cannot tell"."""
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ValidatorMark(name=name or fn.__name__.replace("_", "-")))
        return fn
    return mark


def validator_kind(name: str, *, keys: Iterable[str] = ()) -> Callable:
    """Export a validator KIND (`validate: {<name>: value, <keys>...}`), the way an extension
    adds `playbook:`. The function receives the RENDERED spec as `spec`, `claim`, plus any
    fixtures it names, and returns (answer, why): answer is HOLDS, DOES_NOT_HOLD or CANNOT_TELL.
    It must only LOOK."""
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ValidatorKindMark(name=name, keys=tuple(keys)))
        return fn
    return mark
