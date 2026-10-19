"""Walk actions (WP §6b, T16, adopted 2026-10-19): an action written in YAML as a walk of inner
steps, compiled like a plan, so every inner step has a guard, an effect, an outcome classified
from evidence, a transition (continue/fail/ignore/skip/end, steps-then-word), retry, `always`;
params/extends/resolvers make them templates. Each behaviour has its negative control."""
import asyncio
import json

import pytest

from conftest import main
from testrunner import template
from testrunner.config import ConfigError, load_runner_config
from testrunner.discovery import collect

RUNNER = """
modules: [mod.py]
action_files: [walks/*.yml]
states:
  late: {values: [absent, created, installed, joined], initial: absent}
paths:
  full: [grow]
default_path: full
"""
MOD = """
from pathlib import Path
from testrunner import action, resolver

HERE = Path(__file__).parent

def trail(word):
    with open(HERE / "trail", "a") as f:
        f.write(word + "\\n")

@action()
def note(params):
    trail(str(params.get("say", "?")))

@action(readonly=True)
def diag():
    trail("diag")

@action(requires={"late": "joined"})
def use_joined():
    trail("use-joined")

@action(outcomes=["member", "not-member"])
def is_member(params):
    from testrunner import Outcome
    return Outcome("member" if (HERE / "member").exists() else "not-member")

@resolver("hosts")
def hosts(arg):
    return [h for h in ["h1", "h2", "h3"] if not arg or h != arg]
"""
GROW = """
actions:
  grow:
    doc: grow by one node
    params: {node: n1}
    steps:
      - {id: create, run: "echo create >> trail", produces: {late: {absent: created}}}
      - {id: install, action: note, with: {say: "install {param.node}"}, produces: {late: {created: installed}}}
      - {id: join, run: "echo join >> trail", produces: {late: {installed: joined}}}
"""


@pytest.fixture
def s(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [full]\n")
    return sub


def go(sub, capsys, walks: str, *args: str, extra_runner: str = ""):
    sub.write("walks/main.yml", walks)
    if extra_runner:
        sub.write("testrunner.yml", RUNNER + extra_runner)
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    out = capsys.readouterr()
    j = sub.root / "state" / "journal.jsonl"
    events = [json.loads(ln) for ln in j.read_text().splitlines()] if j.exists() else []
    tr = sub.root / "trail"
    return code, out.out + out.err, events, (tr.read_text().split() if tr.exists() else [])


def ends(events):
    return [(e["step"], e["outcome"]) for e in events if e["event"] == "step_end"]


# ── the walk itself ──────────────────────────────────────────────────────────

def test_a_walk_runs_its_inner_steps_in_order_with_namespaced_ids_and_their_effects(s, capsys):
    code, out, ev, trail = go(s, capsys, GROW, "grow")
    assert code == 0, out
    assert trail == ["create", "install", "n1", "join"]      # `install n1`, split
    assert ends(ev) == [("grow/create", "passed"), ("grow/install", "passed"), ("grow/join", "passed"),
                        ("grow", "passed")]
    assert ev[-1]["state"] == {"late": "joined"}
    starts = {e["step"]: e for e in ev if e["event"] == "step_start"}
    assert starts["grow/install"]["action"] == "grow/install"
    assert (s.root / "state" / "logs" / "grow.create.log").exists()     # a file-safe log name


def test_a_failing_inner_step_fails_the_walk_and_says_which(s, capsys):
    code, out, ev, trail = go(s, capsys, GROW.replace('"echo create >> trail"', '"exit 3"'), "grow")
    assert code != 0
    assert ends(ev) == [("grow/create", "failed"), ("grow", "failed")]
    assert "grow/create ended failed, expected passed — exit 3" in out
    assert trail == []                     # nothing after it ran


def test_a_walk_param_comes_from_the_callers_with_over_the_default(s, capsys):
    s.write("configs/default.yml", "plan: {path: [{action: grow, with: {node: n7}}]}\n")
    code, out, ev, trail = go(s, capsys, GROW)
    assert code == 0, out
    assert trail == ["create", "install", "n7", "join"]


# ── outcomes classified from evidence ────────────────────────────────────────

REFUSE = """
actions:
  provoke:
    params: {url: "http://tang.invalid"}
    steps:
      - id: try
        run: "echo 'Tang servers this host does not declare: {param.url}'; exit $(cat rc 2>/dev/null || echo 2)"
        outcomes:
          refused: {exit: nonzero, output: ["does not declare", "{param.url}"]}
        expect: refused
      - {id: after, run: "echo after >> trail"}
"""


def test_a_command_outcome_is_classified_from_evidence_and_expected(s, capsys):
    code, out, ev, trail = go(s, capsys, REFUSE, "provoke")
    assert code == 0, out
    assert ends(ev)[0] == ("provoke/try", "refused")
    assert trail == ["after"]


def test_negative_control_an_acceptance_is_not_a_refusal(s, capsys):
    s.write("rc", "0")
    code, out, ev, trail = go(s, capsys, REFUSE, "provoke")
    assert code != 0
    assert ends(ev)[0] == ("provoke/try", "passed")          # exit 0: the evidence does not hold
    assert ends(ev)[-1] == ("provoke", "failed")
    assert trail == []


def test_a_param_in_an_output_pattern_is_matched_literally(s, capsys):
    # "." in the URL must not match any character: the substituted value is re.escape'd
    walks = REFUSE.replace('{param.url}\'; exit', 'http://tangXinvalid\'; exit')
    code, out, ev, trail = go(s, capsys, walks, "provoke")
    assert code != 0
    assert ends(ev)[0] == ("provoke/try", "failed")


# ── reactions ────────────────────────────────────────────────────────────────

def test_always_runs_after_a_failure_and_the_failure_stays_the_verdict(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: arrange, run: "echo arrange >> trail"}
      - {id: provoke, run: "exit 1"}
      - {id: skipped, run: "echo NOT >> trail"}
      - {id: restore, run: "echo restore >> trail", always: true}
"""
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code != 0
    assert trail == ["arrange", "restore"]
    assert ends(ev)[-1] == ("w", "failed")
    assert "w/provoke ended failed" in [e for e in ev if e["event"] == "step_end"][-1]["detail"]


def test_a_failing_always_step_after_a_good_end_fails_the_walk(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: probe, action: is-member, on: {member: skip, not-member: continue}}
      - {id: work, run: "echo work >> trail"}
      - {id: restore, run: "exit 4", always: true}
"""
    s.write("member", "")
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code != 0
    assert ("w/restore", "failed") in ends(ev)
    assert ends(ev)[-1] == ("w", "failed")


def test_ignore_records_the_outcome_and_goes_on(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: flaky, run: "exit 1", on: {failed: ignore}}
      - {id: next, run: "echo next >> trail"}
"""
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code == 0, out
    assert trail == ["next"]
    assert ends(ev) == [("w/flaky", "failed"), ("w/next", "passed"), ("w", "passed")]
    assert [e["step"] for e in ev if e["event"] == "step_ignored"] == ["w/flaky"]


def test_negative_control_ignoring_a_step_a_later_guard_needs_is_refused_before_anything_runs(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: create, run: "exit 1", produces: {late: created}, on: {failed: ignore}}
      - {id: install, run: "echo install >> trail", requires: {late: created}}
"""
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code != 0
    assert "w/install: requires {'late': 'created'}" in out
    assert trail == [] and not [e for e in ev if e["event"] == "step_start"]


def test_steps_then_fail_run_the_diagnostics_first(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: join, run: "exit 2", on: {failed: [diag, fail]}}
      - {id: never, run: "echo NOT >> trail"}
"""
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code != 0
    assert trail == ["diag"]
    assert ends(ev) == [("w/join", "failed"), ("w/diag", "passed"), ("w", "failed")]


def test_skip_ends_the_walk_skipped_with_its_effect(s, capsys):
    walks = """
actions:
  w:
    produces_on: {skipped: {late: joined}}
    steps:
      - {id: member, action: is-member, on: {member: skip, not-member: continue}}
      - {id: form, run: "echo form >> trail"}
"""
    s.write("member", "")
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code == 0, out
    assert trail == []
    assert ends(ev)[-1] == ("w", "skipped") and ev[-1]["state"] == {"late": "joined"}
    (s.root / "member").unlink()
    (s.root / "state" / "state.json").unlink()
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert trail == ["form"] and ends(ev)[-1] == ("w", "passed")


def test_end_with_a_declared_outcome_reaches_the_outer_plan(s, capsys):
    walks = """
actions:
  w:
    outcomes: [refused]
    steps:
      - {id: try, run: "exit 5", on: {failed: {end: refused}}}
"""
    s.write("configs/default.yml", "plan: {path: [w], expect: {w: {outcome: refused}}}\n")
    code, out, ev, trail = go(s, capsys, walks)
    assert code == 0, out
    assert ends(ev)[-1] == ("w", "refused")


def test_end_with_an_undeclared_outcome_is_refused_at_load(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: try, run: "exit 5", on: {failed: {end: refused}}}
"""
    code, out, _, _ = go(s, capsys, walks, "w")
    assert code != 0 and "does not declare" in out


def test_retry_an_inner_step_on_evidence(s, capsys):
    walks = """
actions:
  w:
    steps:
      - id: apt
        run: "n=$(cat n 2>/dev/null || echo 0); n=$((n+1)); echo $n > n; echo 'Could not get lock'; [ $n -ge 2 ]"
        retry: {times: 2, if: {output: "Could not get lock"}}
"""
    code, out, ev, trail = go(s, capsys, walks, "w")
    assert code == 0, out
    assert ends(ev) == [("w/apt", "failed"), ("w/apt", "passed"), ("w", "passed")]


# ── templates: params, extends, resolvers ────────────────────────────────────

EXTENDS = """
actions:
  install:
    params: {target: all}
    steps:
      - {id: install, action: note, with: {say: "install@{param.target}"}}
  reprovision:
    extends: install
    params: {victim: "{hosts:h1}"}
    steps:
      - {id: install, with: {say: "reprovision@{param.victim}"}}
      - {id: unlock, action: note, with: {say: "unlock"}}
"""


def test_a_list_inside_a_longer_string_is_refused(s, capsys):
    s.write("configs/default.yml", "plan: [install, reprovision]\n")
    code, out, ev, trail = go(s, capsys, EXTENDS.replace('"{hosts:h1}"', '"{hosts:h3}"').replace(
        "reprovision@{param.victim}", "reprovision@{param.victim}"))
    # a list in a longer string is refused (only a WHOLE-string reference may be a list) ...
    assert code != 0 and "only a value that is the WHOLE string may be one" in out


def test_extends_with_a_whole_string_list_and_append(s, capsys):
    walks = """
actions:
  install:
    params: {target: all}
    steps:
      - {id: install, action: note, with: {say: "install@{param.target}"}}
  reprovision:
    extends: install
    params: {victims: "{hosts:h1}"}
    steps:
      - {id: install, with: {say: "{param.victims}"}}
      - {id: unlock, action: note, with: {say: unlock}}
"""
    s.write("configs/default.yml", "plan: [install, reprovision]\n")
    code, out, ev, trail = go(s, capsys, walks)
    assert code == 0, out
    assert trail == ["install@all", "['h2',", "'h3']", "unlock"]


def test_an_unknown_reference_is_refused_when_the_walks_load(s, capsys):
    code, out, _, _ = go(s, capsys, GROW.replace("{param.node}", "{nodez}"), "grow")
    assert code != 0 and "{nodez} is neither a built-in reference" in out


def test_extends_cycle_and_self_call_are_refused(s, capsys):
    cyc = """
actions:
  a: {extends: b, steps: [{run: "true"}]}
  b: {extends: a, steps: [{run: "true"}]}
"""
    code, out, _, _ = go(s, capsys, cyc, "a")
    assert code != 0 and "extends itself" in out
    selfcall = """
actions:
  a: {steps: [{action: b}]}
  b: {steps: [{action: a}]}
"""
    code, out, _, _ = go(s, capsys, selfcall, "a")
    assert code != 0 and "calls itself" in out


# ── contracts and the static check ───────────────────────────────────────────

def test_the_walks_requires_is_derived_and_its_composed_effect_satisfies_the_next_guard(s, capsys):
    s.write("configs/default.yml", "plan: [grow, use-joined]\n")
    code, out, ev, trail = go(s, capsys, GROW, "--plan")
    assert code == 0, out
    code, out, ev, trail = go(s, capsys, GROW)
    assert code == 0, out
    assert trail[-1] == "use-joined"


def test_negative_control_a_walk_that_does_not_reach_the_state_fails_the_static_check(s, capsys):
    s.write("configs/default.yml", "plan: [grow, use-joined]\n")
    walks = GROW.replace(', produces: {late: {installed: joined}}', '')
    code, out, ev, trail = go(s, capsys, walks, "--plan")
    assert code != 0 and "use-joined: requires {'late': 'joined'}" in out


def test_the_matrix_shows_the_inner_steps(s, capsys):
    code, out, _, _ = go(s, capsys, GROW, "grow", "--plan", "--matrix")
    assert code == 0, out
    assert "  grow/create" in out and "  grow/join" in out


def test_a_plan_cannot_name_an_inner_step_nor_use_a_walk_reaction(s, capsys):
    code, out, _, _ = go(s, capsys, GROW, "grow/create")
    assert code != 0 and "inner step of the walk action 'grow'" in out
    s.write("configs/default.yml", "plan: {path: [grow], on: {grow: {failed: ignore}}}\n")
    code, out, _, _ = go(s, capsys, GROW)
    assert code != 0 and "reaction of a walk action's inner steps" in out


def test_one_name_one_action(s, capsys):
    code, out, _, _ = go(s, capsys, GROW.replace("  grow:", "  note:"), "note")
    assert code != 0 and "one name, one action" in out
    s.write("walks/other.yml", GROW)
    code, out, _, _ = go(s, capsys, GROW, "grow")
    assert code != 0 and "defined twice" in out


def test_list_shows_walks_but_never_inner_steps(s, capsys):
    code, out, _, _ = go(s, capsys, GROW, "--list")
    assert code == 0, out
    assert "grow" in out and "grow/create" not in out


# ── the template grammar ─────────────────────────────────────────────────────

def r(text, values, escape=None):
    async def resolve(name, arg):
        v = values[name]
        return v(arg) if callable(v) else v
    return asyncio.run(template.render(text, resolve, escape))


def test_template_whole_string_keeps_the_type_and_text_joins():
    assert r("{a}", {"a": [1, 2]}) == [1, 2]
    assert r("x-{a}-y", {"a": 3}) == "x-3-y"
    assert r("{{literal}} {a}", {"a": "v"}) == "{literal} v"
    assert r("{f:{a}.x}", {"a": "h1", "f": lambda arg: arg.upper()}) == "H1.X"
    assert r("^{a}$", {"a": "a.b"}, escape=__import__("re").escape) == r"^a\.b$"
    assert r("{a}", {"a": "a.b"}, escape=__import__("re").escape) == r"a\.b"
    assert r({"k": ["{a}"]}, {"a": "v"}) == {"k": ["v"]}


@pytest.mark.parametrize("bad", ["{unclosed", "a}b", "{9x}", "{}"])
def test_template_refuses_malformed(bad):
    with pytest.raises(template.TemplateError):
        r(bad, {})


def test_template_refuses_none_inside_text():
    with pytest.raises(template.TemplateError):
        r("x {a}", {"a": None})
