"""An inner step that FAILED is not its walk's claim, seen early (found on the lab rig 2026-10-29,
run 10c2e15d: `install/install` failed at its last assertion with PVE already installed; the
observer read pve=installed, the enclosing walk `install` declares none -> installed ON PASSED,
and that line was taken as "the walk's claim, seen early" — the value was applied after a
failure, against the failure decision (WP testrunner-state-validation §8.7.8, B1b).

An observed change the enclosing walk declares is that walk's claim seen early only when the
inner step PASSED: the walk can still go on to pass. After a failed inner step the walk ends
failed, and its failure line (the expected end result) is what is judged."""
import json

RUNNER = """
modules: [m.py]
action_files: [walks.yml]
states:
  sys:
    values: [absent, up]
    validate: {python: sys-is}
    observe: {order: [up, absent]}
    machine: |
      [*]    --> absent
      absent --> up     : bring [passed]
      %% the failure's expected end result: nothing changed
      absent --> absent : bring [failed]
      absent --> absent : bring [errored]
      absent --> absent : bring [aborted]
      absent --> absent : bring [skipped]
"""
MOD = """
from pathlib import Path
from testrunner import validator

HERE = Path(__file__).parent

@validator()
def sys_is(claim):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    return s == claim.value, f"the system reads {s!r}"
"""
WALKS = """
actions:
  bring:
    produces: {sys: {absent: up}}
    steps:
      - {id: make, run: "echo up > @ROOT@/system; @EXIT@"}
      - {id: after, run: "touch @ROOT@/after"}
"""
PLAN = "plan:\n  path: [bring]\n"


def make_sub(sub, exit_code):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)
    sub.write("walks.yml", WALKS.replace("@ROOT@", str(sub.root)).replace("@EXIT@", f"exit {exit_code}"))
    sub.write("configs/default.yml", PLAN)
    return sub


def journal(sub):
    return [json.loads(line) for line in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def ev(sub, name):
    return [e for e in journal(sub) if e["event"] == name]


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def test_a_failed_inner_step_is_not_the_walks_claim_seen_early(sub, capsys):
    # the inner step changed the system (up) and failed: the walk's failure line says absent,
    # which is not found — the test fails, the value from before is kept, the reading recorded
    s = make_sub(sub, exit_code=1)
    code, out = s.run(capsys=capsys)
    assert code == 1, out
    early = [e for e in ev(s, "state_changed") if "seen early" in str(e.get("note")) or
             "inside the walk" in str(e.get("note"))]
    assert not early, early                             # THE defect: applied after a failure
    assert stored(s)["sys"] == "absent"                 # the value from before
    assert "expected end result sys='absent' was NOT found" in out
    seen = ev(s, "observed_after_failure")
    assert seen and seen[-1]["observed"] == {"sys": "up"}   # what the rig showed: recorded, not applied
    assert not (s.root / "after").exists()


def test_control_a_passed_inner_step_is_the_walks_claim_seen_early(sub, capsys):
    s = make_sub(sub, exit_code=0)
    code, out = s.run(capsys=capsys)
    assert code == 0, out
    early = [e for e in ev(s, "state_changed") if e["step"] == "bring/make"]
    assert early and early[0]["note"] == "observed inside the walk bring, which declares it"
    assert stored(s)["sys"] == "up" and (s.root / "after").exists()
    assert not ev(s, "observed_after_failure")


BROKEN_RUNNER = RUNNER.replace("values: [absent, up]", "values: [absent, up, broken]") \
    .replace("observe: {order: [up, absent]}", "observe: {order: [up, broken, absent]}") \
    .replace("      absent --> absent : bring [failed]\n", "      absent --> broken : bring [failed]\n")


def test_a_passed_inner_step_never_shows_the_walks_failure_line_early(sub, capsys):
    # the walk declares `absent --> broken` for FAILED; an inner step that PASSED and left broken
    # is not that line seen early (the walk has not failed): it is an undeclared change, and fails
    s = make_sub(sub, exit_code=0)
    s.write("testrunner.yml", BROKEN_RUNNER)
    walks = WALKS.replace("    steps:\n", "    produces_on: {failed: {sys: {absent: broken}}}\n    steps:\n")
    s.write("walks.yml", walks.replace("@ROOT@", str(s.root)).replace("echo up", "echo broken")
            .replace("@EXIT@", "exit 0"))
    code, out = s.run(capsys=capsys)
    assert code == 1, out
    assert not [e for e in ev(s, "state_changed") if "inside the walk" in str(e.get("note"))]
    assert "changed sys without declaring it" in out
    assert not (s.root / "after").exists()
