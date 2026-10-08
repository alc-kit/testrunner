"""The store: every persistent byte a run writes goes through here, into the SUBSCRIBER's
state directory. testrunner itself keeps nothing between runs.

  journal   append-only JSON lines — what happened, in order (`--status` reads it back)
  state     the current values of the state variables, rewritten after every step
  kv(ns)    small documents for anything else a subscriber wants to keep
  scenario  the run config a directory is committed to: the first run selects it, every
            later or parallel runner in the directory adheres to it until it is released
  lock()    the WRITER lock: two runs that change state cannot drive one system at once
            (read-only runs take no writer lock and may run beside one)
  path(..)  where logs and artefacts go

Writes are atomic (temp file + rename): a run killed mid-write leaves the old document,
never half of a new one. The journal is the exception — appends of one line each — and
a torn last line is skipped on read.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LockedError(Exception):
    pass


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class KV:
    """A namespace of JSON documents: store.kv("rig").put("lock", {...})."""

    def __init__(self, root: Path):
        self.root = root

    def _file(self, key: str) -> Path:
        if not key or "/" in key or key.startswith("."):
            raise ValueError(f"bad key {key!r}")
        return self.root / f"{key}.json"

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return json.loads(self._file(key).read_text())
        except FileNotFoundError:
            return default

    def put(self, key: str, value: Any) -> None:
        atomic_write(self._file(key), json.dumps(value, indent=2, sort_keys=True) + "\n")

    def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._file(key).unlink()

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.is_dir() else []


class Journal:
    def __init__(self, file: Path):
        self.file = file

    def append(self, event: str, **fields: Any) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # `at` is for people (second resolution); `ts` (epoch seconds) is for arithmetic
        line = json.dumps({"at": utcnow(), "ts": round(time.time(), 3), "event": event, **fields},
                          sort_keys=True)
        with self.file.open("a") as f:
            f.write(line + "\n")

    def read(self) -> Iterator[dict]:
        try:
            with self.file.open() as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue    # a torn last line from a killed run
        except FileNotFoundError:
            return


class Store:
    SCENARIO = "scenario.json"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.journal = Journal(self.root / "journal.jsonl")
        self._state = KV(self.root)
        self._lock_fd: int | None = None

    # state variables
    def get_state(self) -> dict[str, Any]:
        return self._state.get("state", {}) or {}

    def set_state(self, state: dict[str, Any]) -> None:
        self._state.put("state", state)

    # the scenario
    def get_scenario(self) -> dict | None:
        try:
            return json.loads((self.root / self.SCENARIO).read_text())
        except FileNotFoundError:
            return None

    def claim_scenario(self, record: dict) -> tuple[bool, dict]:
        """Select the scenario unless one is selected already: (claimed, the scenario).

        Race-free between parallel runners: the record is written in full to a temp file
        and then hard-linked into place, which fails if the name exists — so exactly one
        runner wins, and nobody ever reads half a record."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".scenario.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f, indent=2, sort_keys=True)
                f.write("\n")
            try:
                os.link(tmp, self.root / self.SCENARIO)
                return True, record
            except FileExistsError:
                return False, self.get_scenario() or record
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def release_scenario(self) -> dict | None:
        old = self.get_scenario()
        with contextlib.suppress(FileNotFoundError):
            (self.root / self.SCENARIO).unlink()
        return old

    def kv(self, namespace: str) -> KV:
        if not namespace or "/" in namespace or namespace.startswith("."):
            raise ValueError(f"bad namespace {namespace!r}")
        return KV(self.root / "kv" / namespace)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.read(fd, 200).decode(errors="replace").strip()
                raise LockedError(f"another run holds {self.root}/.lock ({holder or 'unknown holder'})") from None
            os.ftruncate(fd, 0)
            os.write(fd, f"pid {os.getpid()} since {utcnow()}\n".encode())
            yield
        finally:
            os.close(fd)   # releases the flock
