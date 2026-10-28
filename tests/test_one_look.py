"""One look, one reading (WP testrunner-state-validation §8.2, E3; user 2026-10-28: "implement both
E3 cost cuts").

Measured on the lab rig (run e01b7095): a derived observer asked its values in order, and each
value's probe read the whole rig again (about 10 s each); and a run looked twice before its first
step, once before the plan and again at run start (about 44 s). Now:
- a fixture with scope "look" is set up once per look: a derived observer's values in order, or
  the check after a step. Every validator in that look shares the one reading;
- the run start reuses the look taken before the plan, while the state it started from is still
  the state the run starts from."""
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
      - {from: up,     to: built, by: build, on: passed}
"""
MOD = """
from pathlib import Path
from testrunner import action, fixture, validator

HERE = Path(__file__).parent
READS = HERE / "reads"

def note(what):
    with READS.open("a") as f:
        f.write(what + "\\n")

@fixture(scope="look")
def reading():
    note("read")                    # one line per reading of the system
    return (HERE / "system").read_text() if (HERE / "system").exists() else "absent"

@validator("probe")
def probe(claim, reading):
    return reading == claim.value, f"system reads {reading!r}"

@action(produces={"sys": {"absent": "up"}})
def make():
    note("make")
    (HERE / "system").write_text("up")

@action(produces={"sys": {"up": "built"}})
def build():
    note("build")
    (HERE / "system").write_text("half-built")   # the claim `built` does not hold, nor does `up`

@action()
def peek(reading):                  # an action asking for a look fixture: no look is open
    pass
"""


def make(sub, mod=MOD):
    sub.write("testrunner.yml", SUB)
    sub.write("m.py", mod)
    sub.write("configs/default.yml", "params: {}\n")


def reads(sub):
    f = sub.root / "reads"
    return f.read_text().split() if f.exists() else []


def journal(sub):
    return [json.loads(line) for line in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def between(log, a, b):
    """The readings between two marks in the reads log."""
    i, j = log.index(a), log.index(b) if b else len(log)
    return log[i + 1:j]


def test_a_derived_observer_reads_once_per_look(sub, capsys):
    # absent rig: the order asks built, up, absent — three values, one reading
    make(sub)
    code, out = sub.run("make", capsys=capsys)
    assert code == 0
    log = reads(sub)
    assert log.index("make") == 1, log          # exactly one reading before the first step
    assert log[0] == "read"


def test_the_check_after_a_step_reads_once(sub, capsys):
    # build's claim fails: the claimed value and the value before are both asked — one reading
    make(sub)
    code, out = sub.run("make", "build", capsys=capsys)
    assert code == 1
    log = reads(sub)
    after_build = between(log, "build", None)
    # the settle (one look) and the observer after the step (one look): two readings, not 2 + 3
    assert after_build == ["read", "read"], log


def test_a_look_fixture_outside_a_look_is_refused(sub, capsys):
    make(sub)
    code, out = sub.run("peek", capsys=capsys)
    assert code != 0
    assert "scope 'look', which is not open here" in out or "which is not open here" in out


def test_the_run_start_reuses_the_look_before_the_plan(sub, capsys):
    make(sub)
    code, out = sub.run("make", capsys=capsys)
    assert code == 0
    assert reads(sub)[:2] == ["read", "make"]           # ONE look before the first step, not two
    start = [e for e in journal(sub) if e["event"] == "run_start"][-1]
    assert start["observed_before_plan"]["read"] == {"sys": "absent"}


def test_control_a_changed_store_is_looked_at_again(sub, capsys, monkeypatch):
    # the state the pre-plan look started from is no longer the state the run starts from:
    # the run must look again, not reuse a stale reading
    from testrunner import runner as runner_mod
    make(sub)
    real = runner_mod.Runner._run

    async def store_changes_first(self, program):
        self.store.set_state({"sys": "up"})       # as if another run had changed it in between
        return await real(self, program)
    monkeypatch.setattr(runner_mod.Runner, "_run", store_changes_first)
    code, out = sub.run("make", capsys=capsys)
    log = reads(sub)
    assert log.count("read") >= 2 and log.index("make") != 1, log    # looked again at the start
    start = [e for e in journal(sub) if e["event"] == "run_start"][-1]
    assert "observed_before_plan" not in start
