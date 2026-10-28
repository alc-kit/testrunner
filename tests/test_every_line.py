"""Every line written out (WP testrunner-state-validation D8 c/d, build step B3; user 2026-10-28:
"explicit self-loops are required", "write out every line", "aborted is a separate transition
step, so it needs its own line"): in the `machine:` form, every outcome of an action that has a
transition from a value has a line. A self-loop is a claim of NO change, checked like any other,
and is declared once, in the machine (the action claims only changes)."""
import json

import pytest

from conftest import main

MOD = """
from pathlib import Path
from testrunner import action, validator

HERE = Path(__file__).parent

def knob(name):
    return (HERE / name).read_text().strip() if (HERE / name).exists() else ""

@validator()
def sys_is(claim):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    return s == claim.value, f"the system reads {s!r}"

@action(produces={"sys": {"absent": "up"}})
def make():
    (HERE / "system").write_text(knob("make-leaves") or "up")
    if knob("make-fails"):
        raise RuntimeError("make failed")

@action()
def peek():
    if knob("peek-changes"):
        (HERE / "system").write_text("up")          # a step that should change nothing, changing it
"""
COMPLETE = """
      [*]    --> absent
      absent --> up     : make [passed]
      absent --> absent : make [failed]
      absent --> absent : make [errored]
      absent --> absent : make [aborted]
      absent --> absent : make [skipped]
      absent --> absent : peek [passed]
      absent --> absent : peek [failed]
      absent --> absent : peek [errored]
      absent --> absent : peek [aborted]
      absent --> absent : peek [skipped]
"""


def make_sub(sub, machine=COMPLETE):
    body = "".join("      " + ln.strip() + "\n" for ln in machine.strip().splitlines())
    sub.write("testrunner.yml", f"""
modules: [m.py]
states:
  sys:
    values: [absent, up]
    validate: {{python: sys-is}}
    model_only: true
    machine: |
{body}""")
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def journal(sub):
    return [json.loads(x) for x in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def test_a_complete_machine_is_accepted(sub, capsys):
    code, out = run(make_sub(sub), capsys, "--list")
    assert code == 0, out
    assert "-> absent [aborted]" in out                 # the self-loops are in the table


@pytest.mark.parametrize("line", ["absent --> absent : make [aborted]", "absent --> absent : make [skipped]",
                                  "absent --> absent : peek [errored]"])
def test_a_missing_outcome_refuses_the_plan(sub, capsys, line):
    code, out = run(make_sub(sub, COMPLETE.replace(line + "\n", "")), capsys, "--list")
    on = line.split("[")[1].rstrip("]")
    by = line.split(": ")[1].split(" [")[0]
    assert code != 0 and "not every outcome is written out" in out
    assert f"absent --> ? : {by} [{on}]" in out


def test_a_self_loop_needs_no_claim_from_the_action(sub, capsys):
    # peek declares no produces at all; its self-loops are not "declared, NOT claimed"
    code, out = run(make_sub(sub), capsys, "--list")
    assert code == 0 and "UNDECIDED" not in out


def test_a_passed_self_loop_is_a_checked_claim(sub, capsys):
    s = make_sub(sub)
    code, out = run(s, capsys, "peek")
    assert code == 0
    chk = [e for e in journal(s) if e["event"] == "state_checked" and e["step"] == "peek"]
    assert chk and chk[0]["value"] == "absent"
    (s.root / "state" / "state.json").unlink()
    s.write("peek-changes", "1")                        # control: it changed what it claims it did not
    code, out = run(s, capsys, "peek")
    assert code == 1


def test_a_failure_self_loop_is_its_checked_end_result(sub, capsys):
    # make fails and leaves the system up: its declared end result (absent: nothing changed) is
    # NOT found, so the test fails and the value from before is kept (B1b)
    s = make_sub(sub)
    s.write("make-fails", "1")
    code, out = run(s, capsys, "make")
    assert code == 1 and "expected end result sys='absent' was NOT found" in out
    s.write("make-leaves", "absent")                    # control: it really changed nothing
    (s.root / "state" / "state.json").unlink()
    code, out = run(s, capsys, "make")
    assert code == 1 and "was NOT found" not in out    # failed as the action did, end result found


def test_the_yaml_form_keeps_the_closed_world(sub, capsys):
    # the migration window: YAML transitions need no line per outcome
    sub.write("testrunner.yml", """
modules: [m.py]
states:
  sys:
    initial: absent
    values: [absent, up]
    validate: {python: sys-is}
    model_only: true
    transitions:
      - {from: absent, to: up, by: make, on: passed}
""")
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    code, out = run(sub, capsys, "--list")
    assert code == 0, out
