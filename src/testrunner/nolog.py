"""NOLOG: values that must never reach a log, the terminal or the journal.

testrunner treats nothing as secret by itself. A value is secret when it is MARKED:
  in a run config     key: {NOLOG: value}        (YAML or TOML)
                      key: !NOLOG value          (YAML shorthand)
  at run time         run.nolog(value)           (e.g. a password an action generated)
Everything testrunner writes — step logs, the echo to the terminal, the journal, outcome
details, tracebacks — has every marked value replaced by MASK. What a command or an
action receives is the real value (TR_PARAMS, params, config): it needs it to work.

Two related switches, for output rather than values:
  an expect rule with `nolog: true`   the answer it types is not written to the log
  an action with nolog=True           its commands' output is neither logged nor shown
"""
from __future__ import annotations

from typing import Any

MASK = "********"
MARK_KEY = "NOLOG"


class NoLog:
    """A YAML `!NOLOG value` before it is unwrapped."""

    def __init__(self, value: Any):
        self.value = value


def unwrap(data: Any, path: str = "") -> tuple[Any, list[str]]:
    """Replace every {NOLOG: v} / NoLog(v) by v; return (data, the dotted paths marked)."""
    if isinstance(data, NoLog):
        return data.value, [path]
    if isinstance(data, dict):
        if set(data) == {MARK_KEY}:
            return data[MARK_KEY], [path]
        out, paths = {}, []
        for k, v in data.items():
            out[k], p = unwrap(v, f"{path}.{k}" if path else str(k))
            paths += p
        return out, paths
    if isinstance(data, list):
        out_l, paths = [], []
        for i, v in enumerate(data):
            item, p = unwrap(v, f"{path}[{i}]")
            out_l.append(item)
            paths += p
        return out_l, paths
    return data, []


def values_at(data: Any, paths: list[str]) -> list[str]:
    """The values at the marked paths (after unwrap), as strings."""
    out = []
    for path in paths:
        cur: Any = data
        for part in path.replace("[", ".[").split("."):
            if part.startswith("["):
                idx = int(part[1:-1])
                cur = cur[idx] if isinstance(cur, list) and idx < len(cur) else None
            else:
                cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None and cur != "":
            out.append(str(cur))
    return out


class Secrets:
    def __init__(self, values: list[str] | None = None):
        self._values: set[str] = set()
        for v in values or []:
            self.add(v)

    def add(self, value: Any) -> Any:
        s = str(value)
        if s:
            self._values.add(s)
        return value

    @property
    def values(self) -> list[str]:
        return sorted(self._values, key=len, reverse=True)   # longest first: no partial masks

    def text(self, s: str) -> str:
        for v in self.values:
            s = s.replace(v, MASK)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        return o

    def stream(self) -> "Redactor":
        return Redactor(self)


class Redactor:
    """Mask secrets in a byte stream whose chunks may split a secret: the last
    (longest secret - 1) bytes are held back until the next chunk or flush()."""

    def __init__(self, secrets: Secrets):
        self.secrets = secrets
        self.pending = b""

    def feed(self, chunk: bytes) -> bytes:
        values = [v.encode() for v in self.secrets.values]
        data = self._mask(self.pending + chunk, values)
        # hold back the longest tail that could be the START of a secret, nothing more
        hold = 0
        for k in range(min(len(data), max((len(v) for v in values), default=1) - 1), 0, -1):
            tail = data[-k:]
            if any(v.startswith(tail) for v in values):
                hold = k
                break
        self.pending = data[len(data) - hold:] if hold else b""
        return data[:len(data) - hold] if hold else data

    def flush(self) -> bytes:
        values = [v.encode() for v in self.secrets.values]
        out, self.pending = self._mask(self.pending, values), b""
        return out

    @staticmethod
    def _mask(data: bytes, values: list[bytes]) -> bytes:
        for v in values:
            data = data.replace(v, MASK.encode())
        return data
