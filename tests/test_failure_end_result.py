"""A failure's expected end result is validated (WP testrunner-state-validation §8.7.8, amended;
build step B1b; user 2026-10-28):

    "we might need to run a validator even after a failure, because that could in fact put a step
    in to an error state, that the next step can use to bring the walk forward. We only have to
    fail the test, if the validator does NOT find the expected end result after a failed run."
    "In fact we can EXPECT a failure to happen in the rig, and devise a deviation of the plan to
    repair the rig before continuing."

- found: the variable moves to the failure's declared end result, and the plan's rules route the
  step (a deviation that repairs the rig);
- not found: the test fails here, whatever the rules say; the value from before is kept, and what
  the observers can attest is recorded (observed_after_failure), never applied;
- no end result declared: the value from before is kept, the reading recorded, rules route as before."""
import json

RUNNER = """
modules: [m.py]
states:
  sys:
    initial: absent
    values: [absent, up, broken]
    validate: {python: sys-is}
    observe: {order: [up, broken, absent]}
    transitions:
      - {from: absent, to: up,     by: make,   on: passed}
@FAILURE_LINE@
      - {from: broken, to: up,     by: repair, on: passed}
"""
FAILURE_LINE = "      - {from: absent, to: broken, by: make,   on: failed}   # the failure's expected end result"
MOD = """
from pathlib import Path
from testrunner import action, validator

HERE = Path(__file__).parent

def knob(name):
    return (HERE / name).read_text().strip() if (HERE / name).exists() else ""

def leave(value):
    (HERE / "system").write_text(value)

@validator()
def sys_is(claim):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    return s == claim.value, f"the system reads {s!r}"

@action(produces={"sys": {"absent": "up"}}@PRODUCES_ON@)
def make():
    leave(knob("make-leaves") or "up")
    if knob("make-fails"):
        raise RuntimeError("make failed")

@action(requires={"sys": "broken"}, produces={"sys": {"broken": "up"}})
def repair():
    leave("up")
    (HERE / "repaired").touch()

@action(requires={"sys": "up"})
def use():
    (HERE / "used").touch()

@action()
def shrug():
    (HERE / "shrugged").touch()

@action()
def finish():
    (HERE / "finished").touch()
"""
PRODUCES_ON = ', produces_on={"failed": {"sys": {"absent": "broken"}}}'
# the plan EXPECTS make may fail into `broken`, and deviates to repair before going on
PLAN = """plan:
  path: [make, use]
  on:
    make:
      - when: {outcome: passed}
        then: continue
      - when: {outcome: failed, state: {sys: broken}}
        then: [repair]
      - when: {outcome: failed}
        then: [shrug]
"""


# with no end result declared, a failure leaves sys where it was: this plan routes any failure to
# shrug, then finishes (a plan that reached `use` from there would be refused by the static check)
PLAN_ROUTED = """plan:
  path: [make, finish]
  on:
    make:
      - when: {outcome: passed}
        then: continue
      - when: {outcome: failed}
        then: [shrug]
"""


def make_sub(sub, declared=True, plan=PLAN):
    sub.write("testrunner.yml", RUNNER.replace("@FAILURE_LINE@", FAILURE_LINE if declared else ""))
    sub.write("m.py", MOD.replace("@PRODUCES_ON@", PRODUCES_ON if declared else ""))
    sub.write("configs/default.yml", plan)
    return sub


def journal(sub):
    return [json.loads(line) for line in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def ev(sub, name):
    return [e for e in journal(sub) if e["event"] == name]


def test_found_the_plan_deviates_to_repair_and_goes_on(sub, capsys):
    s = make_sub(sub)
    s.write("make-fails", "1")
    s.write("make-leaves", "broken")                    # the failure the plan expects
    code, out = s.run(capsys=capsys)
    assert code == 0, out                               # the failure WAS the plan: the run passes
    assert (s.root / "repaired").exists() and (s.root / "used").exists()
    ch = [e for e in ev(s, "state_changed") if e["step"] == "make"]
    assert ch and ch[0]["to"] == "broken" and ch[0]["note"] == "the failure's declared end result"
    assert stored(s)["sys"] == "up"
    ends = {e["step"]: e["outcome"] for e in ev(s, "step_end")}
    assert ends["make"] == "failed"                     # the event happened, and is reported


def test_not_found_the_test_fails_whatever_the_rules_say(sub, capsys):
    s = make_sub(sub)
    s.write("make-fails", "1")
    s.write("make-leaves", "smoke")                     # not the declared end result, not any value
    code, out = s.run(capsys=capsys)
    assert code == 1
    assert "expected end result sys='broken' was NOT found" in out
    assert not (s.root / "repaired").exists() and not (s.root / "used").exists()
    assert not (s.root / "shrugged").exists()           # the catch-all rule WOULD route it: not found stops anyway
    assert stored(s)["sys"] == "absent"                 # the value from before
    kept = [e for e in ev(s, "state_kept") if e["step"] == "make"]
    assert kept and kept[0]["verdicts"][0]["answer"] == "does not hold"
    seen = ev(s, "observed_after_failure")
    assert seen and seen[0]["step"] == "make"           # what the rig showed, recorded ...
    assert seen[0]["observed"] == {"sys": None}         # ... (no value holds), never applied


def test_no_end_result_declared_keeps_the_value_and_records_the_reading(sub, capsys):
    # (a failure that changed nothing: the bench still reads absent. When it does NOT — the kept
    # value contradicts the bench — the NEXT step's observer reads the difference and that step is
    # failed for it; see test_a_contradiction_left_by_a_failure_is_blamed_on_the_next_step.)
    s = make_sub(sub, declared=False, plan=PLAN_ROUTED)
    s.write("make-fails", "1")
    s.write("make-leaves", "absent")
    code, out = s.run(capsys=capsys)
    assert (s.root / "shrugged").exists() and (s.root / "finished").exists()   # the rules route as before
    assert stored(s)["sys"] == "absent"
    assert ev(s, "state_kept") and ev(s, "observed_after_failure")[0]["observed"] == {"sys": "absent"}
    assert "was NOT found" not in out                   # nothing was declared to look for


def test_a_contradiction_left_by_a_failure_is_blamed_on_the_next_step(sub, capsys):
    # MEASURED behaviour, recorded as an OPEN design question (2026-10-28), not a decision: with no
    # end result declared, make fails and leaves `broken`; sys is kept `absent`; the rules route to
    # shrug, which passes, and the observer after shrug reads `broken` and fails shrug for it.
    s = make_sub(sub, declared=False, plan=PLAN_ROUTED)
    s.write("make-fails", "1")
    s.write("make-leaves", "broken")
    code, out = s.run(capsys=capsys)
    assert code == 1 and (s.root / "shrugged").exists() and not (s.root / "finished").exists()
    ends = {e["step"]: e["outcome"] for e in ev(s, "step_end")}
    assert ends["shrug"] == "failed"
    assert stored(s)["sys"] == "broken"


def test_control_a_passed_step_is_judged_as_always(sub, capsys):
    s = make_sub(sub)
    code, out = s.run(capsys=capsys)
    assert code == 0 and stored(s)["sys"] == "up"
    assert not ev(s, "observed_after_failure") and not ev(s, "state_kept")
    assert not (s.root / "repaired").exists() and (s.root / "used").exists()
