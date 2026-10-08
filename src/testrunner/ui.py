"""The input broker: the one owner of the operator's terminal.

While steps run, a line the operator types is either the answer to a pending question
(`await ui.ask(...)`, or a child prompt no expect rule answered) or a command:
  status      what is running, and since when
  abort       stop the run after cancelling the current step (the system is left as is)
  help
Without a terminal (CI, a pipe) the broker is not interactive: `ask()` raises, and a
child prompt nobody answers fails its step instead of hanging.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Callable, TextIO


class NotInteractive(Exception):
    pass


class InputBroker:
    def __init__(self, interactive: bool | None = None, out: TextIO | None = None):
        self.out = out or sys.stderr
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.commands: dict[str, Callable[[str], None]] = {"help": self._help}
        self._questions: asyncio.Queue[tuple[str, asyncio.Future]] | None = None
        self._pending: tuple[str, asyncio.Future] | None = None
        self._reader_fd: int | None = None

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def command(self, name: str, fn: Callable[[str], None]) -> None:
        self.commands[name] = fn

    def _help(self, _: str) -> None:
        self.say("commands: " + ", ".join(sorted(self.commands)))

    # ── wiring to a real terminal ──
    def attach(self) -> None:
        """Start reading stdin lines in the running loop (only when interactive)."""
        self._questions = asyncio.Queue()
        if not self.interactive:
            return
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        buf = bytearray()

        def readable() -> None:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                loop.remove_reader(fd)
                self._reader_fd = None
                return
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf[:] = rest
                self.feed_line(line.decode(errors="replace"))

        loop.add_reader(fd, readable)
        self._reader_fd = fd

    def detach(self) -> None:
        if self._reader_fd is not None:
            asyncio.get_running_loop().remove_reader(self._reader_fd)
            self._reader_fd = None

    # ── input ──
    def feed_line(self, line: str) -> None:
        """One line from the operator (the terminal reader calls this; tests do too)."""
        if self._pending is None and self._questions is not None and not self._questions.empty():
            self._pending = self._questions.get_nowait()
        if self._pending is not None:
            _, fut = self._pending
            self._pending = None
            if not fut.done():
                fut.set_result(line)
            self._prompt_next()
            return
        word, _, rest = line.strip().partition(" ")
        if not word:
            return
        fn = self.commands.get(word)
        if fn is None:
            self.say(f"unknown command {word!r} — " + ", ".join(sorted(self.commands)))
        else:
            fn(rest)

    def _prompt_next(self) -> None:
        if self._pending is None and self._questions is not None and not self._questions.empty():
            self._pending = self._questions.get_nowait()
            self.say(f"? {self._pending[0]}")

    async def ask(self, question: str, choices: list[str] | None = None) -> str:
        if not self.interactive:
            raise NotInteractive(f"needs an answer, but no operator is attached: {question}")
        if self._questions is None:
            raise RuntimeError("InputBroker.attach() was not called")
        prompt = f"{question} [{'/'.join(choices)}]" if choices else question
        while True:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._questions.put_nowait((prompt, fut))
            self._prompt_next()
            answer = (await fut).strip()
            if not choices or answer in choices:
                return answer
            self.say(f"answer one of: {', '.join(choices)}")
