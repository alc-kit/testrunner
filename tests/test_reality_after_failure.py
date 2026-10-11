"""A step that FAILS can still have changed the system. Measured 2026-10-11 on a real
rig: `up` built every VM, then failed on a missing host tool; the runner kept the
model's `rig: absent` (a failed outcome produces nothing, and only the variables a step
produced were re-observed), and release_when {rig: absent} then RELEASED the scenario
over five running VMs. Reality is read after every step, whatever its outcome, and a
scenario is only released on an observed state."""
import json

SUB = """
modules: [m.py]
states: {sys: {values: [absent, up], initial: absent}}
scenario: {release_when: {sys: absent}}
"""
MOD = """
from pathlib import Path
from testrunner import action, observer

SYS = Path(__file__).parent / "system"

@observer("sys")
def look():
    if (Path(__file__).parent / "observer-broken").exists():
        raise RuntimeError("cannot look")
    return "up" if SYS.exists() else "absent"

@action(produces={"sys": {"absent": "up"}})
def build_then_fail():
    SYS.mkdir(exist_ok=True)            # the system now exists ...
    raise RuntimeError("a host tool is missing")   # ... and the step fails anyway

@action(produces={"sys": {"absent": "up"}})
def fail_cleanly():
    raise RuntimeError("refused before touching anything")
"""


def make(sub):
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def scenario_kept(sub):
    return (sub.root / "state" / "scenario.json").exists()


def test_a_failed_step_that_built_the_system_is_judged_by_reality(sub, capsys):
    make(sub)
    code, out = sub.run("build-then-fail", capsys=capsys)
    assert code == 1
    assert stored(sub)["sys"] == "up"                       # not the model's 'absent'
    assert scenario_kept(sub) and "scenario released" not in out


def test_a_failed_step_that_changed_nothing_still_releases(sub, capsys):
    # negative control: same failure, nothing built -> absent is real, release is right
    make(sub)
    code, out = sub.run("fail-cleanly", capsys=capsys)
    assert code == 1 and stored(sub)["sys"] == "absent"
    assert not scenario_kept(sub) and "scenario released" in out


def test_no_release_when_reality_cannot_be_read(sub, capsys):
    # the observer fails at the end: an unobserved 'absent' must not release a scenario
    make(sub)
    (sub.root / "observer-broken").touch()
    code, out = sub.run("fail-cleanly", capsys=capsys)
    assert scenario_kept(sub) and "scenario released" not in out
