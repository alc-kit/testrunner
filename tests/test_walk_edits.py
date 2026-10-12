"""Walk edits: the run config's `edit:` list repeats, inserts, skips and replaces steps of the
compiled walk, anchored on step ids, in order; edits accumulate through `extends:`. Asked for
2026-10-12: "re-run the ping step again after the cluster has been joined", and an easy way
to add, bypass and remove stages without editing the shared path. Strict in a config; with
steps given on the command line an edit whose anchor is not in the run is dropped with a note."""
import json

import pytest

from conftest import main
from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler

RUNNER = """
modules: [mod.py]
states:
  s: {values: [a], initial: a}
paths:
  full: [up, ping, join, verify]
default_path: full
"""
MOD = """
from testrunner import action
for _n in ("up", "ping", "join", "verify", "probe", "extra"):
    exec(f"@action(name={_n!r})\\ndef _{_n}(): pass")
"""


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [{stages: [full], with: {k: 1}}]\n")
    return sub


def compile_(sub, config="default", steps=None):
    rc = load_runner_config(sub.root / "testrunner.yml")
    run = load_run_config(rc, config)
    return Compiler(rc, collect(rc)).compile(run.plan, steps, None, run.plan_of, run.data.get("edit"))


def walk(prog):
    return [s.id for s in prog.main]


def cfg(sub, name, text):
    sub.write(f"configs/{name}.yml", "extends: default\n" + text)
    return name


# ── the four edits ───────────────────────────────────────────────────────────

def test_repeat_runs_the_same_step_again_later(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join}\n"))
    assert walk(prog) == ["up", "ping", "join", "ping#2", "verify"]
    again = prog.step("ping#2")
    assert again.action == "ping" and again.with_ == {"k": 1}          # with copied, not restated


def test_repeat_takes_an_explicit_id_and_before(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, before: verify, id: ping-after-join}\n"))
    assert walk(prog) == ["up", "ping", "join", "ping-after-join", "verify"]


def test_repeating_a_repeat_counts_from_the_base_name(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join}\n"
                                                     "  - {repeat: 'ping#2', after: verify}\n"))
    assert walk(prog) == ["up", "ping", "join", "ping#2", "verify", "ping#3"]


def test_insert_new_steps(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - insert: [probe, {action: extra, id: x1}]\n"
                                                     "    after: join\n"))
    assert walk(prog) == ["up", "ping", "join", "probe", "x1", "verify"]


@pytest.mark.parametrize("skip", ["ping", "[ping, verify]"])
def test_skip(subscriber, skip):
    prog = compile_(subscriber, cfg(subscriber, "c", f"edit:\n  - skip: {skip}\n"))
    assert "ping" not in walk(prog) and walk(prog)[0] == "up"


def test_replace_keeps_the_place_and_may_reuse_the_id(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - replace: ping\n"
                                                     "    by: [{action: ping, with: {k: 2}}, probe]\n"))
    assert walk(prog) == ["up", "ping", "probe", "join", "verify"]
    assert prog.step("ping").with_ == {"k": 2}


def test_edits_apply_in_order_on_ids_earlier_edits_made(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join, id: p2}\n"
                                                     "  - {insert: [probe], after: p2}\n"
                                                     "  - {skip: p2}\n"))
    assert walk(prog) == ["up", "ping", "join", "probe", "verify"]


# ── through extends: edits accumulate, parent first ─────────────────────────

def test_a_child_edits_the_inherited_plan_without_restating_it(subscriber):
    subscriber.write("configs/parent.yml", "extends: default\nedit:\n  - {repeat: ping, after: join, id: p2}\n")
    subscriber.write("configs/child.yml", "extends: parent\nedit:\n  - {insert: [probe], after: p2}\n")
    prog = compile_(subscriber, "child")
    assert walk(prog) == ["up", "ping", "join", "p2", "probe", "verify"]


def test_control_without_accumulation_the_child_anchor_is_missing(subscriber):
    # the child alone (its parent's edit absent) must be refused: p2 does not exist
    subscriber.write("configs/lone.yml", "extends: default\nedit:\n  - {insert: [probe], after: p2}\n")
    with pytest.raises(ConfigError, match=r"edit\[0\]: insert: anchor 'p2' is not a step on the walk"):
        compile_(subscriber, "lone")


# ── strict in a config, lenient with command-line steps ─────────────────────

@pytest.mark.parametrize("text,msg", [
    ("edit:\n  - {repeat: nope, after: join}\n", r"edit\[0\]: repeat: step 'nope' is not a step on the walk"),
    ("edit:\n  - {skip: nope}\n", r"edit\[0\]: skip: step 'nope'"),
    ("edit:\n  - {frobnicate: ping}\n", r"edit\[0\]: want exactly one of repeat, insert, skip, replace"),
    ("edit:\n  - {skip: ping, repeat: ping}\n", r"want exactly one of"),
    ("edit:\n  - {repeat: ping}\n", r"repeat needs exactly one of after: or before:"),
    ("edit:\n  - {insert: [probe], after: up, before: join}\n", r"insert needs exactly one of after: or before:"),
    ("edit:\n  - {skip: ping, after: up}\n", r"skip takes no other key, not after"),
    ("edit:\n  - {replace: ping}\n", r"replace needs by:"),
    ("edit: {skip: ping}\n", r"edit: want a list of edits"),
])
def test_bad_edits_are_refused(subscriber, text, msg):
    with pytest.raises(ConfigError, match=msg):
        compile_(subscriber, cfg(subscriber, "bad", text))


def test_with_command_line_steps_a_missing_anchor_is_a_note(subscriber):
    prog = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join}\n  - {skip: verify}\n"),
                    ["up", "ping"])
    assert walk(prog) == ["up", "ping"]
    assert any("edit[0] (repeat anchor 'join') dropped: not in this run" in n for n in prog.notes)
    assert any("edit[1] (skip step 'verify') dropped" in n for n in prog.notes)


def test_on_and_expect_may_name_an_edited_in_step(subscriber):
    subscriber.write("configs/c.yml", "extends: default\nplan:\n  path: [full]\n  expect: {p2: {outcome: passed}}\n"
                                      "edit:\n  - {repeat: ping, after: join, id: p2}\n")
    prog = compile_(subscriber, "c")
    assert "p2" in prog.expect


def test_expect_on_a_skipped_step_is_refused(subscriber):
    subscriber.write("configs/c.yml", "extends: default\nplan:\n  path: [full]\n  expect: {ping: {outcome: passed}}\n"
                                      "edit:\n  - {skip: ping}\n")
    with pytest.raises(ConfigError, match=r"plan.expect: no step 'ping'"):
        compile_(subscriber, "c")


# ── visible: banner, journal ─────────────────────────────────────────────────

def test_the_banner_and_the_journal_name_the_edits(subscriber, capsys):
    cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join, id: ping-after-join}\n  - {skip: verify}\n")
    code = main(["--root", str(subscriber.root), "--color", "never", "--non-interactive", "-c", "c"])
    out = capsys.readouterr().out
    assert code == 0, out
    want = "edits: +ping-after-join (again ping) after join, -verify"
    assert want in out
    start = next(json.loads(ln) for ln in (subscriber.root / "state" / "journal.jsonl").read_text().splitlines()
                 if json.loads(ln)["event"] == "run_start")
    assert start["steps"] == ["up", "ping", "join", "ping-after-join"] and want in start["source"]


def test_a_selected_scenario_keeps_the_edits(subscriber, capsys):
    cfg(subscriber, "c", "edit:\n  - {repeat: ping, after: join}\n")
    run = ["--root", str(subscriber.root), "--color", "never", "--non-interactive"]
    assert main([*run, "-c", "c", "--select"]) == 0
    capsys.readouterr()
    assert main([*run, "--plan"]) == 0
    assert "+ping#2 (again ping) after join" in capsys.readouterr().out
