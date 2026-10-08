"""The pty process runner: colour forwarding, expect rules, prompts, timeouts."""
import asyncio
import io

import pytest

from testrunner.proc import Proc
from testrunner.term import Stripper, strip
from testrunner.ui import InputBroker

COLOURED_PROMPT = r"printf '\033[1mType \033[31mYES\033[0;1m to go:\033[0m '; read -r a; echo \"got=$a\""


class BinEcho(io.TextIOWrapper):
    """A text stream with a .buffer, like sys.stdout."""

    def __init__(self):
        super().__init__(io.BytesIO(), encoding="utf-8")

    def raw(self) -> bytes:
        self.flush()
        return self.buffer.getvalue()


def run(coro):
    return asyncio.run(coro)


def test_isatty_inside_and_colour_bytes_forwarded_untouched(tmp_path):
    echo = BinEcho()
    p = Proc(tmp_path, tmp_path, echo=echo, color=True)
    r = run(p.run(r"[ -t 0 ] && [ -t 1 ] && printf '\033[32mgreen\033[0m é\n'", log="x.log"))
    assert r.ok
    assert b"\x1b[32mgreen\x1b[0m \xc3\xa9" in echo.raw()             # raw bytes, utf-8 intact
    assert b"\x1b[32m" in (tmp_path / "x.log").read_bytes()           # the log keeps colour
    assert "green é" in r.text and "\x1b" not in r.text               # assertions get plain text


def test_colour_never_strips_the_echo(tmp_path):
    echo = BinEcho()
    r = run(Proc(tmp_path, tmp_path, echo=echo, color=False).run(r"printf '\033[32mgreen\033[0m\n'"))
    assert r.ok and b"\x1b" not in echo.raw() and b"green" in echo.raw()


def test_rule_matches_a_coloured_prompt(tmp_path):
    rules = [{"expect": "Type YES to go", "send": "YES\r", "delay": 0.1}]
    r = run(Proc(tmp_path, tmp_path).run(COLOURED_PROMPT, rules=rules, prompt_idle=5))
    assert r.ok and "got=YES" in r.text and r.answered == ["Type YES to go"]


def test_without_stripping_the_rule_would_not_match():
    # negative control for the test above: the raw bytes really do split the phrase
    raw = b"\x1b[1mType \x1b[31mYES\x1b[0;1m to go:\x1b[0m "
    assert b"Type YES to go" not in raw and b"Type YES to go" in strip(raw)


def test_rule_fires_once_per_occurrence(tmp_path):
    script = "for i in 1 2 3; do printf 'ok? '; read -r a; echo \"a$i=$a\"; done"
    r = run(Proc(tmp_path, tmp_path).run(script, rules=[{"expect": r"ok\? ", "send": "y\r", "delay": 0.05}],
                                         prompt_idle=5))
    assert r.ok and len(r.answered) == 3 and "a3=y" in r.text


def test_unanswered_prompt_fails_without_an_operator(tmp_path):
    r = run(Proc(tmp_path, tmp_path).run("printf 'Password: '; read -r a", prompt_idle=0.4, timeout=20))
    assert not r.ok and r.unanswered == "Password:"


def test_unanswered_prompt_goes_to_the_operator(tmp_path):
    async def go():
        ui = InputBroker(interactive=True, out=io.StringIO())
        ui._questions = asyncio.Queue()   # attach() would read the real stdin
        task = asyncio.ensure_future(Proc(tmp_path, tmp_path, ui=ui).run(
            "printf 'Name: '; read -r a; echo \"hello $a\"", prompt_idle=0.3, timeout=20))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if ui._pending or (ui._questions and not ui._questions.empty()):
                break
        ui.feed_line("world")
        return await task
    r = run(go())
    assert r.ok and "hello world" in r.text


def test_timeout_kills_the_process_group(tmp_path):
    r = run(Proc(tmp_path, tmp_path).run("sleep 30 & sleep 30", timeout=0.5))
    assert r.timed_out and not r.ok


def test_exit_code_is_reported(tmp_path):
    r = run(Proc(tmp_path, tmp_path).run("exit 3"))
    assert r.returncode == 3 and not r.ok


@pytest.mark.parametrize("cut", range(1, 12))
def test_stripper_handles_a_sequence_split_across_chunks(cut):
    data = b"ab\x1b[1;31mcd\x1b[0mef"
    s = Stripper()
    assert s.feed(data[:cut]) + s.feed(data[cut:]) + s.flush() == b"abcdef"
