"""Redo (WP rig-extension Part B step 3): `--from STEP` starts the compiled walk (plan, edits
and all) at STEP; `--resume` starts it at the step the last run stopped at. One-run edits from
the command line (`--skip`, `--repeat`, `--insert`, `--retry`) mirror `edit:` and are notes,
never errors, when a name is not on the walk."""
import json

import pytest

from conftest import main

RUNNER = """
modules: [mod.py]
states:
  s: {values: [a], initial: a}
paths:
  full: [one, two, breaker, four]
default_path: full
"""
MOD = """
from pathlib import Path
from testrunner import action

HERE = Path(__file__).parent
for _n in ("one", "two", "three", "four"):
    exec(f"@action(name={_n!r})\\ndef _{_n}(): pass")

@action()
def breaker():
    n = int((HERE / "count").read_text()) + 1 if (HERE / "count").exists() else 1
    (HERE / "count").write_text(str(n))
    if (HERE / "break").exists() and n <= int((HERE / "break").read_text() or 99):
        raise RuntimeError("broken")

@action(readonly=True)
def gate():
    raise RuntimeError("the probe says no")
"""


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [full]\n")
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def walk(out):
    return [ln.split()[1] for ln in out.splitlines() if ln.strip()[:1].isdigit()]


def ran(sub):
    events = [json.loads(ln) for ln in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]
    last = max(i for i, e in enumerate(events) if e["event"] == "run_start")
    return [e["step"] for e in events[last:] if e["event"] == "step_end"]


# ── --from ───────────────────────────────────────────────────────────────────

def test_from_starts_the_plans_walk_there(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "--plan", "--from", "breaker")
    assert code == 0 and walk(out) == ["breaker", "four"] and "; from breaker" in out


def test_from_a_step_not_on_the_walk_is_refused(subscriber, capsys):
    code, _, err = run(subscriber, capsys, "--plan", "--from", "nope")
    assert code == 2 and "--from nope: not a step on the walk (on it: one, two, breaker, four)" in err


def test_from_applies_after_the_edits(subscriber, capsys):
    subscriber.write("configs/default.yml", "plan: [full]\nedit:\n  - {repeat: one, after: four}\n")
    code, out, _ = run(subscriber, capsys, "--plan", "--from", "four")
    assert code == 0 and walk(out) == ["four", "one#2"]


def test_from_drops_on_and_expect_of_steps_left_out_with_a_note(subscriber, capsys):
    subscriber.write("configs/default.yml", "plan:\n  path: [full]\n  expect: {one: {outcome: passed}}\n"
                                            "  on: {four: {failed: {goto: one}}}\n")
    code, out, _ = run(subscriber, capsys, "--plan", "--from", "two")
    assert code == 0 and "plan.expect.one dropped: not in this run" in out
    assert "plan.on.four.failed (goto 'one') dropped" in out


def test_control_without_from_the_same_config_keeps_them(subscriber, capsys):
    subscriber.write("configs/default.yml", "plan:\n  path: [full]\n  expect: {one: {outcome: passed}}\n")
    code, out, _ = run(subscriber, capsys, "--plan")
    assert code == 0 and "dropped" not in out


# ── --resume ─────────────────────────────────────────────────────────────────

def test_resume_starts_where_the_last_run_stopped(subscriber, capsys):
    subscriber.write("break", "")
    assert run(subscriber, capsys)[0] == 1
    assert ran(subscriber) == ["one", "two", "breaker"]
    (subscriber.root / "break").unlink()
    code, out, _ = run(subscriber, capsys, "--resume")
    assert code == 0 and "resuming at breaker" in out and ran(subscriber) == ["breaker", "four"]


def test_resume_after_a_passed_run_is_refused(subscriber, capsys):
    assert run(subscriber, capsys)[0] == 0
    code, _, err = run(subscriber, capsys, "--resume")
    assert code == 2 and "passed; there is nothing to resume" in err


def test_resume_without_any_run_is_refused(subscriber, capsys):
    code, _, err = run(subscriber, capsys, "--resume")
    assert code == 2 and "--resume: no runs recorded" in err


def test_resume_at_a_step_no_longer_on_the_walk_is_refused(subscriber, capsys):
    subscriber.write("break", "")
    assert run(subscriber, capsys)[0] == 1
    assert run(subscriber, capsys, "--release")[0] == 0     # the scenario's snapshot would win
    subscriber.write("configs/default.yml", "plan: [full]\nedit:\n  - {skip: breaker}\n")
    code, _, err = run(subscriber, capsys, "--resume")
    assert code == 2 and "--from breaker: not a step on the walk" in err


def test_resume_ignores_retry_probes(subscriber, capsys):
    subscriber.write("break", "")
    subscriber.write("configs/default.yml", "plan: [full]\nedit:\n  - retry: breaker\n    if: {probe: gate}\n")
    assert run(subscriber, capsys)[0] == 1
    assert ran(subscriber)[-1] == "breaker~probe1"          # the last thing that ran was the probe
    (subscriber.root / "break").unlink()
    code, out, _ = run(subscriber, capsys, "--resume")
    assert code == 0 and "resuming at breaker" in out


def test_from_and_resume_exclude_each_other(subscriber, capsys):
    with pytest.raises(SystemExit):
        run(subscriber, capsys, "--from", "two", "--resume")


# ── one-run edits from the command line ─────────────────────────────────────

def test_cli_edits(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "--plan", "--skip", "two", "--repeat", "one@after:four",
                       "--insert", "three@before:four", "--retry", "breaker=2:5")
    assert code == 0 and walk(out) == ["one", "breaker", "three", "four", "one#2"]
    assert "edits: -two, +one#2 (again one) after four, +three before four, retry breaker x2" in out


def test_cli_retry_retries(subscriber, capsys):
    subscriber.write("break", "1")                          # fails once
    code, _, _ = run(subscriber, capsys, "--retry", "breaker=1")
    assert code == 0 and ran(subscriber) == ["one", "two", "breaker", "breaker", "four"]


def test_cli_edits_naming_nothing_are_notes(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "--plan", "--skip", "nope", "--repeat", "one@after:nope")
    assert code == 0 and walk(out) == ["one", "two", "breaker", "four"]
    assert "--edit[0] (skip step 'nope') dropped: not in this run" in out
    assert "--edit[1] (repeat anchor 'nope') dropped" in out


@pytest.mark.parametrize("args,msg", [
    (["--repeat", "one"], "--repeat 'one': want NAME@after:ID or NAME@before:ID"),
    (["--insert", "three@beside:four"], "--insert 'three@beside:four': want NAME@after:ID"),
    (["--retry", "breaker"], "--retry 'breaker': want STEP=TIMES or STEP=TIMES:DELAY"),
    (["--retry", "breaker=two"], "--retry 'breaker=two': want STEP=TIMES"),
])
def test_bad_cli_edits_are_refused(subscriber, capsys, args, msg):
    code, _, err = run(subscriber, capsys, "--plan", *args)
    assert code == 2 and msg in err
