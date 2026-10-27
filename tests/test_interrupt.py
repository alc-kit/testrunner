"""An interrupted run is never a pass (found 2026-10-27 stopping a gate3 run with SIGINT: the
journal said `run_end passed: true`, the step that was running had no `step_end`, and the
`finally` that writes run_end also judged release_when — an interrupt on a state that matches it
would release the scenario over a half-built rig). Now every open step ends ABORTED, innermost
first; the run ends failed, "interrupted during <step>"; the scenario stays. Each rule with its
control: the same run, not interrupted, passes and releases."""
import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import REPO
from testrunner.config import load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler
from testrunner.runner import Runner
from testrunner.store import Store
from testrunner.ui import InputBroker

# release_when holds from the start (sys stays absent), so a release would happen if judged
RUNNER = """
modules: [mod.py]
action_files: [walks.yml]
states:
  sys: {values: [absent, up], initial: absent}
scenario: {release_when: {sys: absent}}
"""
MOD = """
import asyncio, os
from pathlib import Path
from testrunner import action

HERE = Path(__file__).parent

@action()
async def slow():
    (HERE / "slow-started").write_text("x")
    await asyncio.sleep(0 if os.environ.get("FAST") else 30)

@action()
def after():
    (HERE / "after-ran").write_text("x")
"""
WALKS = """
actions:
  outer:
    steps:
      - {id: first, action: after}
      - {id: inner, action: slow}
"""


def make(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("walks.yml", WALKS)
    sub.write("configs/default.yml", "params: {}\n")
    return sub


def journal(sub):
    return [json.loads(ln) for ln in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def wait_for(path, seconds=20):
    end = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > end:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def run_in_process(sub, steps, cancel_after: float | None):
    rc = load_runner_config(sub.root / "testrunner.yml")
    reg = collect(rc)
    prog = Compiler(rc, reg).compile(None, steps)
    ui = InputBroker(interactive=False, out=io.StringIO())

    async def go():
        runner = Runner(rc, reg, load_run_config(rc), Store(rc.state_dir), ui, echo=io.StringIO(), color="never")
        task = asyncio.ensure_future(runner.run(prog))
        if cancel_after is not None:
            while not (sub.root / "slow-started").exists():
                await asyncio.sleep(0.02)
            await asyncio.sleep(cancel_after)
            task.cancel()
        return await task
    return asyncio.run(go())


def test_a_cancelled_run_ends_failed_with_every_open_step_aborted(sub):
    s = make(sub)
    with pytest.raises(asyncio.CancelledError):
        run_in_process(s, ["outer"], cancel_after=0.1)
    ev = journal(s)
    ends = [(e["step"], e["outcome"]) for e in ev if e["event"] == "step_end"]
    # first passed; then the inner step and the walk around it, innermost first
    assert ends == [("outer/first", "passed"), ("outer/inner", "aborted"), ("outer", "aborted")]
    end = next(e for e in ev if e["event"] == "run_end")
    assert end["passed"] is False and end["why"] == "interrupted during outer/inner"


def test_control_the_same_run_not_interrupted_passes(sub, monkeypatch):
    monkeypatch.setenv("FAST", "1")
    s = make(sub)
    assert run_in_process(s, ["outer"], cancel_after=None).passed
    assert next(e for e in journal(s) if e["event"] == "run_end")["passed"] is True


def start(s, fast=False):
    """The CLI, as an operator runs it: it claims the scenario, which a stopped run must keep."""
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), **({"FAST": "1"} if fast else {})}
    return subprocess.Popen([sys.executable, str(REPO / "bin" / "testrunner"), "--root", str(s.root), "--color",
                             "never", "--non-interactive", "slow", "after"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def test_control_not_interrupted_the_cli_releases_the_scenario(sub):
    s = make(sub)
    out, _ = start(s, fast=True).communicate(timeout=60)
    assert "scenario released" in out, out
    assert [e for e in journal(s) if e["event"] == "scenario_released"]


def test_sigint_exits_130_and_the_journal_says_interrupted(sub):
    s = make(sub)
    p = start(s)
    try:
        wait_for(s.root / "slow-started")
        time.sleep(0.2)
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out
    assert "interrupted during slow: the run ends failed; the scenario is not released" in out
    ev = journal(s)
    assert [(e["step"], e["outcome"]) for e in ev if e["event"] == "step_end"] == [("slow", "aborted")]
    end = next(e for e in ev if e["event"] == "run_end")
    assert end["passed"] is False and "interrupted during slow" in end["why"]
    assert not (s.root / "after-ran").exists()
    # release_when {sys: absent} holds, and still the stopped run keeps the scenario
    assert not [e for e in ev if e["event"] == "scenario_released"]
    assert (s.root / "state" / "scenario.json").exists()


def test_the_report_of_an_interrupted_run_says_failed(sub):
    from testrunner.report import render_markdown, runs_from_journal
    s = make(sub)
    with pytest.raises(asyncio.CancelledError):
        run_in_process(s, ["outer"], cancel_after=0.1)
    md = render_markdown(runs_from_journal(Store(s.root / "state")))
    assert "❌ FAILED" in md and "interrupted during outer/inner" in md
