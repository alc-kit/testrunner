"""`all` on the command line: the run config's plan, exactly as a bare run; the runner
config's `all.fallback` path when the plan is empty. The steps always say where they came
from. Every rule is paired with the input that must be refused."""
import json

import pytest

from conftest import main
from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler

RUNNER = """
modules: [mod.py]
states:
  s: {values: [a, b], initial: a}
paths:
  lifecycle: [one, two, three, four]
  full: [one, two, three]
default_path: lifecycle
all: {fallback: full}
"""
MOD = """
from testrunner import action
@action()
def one(): pass
@action()
def two(): pass
@action()
def three(): pass
@action()
def four(): pass
"""
CONFIGS = {
    "default": "plan: []\n",
    "base": "extends: default\nplan: [one, four]\n",
    "child": "extends: base\nx: 1\n",                 # inherits base's plan
    "own": "extends: base\nplan: [two]\n",            # states its own
    "strict": "extends: default\nplan: {path: [one], detours: [{after: three, steps: [four]}]}\n",
    "lean": "extends: default\nplan: {path: [], with: {k: v}, detours: [{after: two, steps: [four]}]}\n",
    "nested": "extends: default\nplan: [{stages: [all]}]\n",
    "ranged": "extends: default\nplan: ['all:one..two']\n",
}


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    for name, text in CONFIGS.items():
        sub.write(f"configs/{name}.yml", text)
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def walk(out):
    """The step ids `--plan` printed, in order."""
    return [line.split()[1] for line in out.splitlines() if line.strip()[:1].isdigit()]


def compile_(sub, config, steps, runner=RUNNER):
    sub.write("testrunner.yml", runner)
    rc = load_runner_config(sub.root / "testrunner.yml")
    rcfg = load_run_config(rc, config)
    return Compiler(rc, collect(rc)).compile(rcfg.plan, steps, None, rcfg.plan_of)


# ── what `all` runs ──────────────────────────────────────────────────────────

def test_all_runs_the_plan_and_names_the_file_that_states_it(subscriber, capsys):
    code, out, err = run(subscriber, capsys, "-c", "child", "--plan", "all")
    assert code == 0, out + err
    assert walk(out) == ["one", "four"]
    assert "steps: all = plan of base.yml (via child.yml)" in out


def test_a_bare_run_says_the_same_source_without_all(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "child", "--plan")
    assert code == 0 and walk(out) == ["one", "four"]
    assert "steps: plan of base.yml (via child.yml)" in out and "all =" not in out


def test_a_plan_stated_by_the_config_itself_has_no_via(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "own", "--plan", "all")
    assert code == 0 and walk(out) == ["two"]
    assert "steps: all = plan of own.yml\n" in out


def test_all_on_an_empty_plan_falls_back(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "default", "--plan", "all")
    assert code == 0 and walk(out) == ["one", "two", "three"]
    assert "steps: all = path full (default.yml has no plan)" in out


def test_the_fallback_path_by_name_is_always_the_fixed_walk(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "child", "--plan", "full")
    assert code == 0 and walk(out) == ["one", "two", "three"]
    assert "steps: path full (command line)" in out


def test_other_steps_say_command_line(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "child", "--plan", "two", "one..two")
    assert code == 0 and walk(out) == ["two", "one", "two#2"]
    assert "steps: command line: two one..two" in out


def test_the_old_banner_is_gone(subscriber, capsys):
    """It said `plan of child.yml (--config)` whatever the steps were: the trap this replaces."""
    _, out, _ = run(subscriber, capsys, "-c", "child", "--plan", "all")
    assert "plan of child.yml (--config)" not in out
    assert "run config child.yml (--config): 2 step(s)" in out


def test_a_bare_run_with_an_empty_plan_is_still_refused_and_names_all(subscriber, capsys):
    code, _, err = run(subscriber, capsys, "-c", "default")
    assert code == 2 and "nothing to run: give steps, `all`," in err


# ── `all` is a bare run: the whole plan, strictly ────────────────────────────

def test_all_is_strict_like_a_bare_run(subscriber, capsys):
    for args in (["--plan"], ["--plan", "all"]):
        code, _, err = run(subscriber, capsys, "-c", "strict", *args)
        assert code == 2 and "plan.detours[0]: no step 'three'" in err, (args, err)


def test_control_other_steps_stay_lenient(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, "-c", "strict", "--plan", "one")
    assert code == 0 and "detour at 'three' dropped: not in this run" in out


def test_the_fallback_carries_the_plans_other_keys(subscriber):
    prog = compile_(subscriber, "lean", ["all"])
    assert [s.id for s in prog.main] == ["one", "two", "four", "three"]
    assert prog.params == {"k": "v"}
    assert prog.source == "all = path full (lean.yml has no plan)"


def test_all_on_an_empty_plan_without_a_fallback_is_refused(subscriber):
    with pytest.raises(ConfigError, match=r"default.yml has no plan, and the runner config names no fallback"):
        compile_(subscriber, "default", ["all"], RUNNER.replace("all: {fallback: full}\n", ""))


# ── `all` is never a step ────────────────────────────────────────────────────

@pytest.mark.parametrize("config", ["nested", "ranged"])
def test_all_inside_a_plan_is_refused(subscriber, capsys, config):
    code, out, err = run(subscriber, capsys, "-c", config, "--plan")
    assert code == 2 and "`all` means the run config's plan itself" in err and "name the path 'full'" in err, err
    assert "steps:" not in out


def test_all_inside_a_path_is_refused(subscriber, capsys):
    subscriber.write("testrunner.yml", RUNNER.replace("full: [one, two, three]", "full: [one, all]"))
    code, _, err = run(subscriber, capsys, "-c", "default", "--plan", "full")
    assert code == 2 and "paths.full: `all` means the run config's plan itself" in err


def test_all_with_other_steps_is_refused(subscriber, capsys):
    code, _, err = run(subscriber, capsys, "-c", "child", "--plan", "all", "two")
    assert code == 2 and "`all` stands alone" in err


@pytest.mark.parametrize("runner,msg", [
    (RUNNER.replace("full:", "all:").replace("all: {fallback: full}\n", ""), "a path may not be named `all`"),
    (RUNNER.replace("{fallback: full}", "{fallback: nowhere}"), "all.fallback 'nowhere' is not in paths"),
    (RUNNER.replace("{fallback: full}", "{fallback: full, extra: 1}"), "all takes only fallback"),
])
def test_runner_config_all_is_checked(sub, runner, msg):
    sub.write("testrunner.yml", runner)
    with pytest.raises(ConfigError, match=msg):
        load_runner_config(sub.root / "testrunner.yml")


def test_an_action_named_all_is_refused(subscriber, capsys):
    subscriber.write("mod.py", MOD + "@action(name='all')\ndef everything(): pass\n")
    code, _, err = run(subscriber, capsys, "-c", "child", "--plan", "one")
    assert code == 2 and "an action may not be named `all`" in err


# ── the source travels: scenario snapshot, journal, report ───────────────────

def test_a_selected_scenario_keeps_the_plans_source(subscriber, capsys):
    """The trigger: `--select` a config that inherits its plan, then `all` joins the scenario."""
    assert run(subscriber, capsys, "-c", "child", "--select")[0] == 0
    code, out, _ = run(subscriber, capsys, "--plan", "all")
    assert code == 0 and walk(out) == ["one", "four"]
    assert "steps: all = plan of base.yml (via child.yml)" in out


def test_the_source_reaches_the_journal_and_the_report(subscriber, capsys):
    code, out, err = run(subscriber, capsys, "-c", "child", "all")
    assert code == 0, out + err
    events = [json.loads(line) for line in (subscriber.root / "state" / "journal.jsonl").read_text().splitlines()]
    start = next(e for e in events if e["event"] == "run_start")
    assert start["source"] == "all = plan of base.yml (via child.yml)" and start["steps"] == ["one", "four"]
    code, out, _ = run(subscriber, capsys, "--report-of", "last")
    assert code == 0
    report = (subscriber.root / out.strip().splitlines()[0]).read_text()   # an absolute path stays absolute
    assert "| steps | all = plan of base.yml (via child.yml) |" in report
