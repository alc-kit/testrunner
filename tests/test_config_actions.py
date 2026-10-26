"""Extending a walk from a run config without restating it (asked for 2026-10-26: "insert a ping
on all proxmox hosts between join and golden-build ... a step I can either define locally in the
config or pull/reuse from another action/configuration/element", and "can this also be used to
extend a single step?"):

  between: [a, b]            an inserted/repeated step lands right BEFORE b; refused unless a
                             comes earlier on the walk
  actions:                   walk actions a run config defines, inherited through extends:
  uses: [config]             another config's actions only (not its plan, rig or edits)
  extend: <step>, steps:     that step's walk action, for this walk only — inner steps merged by
                             id, new ones appended or placed by after:/before:/between:; the
                             step keeps its id
"""
import pytest

from testrunner import walks
from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler

RUNNER = """
modules: [mod.py]
states:
  s: {values: [a], initial: a}
actions:
  stage:                         # a repo walk action with three inner steps
    steps:
      - {id: a, action: up}
      - {id: b, action: join}
      - {id: c, action: verify}
paths:
  full: [up, ping, join, images, stage, verify]
default_path: full
"""
MOD = """
from testrunner import action
for _n in ("up", "ping", "join", "images", "verify", "probe", "extra"):
    exec(f"@action(name={_n!r})\\ndef _{_n}(): pass")
"""


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [{stages: [full]}]\n")
    return sub


def compile_(sub, config):
    """What the CLI does: collect, select, register the config's actions, compile."""
    rc = load_runner_config(sub.root / "testrunner.yml")
    run = load_run_config(rc, config)
    reg = collect(rc)
    walks.build_config(reg, rc, run.walk_actions())
    return Compiler(rc, reg).compile(run.plan, None, None, run.plan_of, run.data.get("edit")), reg, run


def walk(prog):
    return [s.id for s in prog.main]


def cfg(sub, name, text, extends="default"):
    sub.write(f"configs/{name}.yml", (f"extends: {extends}\n" if extends else "") + text)
    return name


def inner(reg, action):
    return [st["id"] for st in reg.actions[action].walk.steps]


# ── between: ─────────────────────────────────────────────────────────────────

def test_between_places_the_step_right_before_the_second_anchor(subscriber):
    # join .. stage are NOT adjacent (images between): the step lands right before stage
    prog, _, _ = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {insert: [probe], between: [join, stage]}\n"))
    assert walk(prog) == ["up", "ping", "join", "images", "probe", "stage", "verify"]
    assert "+probe between join and stage" in prog.source


def test_between_also_works_for_repeat(subscriber):
    prog, _, _ = compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {repeat: ping, between: [join, stage]}\n"))
    assert walk(prog) == ["up", "ping", "join", "images", "ping#2", "stage", "verify"]


@pytest.mark.parametrize("between, msg", [
    ("[stage, join]", "'stage' does not come before 'join'"),        # the walk no longer matches
    ("[join, join]", "'join' does not come before 'join'"),
    ("[join]", "between: want [<step id>, <step id>]"),
    ("join", "between: want [<step id>, <step id>]"),
    ("[join, nowhere]", "'nowhere' is not a step on the walk"),
])
def test_a_between_that_does_not_hold_is_refused(subscriber, between, msg):
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[")):
        compile_(subscriber, cfg(subscriber, "c", f"edit:\n  - {{insert: [probe], between: {between}}}\n"))


def test_between_with_after_is_refused(subscriber):
    with pytest.raises(ConfigError, match="exactly one of after:, before: or between:"):
        compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {insert: [probe], after: up, between: [join, stage]}\n"))


# ── actions: in a run config ─────────────────────────────────────────────────

PVE_PING = """actions:
  pve-ping:
    doc: ping, then a probe
    steps:
      - {id: guard, action: ping}
      - {id: check, action: probe}
"""


def test_a_config_defines_an_action_and_inserts_it(subscriber):
    prog, reg, run = compile_(subscriber, cfg(subscriber, "c", PVE_PING +
                                               "edit:\n  - {insert: [pve-ping], between: [join, stage]}\n"))
    assert walk(prog) == ["up", "ping", "join", "images", "pve-ping", "stage", "verify"]
    assert reg.actions["pve-ping"].kind == "walk" and inner(reg, "pve-ping") == ["guard", "check"]
    assert reg.actions["pve-ping"].source.startswith("run config c.yml")


def test_a_childs_inherits_its_parents_actions(subscriber):
    cfg(subscriber, "parent", PVE_PING)
    prog, reg, _ = compile_(subscriber, cfg(subscriber, "child", "edit:\n  - {insert: [pve-ping], after: up}\n",
                                            extends="parent"))
    assert walk(prog)[:2] == ["up", "pve-ping"] and "pve-ping" in reg.actions


def test_a_child_replaces_an_inherited_action_by_name_and_says_so(subscriber):
    cfg(subscriber, "parent", PVE_PING)
    _, reg, run = compile_(subscriber, cfg(subscriber, "child",
                                           "actions:\n  pve-ping:\n    steps: [{id: only, action: extra}]\n"
                                           "edit:\n  - {insert: [pve-ping], after: up}\n", extends="parent"))
    assert inner(reg, "pve-ping") == ["only"]
    assert any("'pve-ping': child.yml (own) replaces parent.yml" in n for n in run.notes), run.notes


def test_a_config_action_may_extend_a_repo_walk(subscriber):
    _, reg, _ = compile_(subscriber, cfg(subscriber, "c", "actions:\n  stage-plus:\n    extends: stage\n"
                                                          "    steps: [{id: d, action: extra}]\n"))
    assert inner(reg, "stage-plus") == ["a", "b", "c", "d"]


@pytest.mark.parametrize("name", ["ping", "stage"])       # a python action, a repo walk
def test_a_config_action_may_not_shadow_a_repo_action(subscriber, name):
    with pytest.raises(ConfigError, match=f"action '{name}' is already defined by the repository"):
        compile_(subscriber, cfg(subscriber, "c", f"actions:\n  {name}:\n    steps: [{{id: x, action: extra}}]\n"))


# ── uses: ────────────────────────────────────────────────────────────────────

def test_uses_imports_the_actions_only(subscriber):
    # shared has its own plan and an edit — neither may leak into the config that uses it
    cfg(subscriber, "shared", PVE_PING + "plan: [up]\nedit:\n  - {skip: [ping]}\n", extends=None)
    prog, reg, _ = compile_(subscriber, cfg(subscriber, "c", "uses: [shared]\n"
                                                            "edit:\n  - {insert: [pve-ping], before: verify}\n"))
    assert walk(prog) == ["up", "ping", "join", "images", "stage", "pve-ping", "verify"]
    assert "pve-ping" in reg.actions


def test_uses_a_config_that_does_not_exist_is_refused(subscriber):
    with pytest.raises(ConfigError, match="uses 'nope', which does not exist"):
        compile_(subscriber, cfg(subscriber, "c", "uses: [nope]\n"))


def test_a_uses_loop_is_refused(subscriber):
    cfg(subscriber, "x", "uses: [y]\n", extends=None)
    cfg(subscriber, "y", "uses: [x]\n", extends=None)
    with pytest.raises(ConfigError, match="extends/uses loop"):
        compile_(subscriber, cfg(subscriber, "c", "uses: [x]\n"))


def test_a_joined_scenario_snapshot_still_has_the_actions(subscriber):
    _, _, run = compile_(subscriber, cfg(subscriber, "c", PVE_PING))
    from testrunner.config import RunConfig
    snap = RunConfig(None, "default", run.data)            # what a joining run rebuilds from the record
    assert list(snap.walk_actions()) == ["pve-ping"]


# ── extend: ──────────────────────────────────────────────────────────────────

def test_extend_merges_by_id_appends_and_positions(subscriber):
    prog, reg, _ = compile_(subscriber, cfg(subscriber, "c", """edit:
  - extend: stage
    steps:
      - {id: b, action: extra}                  # existing id: merged (here: its action replaced)
      - {id: z, action: probe}                  # new, no position: appended
      - {id: first, action: probe, before: a}
      - {id: mid, action: probe, after: b}
      - {id: gap, action: probe, between: [mid, c]}
"""))
    st = prog.step("stage")
    assert st.id == "stage" and st.action == "stage+stage"                 # the step keeps its id
    assert inner(reg, "stage+stage") == ["first", "a", "b", "mid", "gap", "c", "z"]
    assert reg.actions["stage+stage/b"].call == "extra"                   # merged into b
    assert inner(reg, "stage") == ["a", "b", "c"]                           # the repo walk untouched
    assert "extended stage (5 inner step(s))" in prog.source


@pytest.mark.parametrize("step, msg", [
    ("{id: b, action: extra, after: a}", "'b' is already an inner step of stage"),
    ("{id: n, action: extra, after: nope}", "'nope' is not an inner step of stage"),
    ("{id: n, action: extra, between: [c, a]}", "'c' does not come before 'a' in stage"),
    ("{id: n, action: extra, after: a, before: c}", "give one of after:, before:, between:"),
])
def test_extend_refuses_what_does_not_hold(subscriber, step, msg):
    with pytest.raises(ConfigError, match=msg):
        compile_(subscriber, cfg(subscriber, "c", f"edit:\n  - extend: stage\n    steps:\n      - {step}\n"))


def test_extend_of_a_python_action_is_refused(subscriber):
    with pytest.raises(ConfigError, match="'ping' is a python action, not a walk action"):
        compile_(subscriber, cfg(subscriber, "c", "edit:\n  - extend: ping\n    steps: [{id: x, action: extra}]\n"))


def test_extend_needs_steps(subscriber):
    with pytest.raises(ConfigError, match="extend needs steps"):
        compile_(subscriber, cfg(subscriber, "c", "edit:\n  - {extend: stage}\n"))
