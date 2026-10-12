"""Reality is re-read after every step (test_reality_after_failure.py) — except after a
read-only action, which only looks and may produce no state: re-reading after it costs a
full observation and can show nothing new. Measured 2026-10-11 on lab-opnsense: a
`./run.sh status` (a 3 s read-only step) observed the rig three times, ~2.8 s each."""
from conftest import Sub, main

SUB = """
modules: [m.py]
states: {sys: {values: [absent, up], initial: absent}}
"""
MOD = """
from pathlib import Path
from testrunner import action, observer

HERE = Path(__file__).parent

@observer("sys")
def look():
    with open(HERE / "observations", "a") as f:
        f.write("x")
    return "absent"

@action(readonly=True)
def peek():
    pass

@action()
def poke():
    pass
"""


def observations(tmp_path, name, *steps):
    sub = Sub(tmp_path / name)
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *steps])
    assert code == 0
    return len((sub.root / "observations").read_text())


def test_no_observation_after_a_read_only_step(tmp_path):
    assert observations(tmp_path, "two", "peek", "peek") == observations(tmp_path, "one", "peek")


def test_control_a_state_changing_step_is_still_followed_by_an_observation(tmp_path):
    assert observations(tmp_path, "two", "poke", "poke") == observations(tmp_path, "one", "poke") + 1


def test_a_read_only_step_in_a_state_changing_run_is_not_observed_after(tmp_path):
    assert observations(tmp_path, "mixed", "poke", "peek") == observations(tmp_path, "one", "poke")
