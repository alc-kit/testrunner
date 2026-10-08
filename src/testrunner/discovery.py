"""Collect what a subscriber exports: actions, fixtures, observers, hooks.

Python exports are found pytest-style — import the modules the runner config names and
pick up every object carrying a testrunner mark. Shell exports come from the runner
config's `actions:` table: a command plus the same contract a decorator would declare.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .api import (BUILTIN_OUTCOMES, MARK, ActionMark, FixtureMark, HookMark, ObserverMark)
from .config import ConfigError, RunnerConfig


@dataclass
class ActionSpec:
    name: str
    kind: str                       # "python" | "shell"
    requires: dict[str, Any]
    produces: dict[str, Any]
    produces_on: dict[str, dict[str, Any]]
    outcomes: tuple[str, ...]
    tags: tuple[str, ...] = ()
    doc: str = ""
    readonly: bool = False
    nolog: bool = False
    fn: Callable | None = None      # python
    shell: dict | None = None       # shell: {run, env, cwd, rules, exit_codes, timeout}
    source: str = ""

    @property
    def all_outcomes(self) -> frozenset[str]:
        return BUILTIN_OUTCOMES | set(self.outcomes)


@dataclass
class FixtureSpec:
    name: str
    scope: str
    fn: Callable
    source: str = ""


@dataclass
class Registry:
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    fixtures: dict[str, FixtureSpec] = field(default_factory=dict)
    observers: dict[str, Callable] = field(default_factory=dict)
    hooks: dict[str, list[Callable]] = field(default_factory=dict)


def _module_files(entry: Path) -> list[Path]:
    if entry.is_file():
        return [entry]
    if entry.is_dir():
        return sorted(p for p in entry.rglob("*.py")
                      if not any(part.startswith((".", "__")) for part in p.relative_to(entry).parts))
    raise ConfigError(f"module path {entry} does not exist")


def _import_plain(path: Path):
    """A module in a pythonpath directory: imported by its REAL name, so a neighbour that
    does `import <name>` gets this same module object — not a second copy."""
    return importlib.import_module(path.stem)


def _import(path: Path):
    # Named after the absolute path: two subscribers (or two checkouts of one) that both
    # have actions/main.py must not get each other's module from sys.modules.
    full = path.resolve()
    tag = hashlib.sha1(str(full).encode()).hexdigest()[:10]
    modname = f"testrunner_subscriber_{tag}.{full.stem.replace('-', '_')}"
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    # Subscriber modules may import their neighbours (helpers next to the actions).
    parent = str(path.parent.resolve())
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[modname]
        raise
    return module


def collect(rc: RunnerConfig) -> Registry:
    # Directories the subscriber's modules import FROM (a library next to them, or one
    # fetched elsewhere): first on sys.path, in the order given.
    for p in reversed(rc.pythonpath):
        if not p.is_dir():
            raise ConfigError(f"pythonpath entry {p} is not a directory")
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    reg = Registry()
    seen: set[int] = set()
    for entry in rc.modules:
        for f in _module_files(entry):
            plain = f.parent.resolve() in {p.resolve() for p in rc.pythonpath}
            module = _import_plain(f) if plain else _import(f)
            for obj in vars(module).values():
                mark = getattr(obj, MARK, None)
                if mark is None or id(obj) in seen:
                    continue
                seen.add(id(obj))
                where = f"{f.relative_to(rc.root) if f.is_relative_to(rc.root) else f}:{getattr(obj, '__name__', '?')}"
                _register(reg, obj, mark, where)
    for name, spec in rc.shell_actions.items():
        _register_shell(reg, name, spec, rc)
    validate_registry(reg, rc)
    return reg


def _register(reg: Registry, obj: Callable, mark: Any, where: str) -> None:
    if isinstance(mark, ActionMark):
        if mark.name in reg.actions:
            raise ConfigError(f"action {mark.name!r} exported twice: {reg.actions[mark.name].source} and {where}")
        reg.actions[mark.name] = ActionSpec(
            name=mark.name, kind="python", requires=mark.requires, produces=mark.produces,
            produces_on=mark.produces_on, outcomes=mark.outcomes, tags=mark.tags,
            doc=mark.doc, readonly=mark.readonly, nolog=mark.nolog, fn=obj, source=where)
    elif isinstance(mark, FixtureMark):
        if mark.name in reg.fixtures:
            raise ConfigError(f"fixture {mark.name!r} exported twice: {reg.fixtures[mark.name].source} and {where}")
        reg.fixtures[mark.name] = FixtureSpec(mark.name, mark.scope, obj, where)
    elif isinstance(mark, ObserverMark):
        if mark.var in reg.observers:
            raise ConfigError(f"two observers for state {mark.var!r}")
        reg.observers[mark.var] = obj
    elif isinstance(mark, HookMark):
        reg.hooks.setdefault(mark.name, []).append(obj)


SHELL_KEYS = {"run", "env", "cwd", "rules", "exit_codes", "timeout", "requires", "produces",
              "produces_on", "outcomes", "tags", "doc", "readonly", "nolog"}


def _register_shell(reg: Registry, name: str, spec: dict, rc: RunnerConfig) -> None:
    where = f"{rc.file.name}:actions.{name}"
    if not isinstance(spec, dict) or "run" not in spec:
        raise ConfigError(f"{where}: a shell action needs `run:` (a list of argv words or a string)")
    unknown = set(spec) - SHELL_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if name in reg.actions:
        raise ConfigError(f"action {name!r} exported twice: {reg.actions[name].source} and {where}")
    exit_codes = {int(k): v for k, v in (spec.get("exit_codes") or {}).items()}
    outcomes = tuple(spec.get("outcomes") or ()) + tuple(
        v for v in exit_codes.values() if v not in BUILTIN_OUTCOMES and v not in (spec.get("outcomes") or ()))
    reg.actions[name] = ActionSpec(
        name=name, kind="shell", requires=dict(spec.get("requires") or {}),
        produces=dict(spec.get("produces") or {}),
        produces_on={k: dict(v) for k, v in (spec.get("produces_on") or {}).items()},
        outcomes=outcomes, tags=tuple(spec.get("tags") or ()), doc=spec.get("doc", ""),
        readonly=bool(spec.get("readonly", False)), nolog=bool(spec.get("nolog", False)),
        shell={**spec, "exit_codes": exit_codes}, source=where)


def _check_values(rc: RunnerConfig, where: str, mapping: dict, allow_list: bool,
                  allow_map: bool = False) -> None:
    for var, val in mapping.items():
        if var not in rc.states:
            raise ConfigError(f"{where}: unknown state variable {var!r} (declared: {', '.join(rc.states) or 'none'})")
        if allow_map and isinstance(val, dict):     # a transition map {current: next}
            vals = [*val.keys(), *val.values()]
        else:
            vals = val if (allow_list and isinstance(val, list)) else [val]
        for v in vals:
            if v not in rc.states[var]["values"]:
                raise ConfigError(f"{where}: {var}={v!r} is not one of {rc.states[var]['values']}")


def validate_registry(reg: Registry, rc: RunnerConfig) -> None:
    for a in reg.actions.values():
        if a.readonly and (a.produces or a.produces_on):
            raise ConfigError(f"{a.source}: a readonly action cannot produce state")
        _check_values(rc, f"{a.source} requires", a.requires, allow_list=True)
        _check_values(rc, f"{a.source} produces", a.produces, allow_list=False, allow_map=True)
        for outcome, mapping in a.produces_on.items():
            if outcome not in a.all_outcomes:
                raise ConfigError(f"{a.source}: produces_on names outcome {outcome!r}, which it does not declare")
            _check_values(rc, f"{a.source} produces_on.{outcome}", mapping, allow_list=False, allow_map=True)
    for var in reg.observers:
        if var not in rc.states:
            raise ConfigError(f"observer for unknown state variable {var!r}")
