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


def action(name: str | None = None, *, requires: Mapping[str, Any] | None = None,
           produces: Mapping[str, Any] | None = None,
           produces_on: Mapping[str, Mapping[str, Any]] | None = None,
           outcomes: Iterable[str] = (), tags: Iterable[str] = (),
           readonly: bool = False) -> Callable:
    """Export a function as an action.

    requires     state the action must start from: {var: value} or {var: [values]}
    produces     state it leaves behind when it PASSES
    produces_on  state it leaves behind on another outcome: {outcome: {var: value}}
    outcomes     named outcomes it may return besides the built-in ones
    readonly     it only looks: it produces no state, and a run of read-only steps may
                 run beside a state-changing run in the same directory
    """
    def mark(fn: Callable) -> Callable:
        setattr(fn, MARK, ActionMark(
            name=name or fn.__name__.replace("_", "-"),
            requires=dict(requires or {}), produces=dict(produces or {}),
            produces_on={k: dict(v) for k, v in (produces_on or {}).items()},
            outcomes=tuple(outcomes), tags=tuple(tags), readonly=readonly,
            doc=(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""))
        return fn
    return mark


def fixture(fn: Callable | None = None, *, name: str | None = None, scope: str = "step"):
    """Export a fixture: injected into actions (and other fixtures) by parameter name.

    A generator (sync or async) yields its value; the code after the yield is the
    teardown, run when the scope ends — also when the step failed.
    """
    if scope not in ("run", "step"):
        raise ValueError(f"fixture scope must be 'run' or 'step', not {scope!r}")

    def mark(f: Callable) -> Callable:
        setattr(f, MARK, FixtureMark(name=name or f.__name__, scope=scope))
        return f
    return mark(fn) if fn is not None else mark


def observer(var: str) -> Callable:
    """Export a function that reads the REAL value of a state variable."""
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
