"""An observer derived from validators (WP testrunner-state-validation D4, user 2026-10-27):
`observe: {order: [...]}` asks each value's validator in that order, and the first that HOLDS is
the value. A value that cannot tell, or no value holding, means the observer cannot look. Only
validators that LOOK can be asked: an outcome/const one is refused. Each rule has its control."""
import json

from conftest import main

RUNNER = """
modules: [m.py]
states:
  sys:
    initial: absent
    values:
      absent: {validate: {run: "test ! -f system"}}
      up:     {validate: {run: "test -f unsure && exit 3; test -f system"}}
    transitions:
      - {from: absent, to: up, by: make, on: passed}
    observe: {order: [up, absent]}
"""
MOD = """
from pathlib import Path
from testrunner import action

@action(produces={"sys": {"absent": "up"}})
def make():
    (Path(__file__).parent / "system").write_text("up")

@action(requires={"sys": "up"})
def use():
    pass
"""


def setup(sub, runner=RUNNER):
    sub.write("testrunner.yml", runner)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    cap = capsys.readouterr()
    st = sub.root / "state" / "state.json"
    return code, cap.out + cap.err, (json.loads(st.read_text()) if st.exists() else {})


def test_the_first_value_that_holds_is_what_the_system_is(sub, capsys):
    s = setup(sub)
    s.write("system", "up")                         # built by something else: the store never saw it
    code, out, state = run(s, capsys, "use")
    assert code == 0, out
    assert "observed sys='up' (the store said 'absent')" in out
    assert state["sys"] == "up"


def test_control_nothing_built_reads_absent_and_the_plan_is_refused(sub, capsys):
    code, out, _ = run(setup(sub), capsys, "use")
    assert code != 0 and "use: requires {'sys': 'up'}" in out


def test_a_value_that_cannot_tell_means_the_observer_cannot_look(sub, capsys):
    s = setup(sub)
    s.write("system", "up")
    s.write("unsure", "")                           # `up` cannot tell: `absent` must NOT be taken
    code, out, _ = run(s, capsys, "use")
    assert code != 0
    # a value that cannot tell stops the search: the observer could not look, so the stored value
    # stays (never NONE_HOLDS, never the next value in the order)
    assert "observer for sys cannot tell" in out and "sys=up (observe): cannot tell" in out
    assert "sys stays" in out and "no value of sys holds" not in out


def test_a_derived_observer_confirms_what_a_step_claims(sub, capsys):
    code, out, state = run(setup(sub), capsys, "make", "use")
    assert code == 0, out
    assert state["sys"] == "up"


def test_an_outcome_validator_cannot_be_observed(sub, capsys):
    runner = RUNNER.replace('absent: {validate: {run: "test ! -f system"}}', "absent: {validate: {outcome: true}}")
    code, out, _ = run(setup(sub, runner), capsys, "--list")
    assert code != 0 and "observe.order asks 'absent', whose validator (outcome) cannot look" in out


def test_list_says_how_a_variable_is_read(sub, capsys):
    code, out, _ = run(setup(sub), capsys, "--list")
    assert code == 0 and "sys: ['absent', 'up']  initial absent  (observed in order ['up', 'absent'])" in out
