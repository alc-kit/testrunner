"""Fixtures, the pytest way: a callable's parameters are filled by NAME.

A name resolves, in order, to a value the current scope provides (the built-ins: step,
params, proc, state, ... and run, config, store, ui), then to an exported fixture. An
exported fixture is set up once per scope ("run" or "step"), may itself take fixtures,
and may be a generator (sync or async): the code after its `yield` is the teardown, run
in reverse order when the scope closes — also after a failure.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Any, Callable

from .discovery import FixtureSpec


class FixtureError(Exception):
    pass


class Scope:
    def __init__(self, name: str, values: dict[str, Any], parent: "Scope | None" = None):
        self.name, self.values, self.parent = name, dict(values), parent
        self.stack = contextlib.AsyncExitStack()

    def find(self, key: str) -> tuple[bool, Any]:
        s: Scope | None = self
        while s is not None:
            if key in s.values:
                return True, s.values[key]
            s = s.parent
        return False, None

    async def close(self) -> None:
        await self.stack.aclose()


class Fixtures:
    def __init__(self, specs: dict[str, FixtureSpec], reserved: set[str]):
        clash = set(specs) & reserved
        if clash:
            raise FixtureError(f"fixture(s) {', '.join(sorted(clash))} shadow a built-in; rename them")
        self.specs = specs

    async def kwargs_for(self, fn: Callable, scope: Scope, chain: tuple[str, ...] = ()) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for pname, p in inspect.signature(fn).parameters.items():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            found, value = scope.find(pname)
            if not found:
                if pname in self.specs:
                    value = await self._get(pname, scope, chain)
                elif p.default is not p.empty:
                    continue
                else:
                    raise FixtureError(f"{getattr(fn, '__name__', fn)}: no fixture named {pname!r}")
            out[pname] = value
        return out

    async def _get(self, name: str, scope: Scope, chain: tuple[str, ...]) -> Any:
        if name in chain:
            raise FixtureError(f"fixture cycle: {' -> '.join((*chain, name))}")
        spec = self.specs[name]
        target = scope
        while target.name != spec.scope:
            if target.parent is None:
                raise FixtureError(f"fixture {name!r} has scope {spec.scope!r}, which is not open here")
            target = target.parent
        found, value = target.find(name)
        if found:
            return value
        # A run-scoped fixture is set up in the RUN scope, so it cannot see step values.
        kwargs = await self.kwargs_for(spec.fn, target, (*chain, name))
        value = await self._setup(spec.fn, kwargs, target)
        target.values[name] = value
        return value

    @staticmethod
    async def _setup(fn: Callable, kwargs: dict, scope: Scope) -> Any:
        if inspect.isasyncgenfunction(fn):
            return await scope.stack.enter_async_context(contextlib.asynccontextmanager(fn)(**kwargs))
        if inspect.isgeneratorfunction(fn):
            return scope.stack.enter_context(contextlib.contextmanager(fn)(**kwargs))
        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return await asyncio.to_thread(fn, **kwargs)


async def call(fn: Callable, kwargs: dict) -> Any:
    """Call an action/hook/observer: async directly, sync in a thread (it may block)."""
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return await asyncio.to_thread(fn, **kwargs)


def accepted(fn: Callable, available: dict[str, Any]) -> dict[str, Any]:
    """The subset of `available` a hook names (hooks take what they want, pluggy-style)."""
    params = inspect.signature(fn).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return dict(available)
    return {k: v for k, v in available.items() if k in params}
