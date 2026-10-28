"""An observer that LOOKED and finds the system in none of the variable's values answers NONE_HOLDS,
and the variable becomes UNKNOWN (user 2026-10-28: "add the UNKNOWN observer answer for 'no value
holds'").

Measured on the lab rig (dry run, WP testrunner-state-validation §8.2): with corosync stopped on one
node, a derived observer found no value holding, raised the same error as "cannot look", and the run
kept the stored `clustered` — a rig in none of its states would have started a run as clustered.
"No value holds" is a reading; "cannot tell" is not. They must differ."""
import json

SUB = """
modules: [m.py]
states:
  sys:
    values: [absent, up, built]
    initial: absent
    validate: {python: probe}
    observe: {order: [built, up, absent]}
    transitions:
      - {from: absent, to: up,    by: make,  on: passed}
scenario: {release_when: {sys: absent}}
"""
MOD = """
from pathlib import Path
from testrunner import CANNOT_TELL, action, validator

HERE = Path(__file__).parent

@validator("probe")
def probe(claim):
    if (HERE / "blind").exists():
        return None, "the system cannot be reached"          # cannot tell
    seen = (HERE / "system").read_text() if (HERE / "system").exists() else "absent"
    return seen == claim.value, f"system reads {seen!r}"

@action(produces={"sys": {"absent": "up"}})
def make():
    (HERE / "system").write_text("up")

@action(requires={"sys": ["up", "built"]})
def use():
    (HERE / "used").touch()

@action(requires={"sys": ["up", "built"]})
def wreck():
    (HERE / "system").write_text("wrecked")      # in none of the values; the step claims nothing
"""


def make(sub, mod=MOD):
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", mod)
    sub.write("configs/default.yml", "params: {}\n")


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def journal(sub):
    f = sub.root / "state" / "journal.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def test_none_holds_before_the_plan_makes_the_variable_unknown(sub, capsys):
    make(sub)
    sub.run("make", capsys=capsys)
    assert stored(sub)["sys"] == "up"
    (sub.root / "system").write_text("wrecked")           # the system is in none of the values
    code, out = sub.run("use", capsys=capsys)
    assert code != 0
    assert "no value of sys holds" in out and "sys is UNKNOWN" in out
    assert not (sub.root / "used").exists()               # a step that requires sys does not run
    # ... and it says WHY, value by value (the operator sees "a route to LAN B", not only "unknown")
    assert out.count("system reads 'wrecked'") >= 3, out


def test_control_cannot_tell_keeps_the_stored_value(sub, capsys):
    # the observer could not LOOK: the stored value stays, and the step runs on it
    make(sub)
    sub.run("make", capsys=capsys)
    (sub.root / "blind").touch()
    code, out = sub.run("use", capsys=capsys)
    assert "observer for sys cannot tell" in out and "sys stays" in out
    assert "no value of sys holds" not in out
    assert (sub.root / "used").exists()


def test_none_holds_after_a_step_fails_it_and_journals_unknown(sub, capsys):
    make(sub)
    code, out = sub.run("make", "wreck", capsys=capsys)
    assert code == 1
    assert stored(sub)["sys"] is None
    unknown = [e for e in journal(sub) if e["event"] == "state_unknown" and e.get("observed") == "no value holds"]
    assert unknown and unknown[-1]["was"] == "up", journal(sub)
    assert "finds no value of sys holding" in out
    assert "finds no value of sys holding (the model said 'up': sys=built (observe): does not hold — " in out, out
    assert "system reads 'wrecked'" in unknown[-1]["why"]       # the journal keeps the reason too
    end = [e for e in journal(sub) if e["event"] == "run_end"][-1]
    assert not end["passed"] and end["state"]["sys"] is None


def test_a_hand_written_observer_can_answer_none_holds(sub, capsys):
    hand = SUB.replace("    observe: {order: [built, up, absent]}\n", "")
    mod = MOD + '''
from testrunner import NONE_HOLDS, observer

@observer("sys")
def look():
    seen = (HERE / "system").read_text() if (HERE / "system").exists() else "absent"
    return seen if seen in ("absent", "up", "built") else NONE_HOLDS
'''
    sub.write("testrunner.yml", hand)
    sub.write("m.py", mod)
    sub.write("configs/default.yml", "params: {}\n")
    sub.run("make", capsys=capsys)
    (sub.root / "system").write_text("wrecked")
    code, out = sub.run("use", capsys=capsys)
    assert code != 0 and "sys is UNKNOWN" in out
    assert not (sub.root / "used").exists()
