"""Run a command on a pseudo-terminal, asynchronously, answering its prompts.

Why a pty: interactive gates (ansible's `pause`, installers, `read -p`) behave
differently — or refuse — without a terminal, and some put the terminal in raw mode and
FLUSH input buffered before the prompt. So answers are sent only AFTER the prompt is
seen, with a delay, and end in a carriage return (raw mode takes CR, not LF, as Enter).

Expect rules: [{expect: <regex>, send: <text>, delay: <s>}]. Each rule fires every time
its pattern appears in NEW output; rules are scanned in order, first match wins per
read. A prompt no rule answers (output stops mid-line for `prompt_idle` seconds, on a
line that ends like a prompt — `prompt_pattern`) goes to the operator through the input
broker — or, without one, fails the command.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import struct
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from .nolog import MASK, Secrets
from .term import Stripper, child_env, strip, window_size
from .ui import InputBroker, NotInteractive

DEFAULT_DELAY = 1.5
DEFAULT_PROMPT_IDLE = 20.0
# What a stalled partial line must END with to count as a prompt. Without it, a progress
# line that goes quiet ("Waiting for the domain to get an IP address...") would be taken
# for a question — and, with nobody attached, its command killed.
DEFAULT_PROMPT_PATTERN = r"[:?>\]#$]\s*$"


@dataclass
class Rule:
    expect: re.Pattern
    send: str
    delay: float = DEFAULT_DELAY
    nolog: bool = False             # the answer is a secret: never written to the log

    @classmethod
    def make(cls, spec: dict) -> "Rule":
        unknown = set(spec) - {"expect", "send", "delay", "nolog"}
        if unknown or "expect" not in spec or "send" not in spec:
            raise ValueError("expect rule: want expect, send and optionally delay, nolog "
                             f"(got keys {sorted(spec)})")
        return cls(re.compile(spec["expect"].encode() if isinstance(spec["expect"], str) else spec["expect"]),
                   spec["send"], float(spec.get("delay", DEFAULT_DELAY)), bool(spec.get("nolog", False)))


def load_rules(rules: Any) -> list[Rule]:
    """A list of rule dicts, or a path to a JSON/YAML file holding one."""
    if rules is None:
        return []
    if isinstance(rules, (str, Path)):
        p = Path(rules)
        text = p.read_text()
        if p.suffix in (".yml", ".yaml"):
            import yaml
            rules = yaml.safe_load(text)
        else:
            rules = json.loads(text)
    return [Rule.make(r) for r in rules]


@dataclass
class Result:
    argv: list[str]
    returncode: int
    output: bytes = b""                                 # raw: colours and all
    answered: list[str] = field(default_factory=list)   # the patterns that fired, in order
    timed_out: bool = False
    unanswered: str | None = None                       # the prompt nobody answered

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.unanswered is None

    @property
    def text(self) -> str:
        """The output without escape sequences — what to assert on."""
        return strip(self.output).decode(errors="replace")


def _child_setup() -> None:   # runs in the child between fork and exec
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Proc:
    """The `proc` fixture: one per step, logging into the step's log directory."""

    def __init__(self, cwd: Path, log_dir: Path, ui: InputBroker | None = None,
                 echo: TextIO | None = None, env: dict[str, str] | None = None,
                 prompt_idle: float = DEFAULT_PROMPT_IDLE, color: bool = True,
                 prompt_pattern: str = DEFAULT_PROMPT_PATTERN, secrets: Secrets | None = None,
                 nolog: bool = False):
        self.cwd, self.log_dir, self.ui, self.echo = Path(cwd), Path(log_dir), ui, echo
        self.env = env or {}
        self.color = color
        self.prompt_re = re.compile(prompt_pattern)
        self.secrets = secrets or Secrets()
        self.nolog = nolog          # NOLOG action: no output to the log or the terminal
        self.prompt_idle = prompt_idle

    async def run(self, argv: list[str] | str, *, cwd: Path | None = None,
                  env: dict[str, str] | None = None, log: str | None = None,
                  rules: Any = None, timeout: float | None = None,
                  prompt_idle: float | None = None, echo: bool = True, nolog: bool | None = None,
                  on_output: Callable[[bytes], None] | None = None) -> Result:
        if isinstance(argv, str):
            argv = ["bash", "-c", argv]
        argv = [str(a) for a in argv]
        compiled = load_rules(rules)
        quiet = self.nolog if nolog is None else nolog
        for r in compiled:
            if r.nolog:
                self.secrets.add(r.send.rstrip("\r\n"))
        idle_limit = self.prompt_idle if prompt_idle is None else prompt_idle
        full_env = {**os.environ, **child_env(self.color), **self.env, **(env or {})}
        master, slave = os.openpty()
        rows, cols = window_size(self.echo) if self.echo is not None else (50, 200)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        logf = None
        if log:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            logf = (self.log_dir / log).open("ab")
            if quiet:
                logf.write(b"[testrunner: output not logged (NOLOG)]\n")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd or self.cwd), env=full_env,
                stdin=slave, stdout=slave, stderr=slave, preexec_fn=_child_setup)
        except BaseException:
            os.close(master)
            os.close(slave)
            if logf:
                logf.close()
            raise
        os.close(slave)
        loop = asyncio.get_running_loop()
        out = bytearray()          # raw bytes, forwarded and logged as they came
        plain = bytearray()        # the same without escape sequences: rules and prompts match here
        stripper = Stripper()
        scanned = 0                # offset into `plain`
        answered: list[str] = []
        eof = asyncio.Event()
        last_output = time.monotonic()
        sends: list[asyncio.Task] = []
        unanswered: str | None = None
        echo_to = self.echo if (echo and self.echo is not None and not quiet) else None
        if quiet:
            logf_out = None
        else:
            logf_out = logf
        redact_log, redact_echo = self.secrets.stream(), self.secrets.stream()
        # bytes, not text: a decode would split multi-byte characters across chunks
        echo_bin = getattr(echo_to, "buffer", None) if echo_to is not None else None

        def readable() -> None:
            nonlocal scanned, last_output
            try:
                chunk = os.read(master, 65536)
            except OSError:      # EIO: the child side closed
                chunk = b""
            if not chunk:
                loop.remove_reader(master)
                eof.set()
                return
            last_output = time.monotonic()
            out.extend(chunk)
            clean = stripper.feed(chunk)
            plain.extend(clean)
            if logf_out:
                logf_out.write(redact_log.feed(chunk))
                logf_out.flush()
            if echo_to is not None:
                shown = redact_echo.feed(chunk if self.color else clean)
                if echo_bin is not None:
                    echo_to.flush()
                    echo_bin.write(shown)
                    echo_bin.flush()
                else:
                    echo_to.write(shown.decode(errors="replace"))
                    echo_to.flush()
            if on_output:
                on_output(chunk)
            # scan the new region; first matching rule wins, then continue after its match
            while compiled:
                hits = [(m.start(), m.end(), r) for r in compiled
                        if (m := r.expect.search(plain, scanned))]
                if not hits:
                    break
                _, end, rule = min(hits, key=lambda h: h[0])
                scanned = end
                answered.append(rule.expect.pattern.decode(errors="replace"))
                sends.append(asyncio.ensure_future(_send(rule.send, rule.delay)))

        async def _send(text: str, delay: float) -> None:
            await asyncio.sleep(delay)
            with contextlib.suppress(OSError):
                os.write(master, text.encode())
            if logf_out:
                shown = MASK if text.rstrip("\r\n") in self.secrets.values else self.secrets.text(text)
                logf_out.write(f"\n[testrunner: sent {shown!r}]\n".encode())

        async def watch_prompts() -> None:
            nonlocal unanswered, scanned
            while not eof.is_set():
                await asyncio.sleep(min(1.0, idle_limit / 4))
                if time.monotonic() - last_output < idle_limit or any(not t.done() for t in sends):
                    continue
                line_start = plain.rfind(b"\n") + 1
                if line_start < scanned:
                    continue   # this partial line was already answered
                tail = bytes(plain[line_start:]).decode(errors="replace").strip()
                if not tail or not self.prompt_re.search(tail):
                    continue
                if self.ui is None or not self.ui.interactive:
                    unanswered = tail
                    _kill(proc)
                    return
                try:
                    answer = await self.ui.ask(f"[{argv[0]}] {tail}")
                except NotInteractive:
                    unanswered = tail
                    _kill(proc)
                    return
                scanned = len(plain)
                with contextlib.suppress(OSError):
                    os.write(master, (answer + "\r").encode())

        loop.add_reader(master, readable)
        watcher = asyncio.ensure_future(watch_prompts())
        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await _stop(proc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(eof.wait(), 5)
        except asyncio.CancelledError:
            await _stop(proc)
            raise
        finally:
            watcher.cancel()
            for t in sends:
                t.cancel()
            with contextlib.suppress(Exception):
                loop.remove_reader(master)
            os.close(master)
            if logf_out:
                logf_out.write(redact_log.flush())
            if echo_to is not None:
                tail_bytes = redact_echo.flush()
                if tail_bytes:
                    if echo_bin is not None:
                        echo_bin.write(tail_bytes)
                        echo_bin.flush()
                    else:
                        echo_to.write(tail_bytes.decode(errors="replace"))
                        echo_to.flush()
            if logf:
                logf.close()
        return Result(argv, proc.returncode if proc.returncode is not None else -1, bytes(out),
                      answered, timed_out, unanswered)


def _kill(proc: asyncio.subprocess.Process) -> None:
    """The whole session: a playbook's ssh children must not outlive it."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)



async def _stop(proc: asyncio.subprocess.Process, grace: float = 10) -> None:
    _kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
