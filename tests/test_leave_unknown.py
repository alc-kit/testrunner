"""Out of UNKNOWN (user 2026-10-28): "if a step touches a value into something different than UNKNOWN, it
should be allowed. If however the step fails to change the value OUT OF UNKNOWN, that should be
considered a failure."

Found on the lab's sandbox (WP testrunner-state-validation B6c): a breached network made its variable
UNKNOWN, and the step that claims it ran from there and PASSED with nothing checked — no line leaves
UNKNOWN, so no claim applied, and the look after the step (still no value holding) failed nothing."""
import json

SUB = """
modules: [m.py]
states:
  sys:
    values: [absent, up]
    initial: absent
    validate: {python: probe}
    observe: {order: [up, absent]}
    transitions:
      - {from: absent, to: up, by: make,     on: passed}
      - {from: absent, to: up, by: repair,   on: passed}
      - {from: absent, to: up, by: pretend,  on: passed}
scenario: {release_when: {sys: absent}}
"""
MOD = """
from pathlib import Path
from testrunner import action, validator

HERE = Path(__file__).parent

@validator("probe")
def probe(claim):
    seen = (HERE / "system").read_text() if (HERE / "system").exists() else "absent"
    return seen == claim.value, f"system reads {seen!r}"

@action(produces={"sys": {"absent": "up"}})
def make():
    (HERE / "system").write_text("up")

@action()
def wreck():
    (HERE / "system").write_text("wrecked")      # in none of the values

@action(produces={"sys": {"absent": "up"}})
def repair():
    (HERE / "system").write_text("up")           # brings it OUT of UNKNOWN

@action(produces={"sys": {"absent": "up"}})
def pretend():
    pass                                          # claims sys, changes nothing

@action()
def other():
    (HERE / "other").touch()                      # claims nothing about sys

@action(requires={"sys": "up"})
def use():
    (HERE / "used").touch()
"""


def unknown_sys(sub, capsys):
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    sub.run("make", capsys=capsys)
    (sub.root / "system").write_text("wrecked")          # reality moved: sys is in none of its values
    sub.run("other", capsys=capsys)                       # a look at the start reads it: UNKNOWN
    assert stored(sub)["sys"] is None


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def journal(sub):
    f = sub.root / "state" / "journal.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def test_a_step_that_brings_the_value_out_of_unknown_is_allowed(sub, capsys):
    unknown_sys(sub, capsys)
    code, out = sub.run("repair", capsys=capsys)
    assert code == 0, out
    assert stored(sub)["sys"] == "up"
    assert not [e for e in journal(sub) if e["event"] == "unknown_not_left"]


def test_a_step_that_leaves_the_value_unknown_fails(sub, capsys):
    unknown_sys(sub, capsys)
    code, out = sub.run("pretend", capsys=capsys)
    assert code == 1, out
    assert "sys was UNKNOWN before this step, which claims sys, and is still UNKNOWN after it" in out, out
    assert "system reads 'wrecked'" in out                # the reason, value by value
    left = [e for e in journal(sub) if e["event"] == "unknown_not_left"]
    assert left and left[-1]["var"] == "sys" and stored(sub)["sys"] is None


def test_a_step_that_does_not_claim_the_unknown_value_is_not_judged_on_it(sub, capsys):
    unknown_sys(sub, capsys)
    code, out = sub.run("other", capsys=capsys)
    assert code == 0, out
    assert not [e for e in journal(sub) if e["event"] == "unknown_not_left"]


def test_requires_on_an_unknown_value_still_stops_the_step_before_it_runs(sub, capsys):
    unknown_sys(sub, capsys)
    code, out = sub.run("use", capsys=capsys)
    assert code != 0 and "whose value is unknown" in out, out
    assert not (sub.root / "used").exists()
