"""The declared state machine (WP testrunner-state-validation, D1/D5, user 2026-10-27): the
runner config declares every transition, the actions claim the same ones, and a plan where the
two disagree is UNDECIDED and refused. Each refusal has its control: the agreeing machine loads."""
import pytest

from testrunner.config import ConfigError, load_runner_config
from testrunner.discovery import collect

RUNNER = """
modules: [m.py]
states:
  rig:
    initial: absent
    validate: {outcome: true}
    values:
      absent:    {criterion: "no machine exists"}
      up:        {criterion: "every machine runs"}
      installed: {}
    transitions:
@T@
    model_only: true
"""
AGREED = """\
      - {from: absent, to: up, by: up, on: passed}
      - {from: up, to: installed, by: install, on: passed}
      - {from: "*", to: absent, by: destroy, on: passed}"""
MOD = """
from testrunner import action
@action(produces={"rig": {"absent": "up"}})
def up(): pass
@action(produces={"rig": {"up": "installed"}})
def install(): pass
@action(produces={"rig": "absent"})
def destroy(): pass
"""


def load(sub, transitions=AGREED, mod=MOD, runner=RUNNER):
    sub.write("testrunner.yml", runner.replace("@T@", transitions))
    sub.write("m.py", mod)
    rc = load_runner_config(sub.root / "testrunner.yml")
    return rc, collect(rc)


def test_control_an_agreeing_machine_loads_with_its_criteria(sub):
    rc, _ = load(sub)
    sv = rc.states["rig"]
    assert sv.values == ["absent", "up", "installed"] and sv.initial == "absent"
    assert sv.criteria == {"absent": "no machine exists", "up": "every machine runs"}
    assert [t.text() for t in sv.transitions_by("install")] == ["rig: up -> installed by install on passed"]
    assert rc.states["rig"]["values"] == sv.values          # the shape the rest of testrunner reads


def test_a_claim_the_runner_config_does_not_declare_is_refused(sub):
    with pytest.raises(ConfigError, match=r"UNDECIDED(.|\n)*rig: up -> installed by install on passed: claimed by "
                                          r"the action \(m.py:install\), NOT declared"):
        load(sub, AGREED.replace("      - {from: up, to: installed, by: install, on: passed}\n", ""))


def test_a_declared_transition_no_action_claims_is_refused(sub):
    with pytest.raises(ConfigError, match=r"rig: up -> installed by install on passed: declared under "
                                          r"states.rig.transitions, NOT claimed"):
        load(sub, mod=MOD.replace('produces={"rig": {"up": "installed"}}', ""))


def test_the_outcome_must_agree_too(sub):
    with pytest.raises(ConfigError, match=r"by install on passed: claimed(.|\n)*by install on skipped: declared"):
        load(sub, AGREED.replace("by: install, on: passed", "by: install, on: skipped"))


def test_from_any_value_and_from_one_value_are_different_claims(sub):
    with pytest.raises(ConfigError, match=r"rig: \* -> absent by destroy on passed: claimed"):
        load(sub, AGREED.replace('{from: "*", to: absent', "{from: up, to: absent"))


def test_a_declared_transition_by_an_action_that_does_not_exist_is_refused(sub):
    with pytest.raises(ConfigError, match=r"there is no action 'nobody'"):
        load(sub, AGREED + "\n      - {from: installed, to: up, by: nobody, on: passed}")


def test_every_mismatch_is_listed_at_once(sub):
    with pytest.raises(ConfigError) as e:
        load(sub, "      []")
    assert str(e.value).count("NOT declared") == 3


def test_a_transition_must_state_all_four_keys(sub):
    with pytest.raises(ConfigError, match=r"missing on"):
        load(sub, AGREED.replace(", on: passed}", "}", 1))


def test_a_bare_yaml_on_key_is_read_as_on(sub):
    # YAML 1.1 reads an unquoted `on:` as true; the loader takes it as the key it was written as
    rc, _ = load(sub)
    assert {t.on for t in rc.states["rig"].transitions} == {"passed"}


@pytest.mark.parametrize("bad,why", [
    ("{from: up, to: up, by: up, on: passed}", "not a change"),
    ("{from: up, to: booted, by: up, on: passed}", "to 'booted' is not one of"),
    ("{from: gone, to: up, by: up, on: passed}", "from 'gone' is not one of"),
    ("{from: absent, to: up, by: up, on: passed, why: x}", "unknown why"),
])
def test_malformed_transitions_are_refused(sub, bad, why):
    with pytest.raises(ConfigError, match=why):
        load(sub, AGREED + "\n      - " + bad)


def test_a_touched_value_without_a_validator_refuses_the_plan(sub):
    runner = RUNNER.replace("    validate: {outcome: true}\n", "").replace(
        "      up:        {criterion: \"every machine runs\"}",
        "      up:        {criterion: \"every machine runs\", validate: {outcome: true}}")
    with pytest.raises(ConfigError, match=r"value 'absent' has no validator(.|\n)*value 'installed' has no validator"):
        load(sub, runner=runner)


def test_a_value_names_its_own_validator_over_the_variables(sub):
    rc, _ = load(sub, runner=RUNNER.replace('      installed: {}',
                                            '      installed: {validate: {run: ["true"]}}'))
    assert rc.states["rig"].validators["installed"] == {"run": ["true"]}
    assert rc.states["rig"].validators["up"] == {"outcome": True}


def test_a_changing_variable_nothing_reads_must_be_declared_model_only(sub):
    with pytest.raises(ConfigError, match=r"states.rig: nothing reads it"):
        load(sub, runner=RUNNER.replace("    model_only: true\n", ""))


def test_control_a_variable_no_transition_changes_is_a_constant(sub):
    rc, _ = load(sub, "      []", mod="from testrunner import action\n@action()\ndef up(): pass\n",
                 runner=RUNNER.replace("    model_only: true\n", ""))
    assert rc.states["rig"].transitions == []


def test_an_observer_and_model_only_exclude_each_other(sub):
    mod = MOD + "from testrunner import observer\n@observer('rig')\ndef look(): return 'absent'\n"
    with pytest.raises(ConfigError, match=r"has an @observer but is declared model_only"):
        load(sub, mod=mod)
    load(sub, mod=mod, runner=RUNNER.replace("    model_only: true\n", ""))       # control


def test_observe_order_must_name_every_value(sub):
    runner = RUNNER.replace("    model_only: true\n", "    observe: {order: [up, absent]}\n")
    with pytest.raises(ConfigError, match=r"observe.order must list every value exactly once"):
        load(sub, runner=runner)


def test_a_walks_inner_step_claims_as_walk_slash_id(sub):
    sub.write("walks.yml", """
actions:
  bring:
    steps:
      - {id: make, run: "true", produces: {rig: {absent: up}}}
""")
    runner = RUNNER.replace("modules: [m.py]", "modules: [m.py]\naction_files: [walks.yml]")
    t = AGREED + "\n      - {from: absent, to: up, by: bring/make, on: passed}"
    load(sub, t, runner=runner)
    with pytest.raises(ConfigError, match=r"by bring/make on passed: claimed"):
        load(sub, runner=runner)


def test_an_inner_step_that_calls_an_action_carries_that_actions_claims(sub):
    # the called action's claim is checked once, as that action's — not again per caller
    sub.write("walks.yml", """
actions:
  bring:
    steps:
      - {id: make, action: up}
""")
    load(sub, runner=RUNNER.replace("modules: [m.py]", "modules: [m.py]\naction_files: [walks.yml]"))
