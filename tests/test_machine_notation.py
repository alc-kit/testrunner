"""The `machine:` notation (WP testrunner-state-validation D8, build step B2; user 2026-10-28):
Mermaid stateDiagram-v2 lines with UML `action [outcome]` labels, parsed to the same transitions as
the YAML records, which stay valid during the migration window. Everything outside the subset is
refused with its line and the reason; the state x event table is generated from the machine."""
import pytest

from testrunner.config import ConfigError
from testrunner.statemachine import ANY, Transition, parse_states, state_event_table, table_text

VALUES = ["absent", "up", "installed", "founded", "clustered"]

# gate3's machine as merged (proxmox-gate3 testrunner.yml, 2026-10-28): YAML records
GATE3_YAML = {"rig": {"initial": "absent", "values": VALUES, "validate": {"outcome": True}, "transitions": [
    {"from": "absent", "to": "up", "by": "up", "on": "passed"},
    {"from": "up", "to": "installed", "by": "install", "on": "passed"},
    {"from": "installed", "to": "founded", "by": "form", "on": "passed"},
    {"from": "installed", "to": "founded", "by": "form", "on": "skipped"},
    {"from": "founded", "to": "clustered", "by": "join", "on": "passed"},
    {"from": "*", "to": "absent", "by": "destroy", "on": "passed"},
]}}
# the same machine, written out (D8 d: no `*`; the absent -> absent self-loop arrives with B3)
GATE3_MACHINE = """\
stateDiagram-v2
%% gate3's rig, as merged 2026-10-28
[*]       --> absent
absent    --> up        : up [passed]
up        --> installed : install [passed]
installed --> founded   : form [passed]
%% the cluster was already formed
installed --> founded   : form [skipped]
founded   --> clustered : join [passed]
up        --> absent    : destroy [passed]
installed --> absent    : destroy [passed]
founded   --> absent    : destroy [passed]
clustered --> absent    : destroy [passed]
"""


def machine(text, values=VALUES, **extra):
    return parse_states("t.yml", {"rig": {"values": values, "validate": {"outcome": True}, "machine": text, **extra}})


def expanded(sv):
    """Transitions with `*` written out, self-loops dropped (the YAML form never has one)."""
    out = set()
    for t in sv.transitions:
        for v in (sv.values if t.frm == ANY else [t.frm]):
            if v != t.to:
                out.add(Transition(t.var, t.by, t.on, v, t.to))
    return out


def test_gate3s_machine_parses_to_the_same_transitions_as_its_yaml():
    yaml_sv = parse_states("t.yml", GATE3_YAML)["rig"]
    m_sv = machine(GATE3_MACHINE)["rig"]
    assert m_sv.initial == yaml_sv.initial == "absent"
    assert set(m_sv.transitions) == expanded(yaml_sv)
    assert all(t.frm != ANY for t in m_sv.transitions)


def test_control_a_missing_line_is_not_equal():
    m_sv = machine(GATE3_MACHINE.replace("founded   --> clustered : join [passed]\n", ""))["rig"]
    assert set(m_sv.transitions) != expanded(parse_states("t.yml", GATE3_YAML)["rig"])


def test_no_initial_line_means_no_initial_value():
    assert machine("absent --> up : up [passed]\n")["rig"].initial is None


def test_an_inner_step_can_be_the_action():
    sv = machine("absent --> up : build/fetch [passed]\n")["rig"]
    assert sv.transitions[0].by == "build/fetch"


@pytest.mark.parametrize("text,why", [
    ("absent --> up\n", "not a machine line"),                                     # no label
    ("absent --> up : up\n", "not a machine line"),                                # no [outcome]
    ("absent --> up : up passed\n", "not a machine line"),
    ("absent -> up : up [passed]\n", "not a machine line"),                        # not Mermaid's arrow
    ("state rig {\n", "not a machine line"),                                       # composite state
    ("note right of up : x\n", "not a machine line"),
    ("absent --> up : up [passed] %% why\n", "must be on its own line"),
    ("[*] --> absent\n[*] --> up\n", "a second `[*] -->` line"),
    ("[*] --> nowhere\n", "'nowhere' is not one of"),
    ("[*] --> absent : up [passed]\n", "marks the initial value only"),
    ("up --> [*] : destroy [passed]\n", "a transition to `[*]`"),
    ("absent --> nowhere : up [passed]\n", "'nowhere' is not one of"),
    ("absent --> absent : destroy [passed]\n", "a self-loop is not accepted yet"),
    ("absent --> up : up [passed]\nabsent --> up : up [passed]\n", "declared twice"),
    ("up --> absent : addnode [passed] / expected += late\n", "a UML effect"),
    ("absent --> up : up [passed]\nstateDiagram-v2\n", "may only be the first line"),
    ("", "want a block of lines"),
])
def test_refused(text, why):
    with pytest.raises(ConfigError, match=why.replace("[", r"\[").replace("*", r"\*").replace("(", r"\(")):
        machine(text)


def test_one_end_result_per_value_action_and_outcome():
    # two failure modes for one outcome: refused until classification exists (WP step B1c)
    with pytest.raises(ConfigError, match=r"make \[failed\] from 'absent' has 2 end results \(broken, up\)"):
        machine("absent --> up : make [failed]\nabsent --> broken : make [failed]\n",
                values=["absent", "up", "broken"])
    with pytest.raises(ConfigError, match=r"has 2 end results .*\(with the \* line\)"):
        parse_states("t.yml", {"rig": {"values": ["absent", "up", "broken"], "validate": {"outcome": True},
                                       "transitions": [{"from": "*", "to": "broken", "by": "make", "on": "failed"},
                                                       {"from": "absent", "to": "up", "by": "make", "on": "failed"}]}})
    # control: different outcomes, or different values, are separate questions
    machine("absent --> up : make [passed]\nabsent --> broken : make [failed]\n", values=["absent", "up", "broken"])


def test_machine_and_transitions_exclude_each_other():
    with pytest.raises(ConfigError, match="`machine` and `transitions` exclude each other"):
        machine("absent --> up : up [passed]\n", transitions=[])


def test_machine_and_initial_exclude_each_other():
    with pytest.raises(ConfigError, match="`machine` and `initial` exclude each other"):
        machine("[*] --> absent\n", initial="absent")


def test_the_state_event_table():
    sv = parse_states("t.yml", GATE3_YAML)["rig"]
    actions, rows = state_event_table(sv)
    assert actions == ["up", "install", "form", "join", "destroy"]
    assert rows["installed"]["form"] == ["-> founded [passed]", "-> founded [skipped]"]
    assert rows["absent"]["install"] == []                                       # claims nothing
    assert rows["clustered"]["destroy"] == ["-> absent [passed] (any)"]          # `*`, expanded and marked
    text = "\n".join(table_text(sv))
    assert "· = this action claims nothing" in text and "-> up [passed]" in text


def test_a_run_on_a_machine_block_confirms_its_claims(sub, capsys):
    import json
    sub.write("testrunner.yml", """
modules: [m.py]
states:
  sys:
    values: [absent, up]
    validate: {outcome: true}
    model_only: true
    machine: |
      [*]    --> absent
      absent --> up : make [passed]
""")
    sub.write("m.py", """
from testrunner import action

@action(produces={"sys": {"absent": "up"}})
def make():
    pass
""")
    sub.write("configs/default.yml", "params: {}\n")
    code, out = sub.run("make", capsys=capsys)
    assert code == 0, out
    assert json.loads((sub.root / "state" / "state.json").read_text())["sys"] == "up"
    code, out = sub.run("--list", capsys=capsys)
    assert "absent -> up  by make on passed" in out and "-> up [passed]" in out


def test_control_a_machine_that_disagrees_with_the_action_is_refused(sub, capsys):
    # D1 holds for the machine form too: the action claims absent -> up, the machine says up -> absent
    sub.write("testrunner.yml", """
modules: [m.py]
states:
  sys:
    values: [absent, up]
    validate: {outcome: true}
    model_only: true
    machine: |
      [*] --> absent
      up  --> absent : make [passed]
""")
    sub.write("m.py", """
from testrunner import action

@action(produces={"sys": {"absent": "up"}})
def make():
    pass
""")
    sub.write("configs/default.yml", "params: {}\n")
    from conftest import main
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", "--list"])
    err = capsys.readouterr().err
    assert code != 0 and "UNDECIDED" in err
    assert "NOT declared under states.sys.machine" in err      # the message names the form in use
