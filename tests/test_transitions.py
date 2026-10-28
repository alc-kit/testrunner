"""Transition rules (D7, approved 2026-10-12): what comes after a step is decided by the first
rule that matches the step's END: its outcome AND the observed state after it. A step written
with rules that matches none stops the run, failed (the marker is explicit); the old
outcome-only `on:` form keeps its meaning. The state/event matrix is printed (--matrix),
journaled at run start, and in both reports."""
import json

import pytest

from conftest import main
from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler

RUNNER = """
modules: [mod.py]
states:
  rig:
    values: [absent, up, installed, clustered]
    initial: absent
    validate: {outcome: true}
    transitions:
      - {from: "*", to: up,        by: up,      on: passed}
      - {from: "*", to: installed, by: install, on: passed}
      - {from: "*", to: clustered, by: form,    on: passed}
      - {from: "*", to: clustered, by: join,    on: passed}
paths:
  full: [up, install, {action: form, max_visits: 2}, {action: join, max_visits: 2}, verify]
default_path: full
"""
MOD = """
from pathlib import Path
from testrunner import action, observer

HERE = Path(__file__).parent

def knob(name, default=""):
    p = HERE / name
    return p.read_text().strip() if p.exists() else default

def leave(state):
    (HERE / "rig").write_text(state)

@observer("rig")
def look():
    return knob("rig", "absent")

@action(requires={"rig": "absent"}, produces={"rig": "up"})
def up():
    leave("up")

@action(requires={"rig": "up"}, produces={"rig": "installed"})
def install():
    leave("installed")

@action(requires={"rig": ["installed", "clustered"]}, produces={"rig": "clustered"})
def form():
    leave("clustered") if knob("form-forms", "yes") == "yes" else None

@action(requires={"rig": ["installed", "clustered"]}, produces={"rig": "clustered"})
def join():
    # what the cluster REALLY is after join: the knob's first word, then the next, ...
    n = int(knob("joins", "0")) + 1
    (HERE / "joins").write_text(str(n))
    leaves = (knob("join-leaves", "clustered").split() or ["clustered"])
    leave(leaves[min(n, len(leaves)) - 1])
    if knob("join-fails"):
        raise RuntimeError("join failed")

@action(readonly=True)
def membership():
    pass

@action()
def verify():
    pass
"""


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [full]\n")
    return sub


# a join the observer contradicts (it claims clustered; the rig is only installed) ends FAILED,
# with the observed state: the rules route on that (WP testrunner-state-validation, D4)
RULES = """plan:
  path: [full]
  on:
    join:
      - when: {outcome: passed, state: {rig: clustered}}
        then: continue
      - when: {outcome: failed, state: {rig: installed}}
        then: {goto: form}
      - when: {outcome: failed}
        then: [membership]
"""


def run(sub, capsys, config_text, *args):
    sub.write("configs/c.yml", config_text)
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", "-c", "c", *args])
    cap = capsys.readouterr()
    j = sub.root / "state" / "journal.jsonl"
    events = [json.loads(ln) for ln in j.read_text().splitlines()] if j.exists() else []
    return code, cap.out, cap.err, events


def steps(events):
    return [e["step"] for e in events if e["event"] == "step_end"]


def compile_(sub, text):
    sub.write("configs/x.yml", text)
    rc = load_runner_config(sub.root / "testrunner.yml")
    cfg = load_run_config(rc, "x")
    return Compiler(rc, collect(rc)).compile(cfg.plan, None, None, cfg.plan_of, cfg.data.get("edit"))


# ── routing on outcome AND observed state ───────────────────────────────────

def test_the_marker_state_continues(subscriber, capsys):
    code, out, _, ev = run(subscriber, capsys, RULES)
    assert code == 0, out
    assert steps(ev) == ["up", "install", "form", "join", "verify"]


def test_a_join_that_left_no_cluster_fails_and_diverts_to_form(subscriber, capsys):
    subscriber.write("join-leaves", "installed clustered")      # the first join leaves no cluster
    code, out, _, ev = run(subscriber, capsys, RULES)
    assert steps(ev) == ["up", "install", "form", "join", "form", "join", "verify"]
    assert "join ended failed: goto form (rule: outcome failed rig=installed)" in out
    assert "the observer reads rig='installed', the model says 'clustered'" in out


def test_no_rule_matches_the_run_stops_failed(subscriber, capsys):
    subscriber.write("join-leaves", "up")                       # a state no rule names
    rules = RULES.replace("      - when: {outcome: failed}\n        then: [membership]\n", "")
    code, out, _, ev = run(subscriber, capsys, rules)
    assert code == 1 and steps(ev)[-1] == "join"
    end = next(e for e in ev if e["event"] == "run_end")
    assert "join ended failed with state {'rig': 'up'}: no transition rule matches" in end["why"]


def test_control_the_old_form_reacts_to_the_outcome_only(subscriber, capsys):
    # a join that leaves the rig `up` while claiming clustered is a FAILED join: the outcome-only
    # form reacts to that outcome, whatever the state; one that leaves it clustered goes on
    subscriber.write("join-leaves", "up")
    code, _, _, ev = run(subscriber, capsys, "plan:\n  path: [full]\n  on: {join: {failed: stop}}\n")
    assert code == 1 and steps(ev)[-1] == "join"
    (subscriber.root / "join-leaves").unlink()
    (subscriber.root / "joins").unlink()
    (subscriber.root / "rig").unlink()
    (subscriber.root / "state" / "state.json").unlink()
    (subscriber.root / "state" / "scenario.json").unlink(missing_ok=True)
    code, _, _, ev = run(subscriber, capsys, "plan:\n  path: [full]\n  on: {join: {failed: stop}}\n")
    assert code == 0 and steps(ev)[-1] == "verify"


def test_a_failed_step_runs_the_rules_steps_then_goes_on(subscriber, capsys):
    subscriber.write("join-fails", "1")
    code, out, _, ev = run(subscriber, capsys, RULES)
    assert steps(ev) == ["up", "install", "form", "join", "membership", "verify"]
    assert code == 0                                            # a planned reaction is part of the plan (as on: always was)
    assert "join ended failed: reaction (rule: outcome failed)" in out


def test_a_rule_without_an_outcome_matches_any(subscriber, capsys):
    text = "plan:\n  path: [full]\n  on:\n    join:\n      - when: {state: {rig: [clustered, installed]}}\n        then: continue\n"
    subscriber.write("join-leaves", "installed")
    code, _, _, ev = run(subscriber, capsys, text)
    assert code == 0 and steps(ev)[-1] == "verify"


def test_a_config_reroutes_an_inherited_plan_with_a_transition_edit(subscriber, capsys):
    subscriber.write("configs/base.yml", RULES)
    subscriber.write("join-leaves", "installed clustered")
    text = ("extends: base\nedit:\n  - transition: join\n    rules:\n"
            "      - {when: {outcome: failed, state: {rig: installed}}, then: stop}\n"
            "      - {when: {outcome: passed}, then: continue}\n")
    code, out, _, ev = run(subscriber, capsys, text)
    assert steps(ev) == ["up", "install", "form", "join"]       # the edit's rules replaced the plan's
    assert "edits: transitions of join" in out
    assert "join ended failed: plan says stop (rule: outcome failed rig=installed)" in out


# ── checked before anything runs ────────────────────────────────────────────

@pytest.mark.parametrize("rule,msg", [
    ("- {when: {outcome: passed, state: {rig: sideways}}, then: continue}", r"state rig='sideways' is not a declared state"),
    ("- {when: {outcome: odd}, then: continue}", r"action 'join' never ends \['odd'\]"),
    ("- {when: {outcome: passed}, then: {goto: nowhere}}", r"goto 'nowhere' is not a step on the main path"),
    ("- {when: {colour: red}, then: continue}", r"when: takes outcome and state"),
    ("- {if: {outcome: passed}, then: continue}", r"a rule is \{when:"),
    ("- {when: {outcome: passed}, then: {jump: x}}", r"want continue, stop, \{goto: <step>\} or a step list"),
    ("- {when: {outcome: passed}, then: maybe}", r"'maybe' is neither an action nor a path"),
])
def test_bad_rules_are_refused(subscriber, rule, msg):
    with pytest.raises(ConfigError, match=msg):
        compile_(subscriber, "plan:\n  path: [full]\n  on:\n    join:\n      " + rule + "\n")


def test_an_empty_rule_list_is_refused(subscriber):
    with pytest.raises(ConfigError, match=r"want a list of \{when"):
        compile_(subscriber, "plan:\n  path: [full]\n  on:\n    join: []\n")


def test_the_static_check_follows_a_rule_the_model_cannot_reach(subscriber, capsys):
    # the model says join leaves the rig clustered; only an observer can report `installed`.
    # The rule's branch is judged anyway: running `up` from installed breaks up's requires.
    text = ("plan:\n  path: [full]\n  on:\n    join:\n"
            "      - {when: {outcome: passed, state: {rig: installed}}, then: [up]}\n"
            "      - {when: {outcome: passed}, then: continue}\n")
    code, out, _, _ = run(subscriber, capsys, text, "--plan")
    assert code == 2 and "up#2: requires {'rig': 'absent'}, but up > install > form > join leaves {'rig': 'installed'}" in out


def test_control_the_same_plan_without_that_rule_passes_the_check(subscriber, capsys):
    text = "plan:\n  path: [full]\n  on:\n    join:\n      - {when: {outcome: passed}, then: continue}\n"
    code, _, _, _ = run(subscriber, capsys, text, "--plan")
    assert code == 0


# ── the state/event matrix ───────────────────────────────────────────────────

def test_plan_matrix_prints_the_matrix(subscriber, capsys):
    code, out, _, _ = run(subscriber, capsys, RULES, "--plan", "--matrix")
    assert code == 0
    lines = out.splitlines()
    head = next(i for i, ln in enumerate(lines) if "requires (state in)" in ln)
    table = "\n".join(lines[head:])
    assert "join" in table and "rig: installed|clustered" in table
    assert "outcome passed rig=clustered -> continue; otherwise -> stop (failed)" in table
    failed = next(ln for ln in table.splitlines() if "| failed " in ln and "goto form" in ln)
    assert "(kept: no end result declared)" in failed                 # no end result declared: kept (user 2026-10-28)
    assert "| outcome failed rig=installed -> goto form; outcome failed -> run membership, then go on" in failed
    assert "passed*" in table and "stop (aborted)" in table
    assert "on join otherwise: stop (failed)" in out


def test_control_without_matrix_no_table(subscriber, capsys):
    _, out, _, _ = run(subscriber, capsys, RULES, "--plan")
    assert "requires (state in)" not in out


def test_the_matrix_is_journaled_and_in_both_reports(subscriber, capsys):
    code, _, _, ev = run(subscriber, capsys, RULES)
    start = next(e for e in ev if e["event"] == "run_start")
    rows = start["matrix"]
    i = next(i for i, r in enumerate(rows) if r["step"] == "join")
    assert rows[i]["outcome"] == "passed*" and rows[i + 1]["outcome"] == "failed"
    assert "goto form" in rows[i + 1]["next"] and rows[i + 1]["after"] == "(kept: no end result declared)"
    for fmt, needle in (("md", "## State/event matrix"), ("html", "<h2>State/event matrix</h2>")):
        main(["--root", str(subscriber.root), "--color", "never", "--report-of", "last", "--report-format", fmt])
        path = capsys.readouterr().out.strip().splitlines()[0]
        assert needle in (subscriber.root / path).read_text()
