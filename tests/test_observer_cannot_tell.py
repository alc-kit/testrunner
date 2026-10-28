"""An observer that cannot tell says so (CANNOT_TELL), and an UNKNOWN variable stays unknown.

Measured 2026-10-28 (lab run baf2f66c): `form` claimed rig=clustered, the probe validators found
neither the claim nor the value before it held, so rig became UNKNOWN (D6). Two seconds later the
observer, which can only tell absent from not-absent, answered its floor `up`, and the runner
stored it as reality ("reality wins"): run_end and state.json said `up` over a rig with PVE
installed and a cluster on one node. An observer's answer is reality only when it READ a value."""
import json

SUB = """
modules: [m.py]
states:
  sys:
    values: [absent, up, built]
    initial: absent
    validate: {python: probe}
    transitions:
      - {from: absent, to: up,    by: make,  on: passed}
      - {from: up,     to: built, by: build, on: passed}
"""
# `make` is confirmed; `build` passes but leaves the system half-built: neither `built` (the
# claim) nor `up` (the value before) holds, so sys becomes UNKNOWN. The observer, like ext.rig's
# observe_rig, can tell absent from not-absent only.
MOD = """
from pathlib import Path
from testrunner import CANNOT_TELL, action, observer, validator

HERE = Path(__file__).parent
FLOOR = HERE / "observer-answers-floor"      # the old behaviour, for the negative control

@validator("probe")
def probe(claim):
    seen = (HERE / "system").read_text() if (HERE / "system").exists() else "absent"
    return seen == claim.value, f"system reads {seen!r}"

@observer("sys")
def look(run):
    if not (HERE / "system").exists():
        return "absent"
    have = run.state.get("sys")
    if have is None:
        return "up" if FLOOR.exists() else CANNOT_TELL
    return have

@action(produces={"sys": {"absent": "up"}})
def make():
    (HERE / "system").write_text("up")

@action(produces={"sys": {"up": "built"}})
def build():
    (HERE / "system").write_text("half-built")
"""


def make(sub):
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def journal(sub):
    return [json.loads(line) for line in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def test_an_unknown_variable_stays_unknown_when_the_observer_cannot_tell(sub, capsys):
    make(sub)
    code, out = sub.run("make", "build", capsys=capsys)
    assert code == 1
    assert stored(sub)["sys"] is None                                   # still UNKNOWN
    end = [e for e in journal(sub) if e["event"] == "run_end"][-1]
    assert end["state"]["sys"] is None and not end["passed"]
    assert "cannot tell" in out and "reality wins" not in out


def test_control_an_observer_that_answers_its_floor_is_stored(sub, capsys):
    # negative control: the same run, with the observer answering `up` as observe_rig did —
    # the runner stores it, which is exactly what the observer must not do
    make(sub)
    (sub.root / "observer-answers-floor").touch()
    code, out = sub.run("make", "build", capsys=capsys)
    assert code == 1 and stored(sub)["sys"] == "up"


def test_cannot_tell_before_the_plan_keeps_the_stored_value(sub, capsys):
    # before the plan: an observer that cannot tell leaves the stored value, and says why
    make(sub)
    (sub.root / "system").write_text("half-built")
    sub.write("state/state.json", json.dumps({"sys": None}))
    code, out = sub.run("--plan", capsys=capsys)
    code, out = sub.run("build", capsys=capsys)
    assert "observer for sys cannot tell" in out
    assert "could not observe the current state" not in out             # not the whole look abandoned
