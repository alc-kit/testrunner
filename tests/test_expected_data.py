"""Declared data — extended state (WP testrunner-state-validation §10 B4; D9 decisions 1–2, user
2026-10-28: "allow / for expected"). `data:` declares sets of names; a `machine:` line may carry a
UML effect `/ <name> += <template>` or `-= <template>`, applied only when that line's claim is
CONFIRMED by its validator. The value is stored with the state, journalled, a run fixture (`data`)
and a template (`{data.<name>}`) — so a probe can judge "clustered" against the CURRENT expected
nodes, not a fixed list (D9: "up" after a late node was added and the founder removed)."""
import json

import pytest

from conftest import main

RUNNER = """
modules: [m.py]
data:
  expected: {initial: @INITIAL@}
states:
  sys:
    values: [absent, up]
    validate: {python: sys-is}
    model_only: true
    machine: |
      [*]    --> absent
      absent --> up     : make [passed]
      absent --> absent : make [failed]
      absent --> absent : make [errored]
      absent --> absent : make [aborted]
      absent --> absent : make [skipped]
      up     --> up     : grow [passed] / expected += {param.node}
      up     --> up     : grow [failed]
      up     --> up     : grow [errored]
      up     --> up     : grow [aborted]
      up     --> up     : grow [skipped]
      up     --> up     : shrink [passed] / expected -= {param.node}
      up     --> up     : shrink [failed]
      up     --> up     : shrink [errored]
      up     --> up     : shrink [aborted]
      up     --> up     : shrink [skipped]
      up     --> absent : wipe [passed]
      up     --> up     : wipe [failed]
      up     --> up     : wipe [errored]
      up     --> up     : wipe [aborted]
      up     --> up     : wipe [skipped]
"""
MOD = """
import json
from pathlib import Path
from testrunner import action, resolver, validator

HERE = Path(__file__).parent

def knob(name):
    return (HERE / name).read_text().strip() if (HERE / name).exists() else ""

@resolver("catalogue")
def catalogue(arg):
    return ["n1", "n2", "n3"]

@validator()
def sys_is(claim, data):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    (HERE / "seen-expected").write_text(json.dumps(data.get("expected")))
    if knob("lie"):
        return False, "the probe says no"
    return s == claim.value, f"the system reads {s!r}"

@action(produces={"sys": {"absent": "up"}})
def make():
    (HERE / "system").write_text("up")

@action()
def grow():
    pass

@action()
def shrink():
    pass

@action(produces={"sys": {"up": "absent"}})
def wipe():
    (HERE / "system").write_text("absent")
"""


def make_sub(sub, initial="[n1, n2, n3]"):
    sub.write("testrunner.yml", RUNNER.replace("@INITIAL@", initial))
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def journal(sub):
    return [json.loads(x) for x in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]



def data_now(sub):
    return [e for e in journal(sub) if e["event"] == "run_end"][-1]["data"]


def test_initial_data_is_declared_stored_and_journalled(sub, capsys):
    s = make_sub(sub)
    code, out = run(s, capsys, "make")
    assert code == 0, out
    start = [e for e in journal(s) if e["event"] == "run_start"][-1]
    assert start["data"] == {"expected": ["n1", "n2", "n3"]}
    assert json.loads((s.root / "seen-expected").read_text()) == ["n1", "n2", "n3"]   # the `data` fixture


def test_initial_data_from_a_resolver(sub, capsys):
    s = make_sub(sub, initial='"{catalogue}"')
    code, out = run(s, capsys, "make")
    assert code == 0, out
    assert data_now(s) == {"expected": ["n1", "n2", "n3"]}


def test_an_effect_applies_when_its_claim_is_confirmed_and_persists(sub, capsys):
    s = make_sub(sub)
    run(s, capsys, "make")
    code, out = run(s, capsys, "grow", "--with", "node=n4")
    assert code == 0, out
    ch = [e for e in journal(s) if e["event"] == "data_changed"]
    assert ch and ch[-1]["op"] == "+=" and ch[-1]["now"] == ["n1", "n2", "n3", "n4"]
    assert ch[-1]["by"] == "up --> up : grow [passed]"
    code, out = run(s, capsys, "shrink", "--with", "node=n1")      # a later run starts from the stored value
    assert code == 0, out
    assert data_now(s) == {"expected": ["n2", "n3", "n4"]}


def test_set_semantics_and_no_change_is_journalled(sub, capsys):
    s = make_sub(sub)
    run(s, capsys, "make")
    code, out = run(s, capsys, "grow", "--with", "node=n2")       # already a member
    assert code == 0
    ch = [e for e in journal(s) if e["event"] == "data_changed"][-1]
    assert ch["was"] == ch["now"] == ["n1", "n2", "n3"]


def test_control_no_effect_when_the_claim_does_not_hold(sub, capsys):
    s = make_sub(sub)
    run(s, capsys, "make")
    s.write("lie", "1")                                         # the validator refuses the self-loop claim
    code, out = run(s, capsys, "grow", "--with", "node=n4")
    assert code == 1
    assert not [e for e in journal(s) if e["event"] == "data_changed"]
    assert data_now(s) == {"expected": ["n1", "n2", "n3"]}


def test_data_is_listed(sub, capsys):
    code, out = run(make_sub(sub), capsys, "--list")
    assert code == 0 and "expected: initial ['n1', 'n2', 'n3']" in out
    assert "/ expected += {param.node}" in out


@pytest.mark.parametrize("change,why", [
    ("      up     --> up     : grow [passed] / expected += {param.node}\n",
     "      up     --> up     : grow [passed] / members += {param.node}\n"),
    ("data:\n  expected: {initial: @INITIAL@}\n", "data:\n  sys: {initial: [a]}\n"),
    ("data:\n  expected: {initial: @INITIAL@}\n", "data:\n  expected: {initial: 3}\n"),
])
def test_refused(sub, capsys, change, why):
    sub.write("testrunner.yml", RUNNER.replace(change, why).replace("@INITIAL@", "[n1]"))
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "params: {}\n")
    code, out = run(sub, capsys, "--list")
    assert code != 0


def test_an_effect_on_a_failure_line_is_refused():
    from testrunner.config import ConfigError
    from testrunner.statemachine import parse_states
    with pytest.raises(ConfigError, match="an effect on a failure line"):
        parse_states("t.yml", {"sys": {"values": ["absent", "up"], "validate": {"outcome": True},
                                       "machine": "absent --> absent : make [failed] / expected += x\n"}},
                     {"expected": ["a"]})


def test_the_look_before_the_plan_sees_the_declared_data(sub, capsys):
    # found by B6's rig proof (2026-10-28): the observers asked BEFORE the plan had no `data`
    # fixture, so every probe that judges against the expected nodes answered "cannot tell"
    s = make_sub(sub)
    s.write("testrunner.yml", (s.root / "testrunner.yml").read_text().replace(
        "    model_only: true\n", "    observe: {order: [up, absent]}\n"))
    s.write("system", "up")                                  # the system IS up; the store says nothing
    code, out = run(s, capsys, "--dry-run", "grow", "--with", "node=n9")
    assert "no fixture named 'data'" not in out
    assert "observed sys='up'" in out, out
    assert json.loads((s.root / "seen-expected").read_text()) == ["n1", "n2", "n3"]
