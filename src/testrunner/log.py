"""testrunner's own log: what the runner DECIDED, and why — for whoever debugs a run.

The terminal shows what a run DID (banners, the children's output, outcomes). This log
shows the reasoning in between: which config was chosen and how, why a step was skipped
(`when:` key by key), which state it required and found, what each outcome produced,
which reaction the walker took, every fixture set up, every command started (argv, cwd,
the environment it got on top of the runner's own, exit code, duration) and the output
of the commands that are NOT echoed — the probes whose answers decide what happens next.

Two sinks, both fed by Python's standard `logging`:

  the trace file   <state_dir>/logs/testrunner-<run id>.log, ALWAYS at DEBUG, for every
                   invocation that runs or plans something, plus testrunner-last.log
                   pointing at the newest. A run that failed is debugged from its trace,
                   not by running it again. The newest KEEP traces are kept.
  the terminal     (stderr) off by default; -v shows INFO, -vv DEBUG
                   (or --log-level LEVEL, or $TESTRUNNER_LOG_LEVEL).

Subscribers log the same way — `log = logging.getLogger(__name__)` in an action module —
and their lines land in the same trace, tagged with the step that wrote them. NOLOG
values are masked in both sinks like everywhere else.

Levels: INFO = a decision or a milestone (one line each); DEBUG = the detail behind it;
WARNING/ERROR = something an operator should read even when the run passed.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from .nolog import Secrets

KEEP = 50                       # trace files kept per state directory
ENV_LEVEL = "TESTRUNNER_LOG_LEVEL"
OUTPUT_HEAD, OUTPUT_TAIL = 40, 160   # lines of a quiet command's output put in the trace

# The step a log line belongs to: set by the runner around each step, inherited by every
# task the step starts (contextvars follow asyncio tasks), so a fixture or an ext helper
# deep down still says which step it served.
STEP: contextvars.ContextVar[str] = contextvars.ContextVar("testrunner_step", default="-")

# Subscriber modules are imported as testrunner_subscriber_<hash>.<file> (discovery.py);
# the hash keeps two checkouts apart but says nothing to a reader.
_SUBSCRIBER = re.compile(r"^testrunner_subscriber_[0-9a-f]+\.")

log = logging.getLogger(__name__)


def level_of(text: str | int | None) -> int | None:
    if text is None or text == "":
        return None
    if isinstance(text, int):
        return text
    value = logging.getLevelName(str(text).upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown log level {text!r} (want DEBUG, INFO, WARNING or ERROR)")
    return value


class _Format(logging.Formatter):
    def __init__(self, holder: "Logs", stamp: bool):
        super().__init__()
        self.holder, self.stamp = holder, stamp

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        name = _SUBSCRIBER.sub("", record.name)
        step = getattr(record, "step", "-")
        if self.stamp:
            t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            # file:line — every line in the trace points at the code that wrote it
            line = (f"{t}.{int(record.msecs):03d}Z {record.levelname:<7} [{step}] {name} "
                    f"({record.filename}:{record.lineno}): {msg}")
        else:
            line = f"[{record.levelname.lower()} {step} {name}] {msg}"
        # masked at FORMAT time: a line buffered before the run's secrets were known is
        # still masked when it is written
        return self.holder.secrets.text(line)


class _StepTag(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.step = STEP.get()
        return True


class _Buffer(logging.Handler):
    """Holds records until the trace file can be opened (the state directory is known
    only once the runner config is read)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) < 10000:
            self.records.append(record)


class Logs:
    """One per invocation: installs the handlers on the root logger, removes them in close()."""

    def __init__(self, terminal_level: int | None = None, stream: TextIO | None = None):
        self.secrets = Secrets()
        self.file: Path | None = None
        self.root = logging.getLogger()
        self._saved_level = self.root.level
        self.root.setLevel(logging.DEBUG)
        self._tag = _StepTag()
        self.handlers: list[logging.Handler] = []
        self.buffer: _Buffer | None = _Buffer()
        self._add(self.buffer)
        if terminal_level is not None:
            term = logging.StreamHandler(stream or sys.stderr)
            term.setLevel(terminal_level)
            term.setFormatter(_Format(self, stamp=False))
            self._add(term)
        # chatty libraries stay out unless they have something to say
        for noisy in ("asyncio",):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    def _add(self, h: logging.Handler) -> None:
        h.addFilter(self._tag)
        self.root.addHandler(h)
        self.handlers.append(h)

    def open_file(self, log_dir: Path, run_id: str, dry: bool = False) -> Path:
        """Start the trace file; what was logged before it is written first. A --dry-run's trace
        is testrunner-dry-<id>.log and never becomes testrunner-last.log (it is not a run)."""
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"testrunner-{'dry-' if dry else ''}{run_id}.log"
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_Format(self, stamp=True))
        fh.addFilter(self._tag)
        if self.buffer is not None:
            for record in self.buffer.records:
                fh.handle(record)
            self.root.removeHandler(self.buffer)
            self.handlers.remove(self.buffer)
            self.buffer = None
        self.root.addHandler(fh)
        self.handlers.append(fh)
        self.file = path
        if dry:
            self._prune(log_dir)
            return path
        last = log_dir / "testrunner-last.log"
        try:
            if last.is_symlink() or last.exists():
                last.unlink()
            last.symlink_to(path.name)
        except OSError as e:
            log.debug("could not point %s at the new trace: %s", last, e)
        self._prune(log_dir)
        return path

    @staticmethod
    def _prune(log_dir: Path) -> None:
        traces = sorted(log_dir.glob("testrunner-*.log"), key=lambda p: p.stat().st_mtime)
        traces = [p for p in traces if not p.is_symlink()]
        for old in traces[:-KEEP]:
            try:
                old.unlink()
            except OSError:
                pass

    def close(self) -> None:
        for h in self.handlers:
            self.root.removeHandler(h)
            h.close()
        self.handlers.clear()
        self.root.setLevel(self._saved_level)


def output_excerpt(text: str, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL) -> str:
    """A command's output for the trace: whole when short, else its head and its tail
    (the end is where a failure explains itself)."""
    lines = text.replace("\r", "").rstrip("\n").splitlines()
    if len(lines) <= head + tail:
        return "\n".join(f"    | {ln}" for ln in lines)
    cut = len(lines) - head - tail
    return "\n".join([*(f"    | {ln}" for ln in lines[:head]),
                      f"    | ... {cut} line(s) not shown ...",
                      *(f"    | {ln}" for ln in lines[-tail:])])


# variables testrunner sets on every child for colour; not part of what a command MEANS
_COLOUR_VARS = {"TERM", "FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS", "NO_COLOR"}


def command_line(argv: list[str], cwd: Any, env: dict[str, Any] | None) -> str:
    """One copy-pasteable shell line that reruns a command as testrunner ran it: its
    directory, the environment it got ON TOP of the runner's own (unset ones as
    `env -u`), and its argv — everything quoted for a POSIX shell."""
    unset, assign = [], []
    for k in sorted(env or {}):
        if k in _COLOUR_VARS:
            continue
        v = env[k]
        if v is None:
            if k in os.environ:
                unset += ["-u", k]
        elif os.environ.get(k) != str(v):
            assign.append(f"{k}={shlex.quote(str(v))}")
    prefix = (["env", *unset] if unset else []) + assign
    return f"cd {shlex.quote(str(cwd))} && " + " ".join([*prefix, *map(shlex.quote, argv)])


def env_delta(env: dict[str, Any] | None) -> str:
    """The variables a command gets ON TOP of the runner's own environment (or unset)."""
    if not env:
        return "{}"
    parts = []
    for k in sorted(env):
        v = env[k]
        if v is None:
            parts.append(f"{k}=<unset>")
        elif os.environ.get(k) != str(v):
            parts.append(f"{k}={v}")
    return "{" + ", ".join(parts) + "}"
