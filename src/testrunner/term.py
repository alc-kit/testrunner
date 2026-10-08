"""Terminal handling: colour forwarding, ANSI stripping, the runner's own styling.

Colour is FORWARDED, not reproduced: a child runs on a pty, believes it has a terminal
and colours its output itself; the runner passes those bytes through untouched. Only
matching (expect rules, prompt detection) works on a stripped copy, so a prompt that is
coloured mid-word still matches its rule.

Two separate decisions:
  the CHILDREN's colour   forwarded raw whenever it is not switched off — `--color never`
                          or NO_COLOR. Whether the runner's own stdout is a terminal does
                          NOT matter: a run redirected to a file (an unattended run under a
                          watcher) keeps its colour; read it with `less -R`.
                          `--color always` also sets FORCE_COLOR / CLICOLOR_FORCE /
                          PY_COLORS for tools that colour only when told to.
  the runner's OWN lines  coloured under `always`, or under `auto` on a terminal.
Under `never` the children are asked not to colour (NO_COLOR) and what they print anyway
is stripped. Log files keep exactly what was forwarded.
"""
from __future__ import annotations

import fcntl
import os
import re
import struct
import termios
from typing import TextIO

# CSI (colours, cursor), OSC (titles, hyperlinks; BEL or ST terminated), other ESC pairs.
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_HOLD_MAX = 512   # an unterminated escape longer than this is not held back any longer


def strip(data: bytes) -> bytes:
    return ANSI.sub(b"", data)


class Stripper:
    """Strip escape sequences from a stream whose chunks may split a sequence."""

    def __init__(self) -> None:
        self.pending = b""

    def feed(self, chunk: bytes) -> bytes:
        data = self.pending + chunk
        self.pending = b""
        esc = data.rfind(b"\x1b")
        if esc != -1 and len(data) - esc < _HOLD_MAX and not ANSI.match(data, esc):
            data, self.pending = data[:esc], data[esc:]
        return strip(data)

    def flush(self) -> bytes:
        data, self.pending = self.pending, b""
        return strip(data)


def color_enabled(mode: str, stream: TextIO) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def window_size(stream: TextIO, default: tuple[int, int] = (50, 200)) -> tuple[int, int]:
    """(rows, cols) of the operator's terminal, so children wrap and draw like it."""
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols:
            return rows, cols
    except (OSError, ValueError, AttributeError):
        pass
    return default


def forward_colour(mode: str) -> bool:
    """Do the children's colours reach the operator (and the log)?"""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return not os.environ.get("NO_COLOR")


def child_env(color: bool, force: bool = False) -> dict[str, str]:
    """Make sure a child that asks "can I colour?" hears yes (or no, under `never`)."""
    env: dict[str, str] = {}
    if color:
        if not os.environ.get("TERM") or os.environ.get("TERM") == "dumb":
            env["TERM"] = "xterm-256color"
        if force:
            env.update({"FORCE_COLOR": "1", "CLICOLOR_FORCE": "1", "PY_COLORS": "1"})
    else:
        env["NO_COLOR"] = "1"
    return env


class Style:
    """The runner's own banners — the same palette run.sh used."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _w(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def banner(self, text: str) -> str:
        return self._w("1;36", text)

    def ok(self, text: str) -> str:
        return self._w("1;32", text)

    def bad(self, text: str) -> str:
        return self._w("1;31", text)

    def warn(self, text: str) -> str:
        return self._w("1;33", text)

    def dim(self, text: str) -> str:
        return self._w("2", text)
