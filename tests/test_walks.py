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
  late:
    values: [absent, created, installed, joined]
    initial: absent
    validate: {outcome: true}
    model_only: true
    transitions: @T@
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
# the transitions GROW's inner steps claim, as the runner config must declare them
GROW_T = """
      - {from: absent, to: created, by: grow/create, on: passed}
      - {from: created, to: installed, by: grow/install, on: passed}
      - {from: installed, to: joined, by: grow/join, on: passed}"""
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
    sub.write("testrunner.yml", RUNNER.replace("@T@", GROW_T))
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [full]\n")
    return sub


def go(sub, capsys, walks: str, *args: str, extra_runner: str = "", transitions: str | None = None):
    """`transitions`: what the runner config declares for `late` — by default what GROW claims;
    a walk that claims something else declares that (or "[]": nothing)."""
    sub.write("walks/main.yml", walks)
    if (walks is not GROW and transitions is None and "produces" not in walks) or transitions == "[]":
        transitions = "[]"
    if extra_runner or transitions is not None:
        sub.write("testrunner.yml", RUNNER.replace("@T@", GROW_T if transitions is None else transitions)
                  + extra_runner)
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
    code, out, ev, trail = go(s, capsys, walks, "w",
                              transitions='\n      - {from: "*", to: created, by: w/create, on: passed}')
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
    t = '\n      - {from: "*", to: joined, by: w, on: skipped}'
    code, out, ev, trail = go(s, capsys, walks, "w", transitions=t)
    assert code == 0, out
    assert trail == []
    assert ends(ev)[-1] == ("w", "skipped") and ev[-1]["state"] == {"late": "joined"}
    (s.root / "member").unlink()
    (s.root / "state" / "state.json").unlink()
    code, out, ev, trail = go(s, capsys, walks, "w", transitions=t)
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
    code, out, ev, trail = go(s, capsys, walks, "--plan", transitions=GROW_T.rsplit("\n", 1)[0])
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
    actions = out.split("\nstates:")[0]               # the actions section; states names inner steps as `by`
    assert "grow" in actions and "grow/create" not in actions
    assert "absent -> created  by grow/create on passed" in out   # the declared transition is shown


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


# ── extension kinds: ext.ansible `playbook`, ext.vagrant `vagrant` (stub binaries) ────────────

EXT_MOD = MOD + '''
from testrunner import fixture
from testrunner.ext.ansible import Ansible
from testrunner.ext.vagrant import Vagrant

@fixture
def ans(proc, run):
    return Ansible(proc, cwd=run.root, inventory=run.root / "inv")

@fixture
def vg(proc, run):
    return Vagrant(proc, cwd=run.root)
'''


@pytest.fixture
def ext(s, tmp_path, monkeypatch):
    import os
    import stat
    d = tmp_path / "bin"
    d.mkdir()
    rec = tmp_path / "calls.txt"
    for name in ("ansible-playbook", "vagrant"):
        p = d / name
        p.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$(basename "$0") $*" >> {rec}\n'
                     'cat "${STUB_SAY:-/dev/null}" 2>/dev/null\nexit "${STUB_RC:-0}"\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    s.write("mod.py", EXT_MOD)
    s.write("testrunner.yml", RUNNER.replace("@T@", "[]") + "ext: {ansible: {}, vagrant: {}}\n")
    s.calls = rec
    return s


PLAY = """
actions:
  install:
    params: {target: [n1, n2], log: install.log}
    steps:
      - id: install
        playbook: playbooks/install.yml
        limit: "{param.target}"
        extra: {encrypt: "true", who: "{param.target}"}
        rules: {file: drive/rules.json, VICTIM: "n2"}
        log: "{param.log}"
  reprovision:
    extends: install
    params: {victim: n2}
    steps:
      - {id: install, limit: ["{param.victim}"], extra: {force: "true"}, log: reprovision.log}
      - {id: create, vagrant: up, machines: ["{param.victim}"], parallel: false}
"""


def test_playbook_and_vagrant_kinds_run_with_rendered_values(ext, capsys):
    ext.write("drive/rules.json", '[{"expect": "victim @VICTIM@\\\\?", "send": "@VICTIM@\\r"}]')
    ext.write("walks/main.yml", PLAY)
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "reprovision"])
    out = capsys.readouterr()
    assert code == 0, out.out + out.err
    calls = ext.calls.read_text().splitlines()
    # `who` IS one reference: the list itself, passed to ansible as JSON (never a Python repr)
    assert calls[0] == f"ansible-playbook playbooks/install.yml -i {ext.root / 'inv'} --limit n2 " \
                       '-e encrypt=true -e {"who": ["n1", "n2"]} -e force=true'
    assert calls[1] == "vagrant up --no-parallel n2"
    assert (ext.root / "state" / "logs" / "reprovision.log").exists()


def test_a_rules_template_that_is_not_filled_completely_errors_the_step(ext, capsys):
    ext.write("drive/rules.json", '[{"expect": "@VICTIM@ @OTHER@", "send": "y\\r"}]')
    ext.write("walks/main.yml", PLAY)
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "install"])
    out = capsys.readouterr().out
    assert code != 0 and "needs OTHER" in out
    assert not ext.calls.exists()                # refused before ansible ran


def test_a_playbook_refusal_is_classified_and_expected(ext, capsys, monkeypatch):
    ext.write("say", "FAILED: Tang servers this host does not declare: http://tang.invalid\n")
    monkeypatch.setenv("STUB_SAY", str(ext.root / "say"))
    monkeypatch.setenv("STUB_RC", "2")
    ext.write("walks/main.yml", """
actions:
  foreign:
    steps:
      - id: provoke
        playbook: playbooks/encrypt.yml
        outcomes: {refused: {exit: nonzero, output: "does not declare: http://tang\\\\.invalid"}}
        expect: refused
""")
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "foreign"])
    out = capsys.readouterr()
    assert code == 0, out.out + out.err
    monkeypatch.setenv("STUB_RC", "0")           # negative control: accepted is not refused
    (ext.root / "state" / "state.json").unlink(missing_ok=True)
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "foreign"])
    assert code != 0


def test_a_kind_without_its_extension_is_refused_at_load(s, capsys):
    code, out, _, _ = go(s, capsys, PLAY, "install")
    assert code != 0 and "an inner step is exactly ONE of" in out and "playbook" in out


# ── ext.rig resolvers (a duck-typed rig: they only use its public API) ──────────────────────

class FakeCatalogue:
    def role1(self, name):
        return {"founder": "n1"}[name]

    def role(self, name):
        return {"victims": ["n2", "n3"]}.get(name, [])


class FakeRig:
    catalogue = FakeCatalogue()
    founder = "n1"

    def late_node(self, params):
        return params.get("late", "n9")

    async def live_nodes(self, vg):
        return ["n1", "n2"]

    async def live_limit(self, vg):
        return await self.live_nodes(vg)


class FakeAns:
    async def hostvar(self, host, expr):
        return {"n1/cluster_name": "c1"}.get(f"{host}/{expr}", "")


def test_rig_resolvers():
    from testrunner.api import OutcomeError
    from testrunner.ext.rig import kinds as k
    rig, ans = FakeRig(), FakeAns()
    run = asyncio.run
    assert run(k.live(rig, None)) == ["n1", "n2"]
    assert k.role("founder", rig) == "n1"
    assert k.roles("victims", rig) == ["n2", "n3"]
    assert run(k.rig_attr("founder", rig, {})) == "n1"
    assert run(k.rig_attr("late_node", rig, {"late": "n4"})) == "n4"
    assert run(k.inventory("n1/cluster_name", ans)) == "c1"
    assert run(k.peer_of("n1", rig, None)) == "n2"
    for bad in (lambda: k.roles("nope", rig), lambda: run(k.rig_attr("_private", rig, {})),
                lambda: run(k.inventory("n1.cluster_name", ans)), lambda: run(k.inventory("n2/x", ans))):
        with pytest.raises(OutcomeError):
            bad()


# ── T-D9: inner steps in edit:, --skip, --from and --resume ─────────────────────────────────

FLAKY = """
actions:
  w:
    steps:
      - {id: first, run: "echo first >> trail"}
      - id: apt
        run: "n=$(cat n 2>/dev/null || echo 0); n=$((n+1)); echo $n > n; echo 'Could not get lock'; [ $n -ge 2 ]"
      - {id: last, run: "echo last >> trail"}
      - {id: cleanup, run: "echo cleanup >> trail", always: true}
"""


def test_edit_retry_reaches_an_inner_step(s, capsys):
    s.write("configs/c.yml", "plan: [w]\nedit: [{retry: w/apt, times: 1, if: {output: 'Could not get lock'}}]\n")
    code, out, ev, trail = go(s, capsys, FLAKY, "-c", "c")
    assert code == 0, out
    assert ends(ev) == [("w/first", "passed"), ("w/apt", "failed"), ("w/apt", "passed"), ("w/last", "passed"),
                        ("w/cleanup", "passed"), ("w", "passed")]
    # negative control: the same walk without the edit fails at w/apt
    import shutil
    shutil.rmtree(s.root / "state")          # a fresh scenario: the claimed one carries the edit (T9)
    for f in ("n", "trail"):
        (s.root / f).unlink(missing_ok=True)
    code, out, ev, trail = go(s, capsys, FLAKY, "w")
    assert code != 0 and ("w/apt", "failed") in ends(ev)


def test_skip_an_inner_step_from_the_command_line(s, capsys):
    code, out, ev, trail = go(s, capsys, GROW, "grow", "--skip", "grow/install")
    assert code == 0, out
    assert trail == ["create", "join"]


def test_only_retry_and_skip_reach_inside_a_walk(s, capsys):
    s.write("configs/c.yml", "plan: [w]\nedit: [{insert: [diag], after: w/first}]\n")
    code, out, _, _ = go(s, capsys, FLAKY, "-c", "c")
    assert code != 0 and "can only be retried or skipped" in out
    s.write("configs/c.yml", "plan: [w]\nedit: [{retry: w/nope}]\n")
    code, out, _, _ = go(s, capsys, FLAKY, "-c", "c")
    assert code != 0 and "has no step 'nope'" in out


def test_resume_starts_inside_the_walk_where_it_stopped_not_at_the_always_step(s, capsys):
    s.write("configs/default.yml", "plan: [w]\n")
    code, out, ev, trail = go(s, capsys, FLAKY)
    assert code != 0
    assert trail == ["first", "cleanup"]                 # cleanup ran (always) after w/apt failed
    (s.root / "trail").unlink()
    code, out, ev, trail = go(s, capsys, FLAKY, "--resume")
    assert code == 0, out
    assert trail == ["last", "cleanup"]                  # w/first NOT re-run; resumed at w/apt


def test_from_an_inner_step(s, capsys):
    code, out, ev, trail = go(s, capsys, GROW, "grow", "--from", "grow/join")
    assert code == 0, out
    assert trail == ["join"]


def test_the_report_shows_a_failed_inner_steps_log(s, capsys):
    walks = GROW.replace('"echo join >> trail"', '"echo the join broke; exit 1"')
    code, out, ev, trail = go(s, capsys, walks, "grow", "--report", "--report-format", "md")
    assert code != 0
    md = (s.root / "state" / "reports" / "latest.md").read_text()
    assert "grow/join" in md and "logs/grow.join.log" in md and "the join broke" in md


def test_config_resolver_and_require_nodes_kind():
    from types import SimpleNamespace
    from testrunner.api import OutcomeError
    from testrunner.ext.rig import kinds as k

    class Rig(FakeRig):
        PREFIX = "MYRIG"
        seen = []

        def e(self, name, params=None, default=None):
            return {"EGRESS": "group"}.get(name, default)

        async def require_nodes(self, vg, stage, *nodes, subset=None):
            self.seen.append((stage, nodes, subset))
            missing = [n for n in nodes if n not in ("n1", "n2")]
            if missing:
                raise OutcomeError("failed", f"{stage} needs {missing[0]}")
            return ["n1", "n2"]
    rig = Rig()
    assert k.config("EGRESS", rig, {}) == "group"
    assert k.config("ISOLATION|0", rig, {}) == "0"
    assert k.config("ISOLATION|", rig, {}) == ""
    with pytest.raises(OutcomeError, match="MYRIG_ISOLATION is unset"):
        k.config("ISOLATION", rig, {})
    step = SimpleNamespace(parent="reprovision", id="reprovision/guard")
    asyncio.run(k.require_nodes({"require_nodes": ["n2"]}, rig, None, {}, step))
    assert rig.seen[-1] == ("reprovision", ("n2",), None)
    with pytest.raises(OutcomeError, match="reprovision needs n9"):
        asyncio.run(k.require_nodes({"require_nodes": "n9"}, rig, None, {}, step))


def test_a_resolved_list_inside_a_limit_is_spliced(ext, capsys):
    ext.write("walks/main.yml", """
actions:
  w:
    params: {nodes: [n1, n2]}
    steps:
      - {id: p, playbook: verify.yml, limit: ["{param.nodes}", cache_hosts]}
""")
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "w"])
    assert code == 0, capsys.readouterr()
    assert "--limit n1,n2,cache_hosts" in ext.calls.read_text()


def test_refuse_fails_by_name_only_when_its_condition_holds(s, capsys):
    walks = """
actions:
  w:
    steps:
      - {id: legacy, refuse: "stage w on a LEGACY rig — refused", when: {params.networking: legacy}}
      - {id: work, run: "echo work >> trail"}
"""
    s.write("configs/default.yml", "params: {networking: legacy}\nplan: [w]\n")
    code, out, ev, trail = go(s, capsys, walks)
    assert code != 0 and "stage w on a LEGACY rig — refused" in out and trail == []
    import shutil
    shutil.rmtree(s.root / "state")
    s.write("configs/default.yml", "params: {networking: sdn}\nplan: [w]\n")    # control
    code, out, ev, trail = go(s, capsys, walks)
    assert code == 0 and trail == ["work"]


def test_require_hosts_refuses_an_empty_group_before_the_playbook(ext, capsys, monkeypatch):
    import os
    import stat
    inv = ext.calls.parent / "bin" / "ansible-inventory"
    inv.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "ansible-inventory $*" >> {ext.calls}\n'
                   'echo \'{"all": {"children": ["web", "empty"]}, "web": {"hosts": ["w1"]}, "empty": {}}\'\n')
    inv.chmod(inv.stat().st_mode | stat.S_IEXEC)
    ext.write("walks/main.yml", """
actions:
  w:
    params: {group: empty}
    steps:
      - {id: guard, require_hosts: "{param.group}", hint: "see ./run.sh membership"}
      - {id: p, playbook: site.yml}
""")
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive", "w"])
    out = capsys.readouterr().out
    assert code != 0 and "targets the group 'empty', which resolves to NO hosts" in out and "membership" in out
    assert "ansible-playbook" not in ext.calls.read_text()
    import shutil
    shutil.rmtree(ext.root / "state")
    ext.write("configs/default.yml", "plan: [{action: w, with: {group: web}}]\n")     # control
    code = main(["--root", str(ext.root), "--color", "never", "--non-interactive"])
    assert code == 0, capsys.readouterr().out
    assert "ansible-playbook site.yml" in ext.calls.read_text()


def test_guard_kinds_are_readonly_so_reality_is_not_reread_after_them(ext):
    import textwrap
    ext.write("walks/main.yml", textwrap.dedent("""
        actions:
          w:
            steps:
              - {id: guard, require_hosts: web}
              - {id: nope, refuse: "nope", when: {params.x: 1}}
              - {id: p, playbook: site.yml}
              - {id: p2, playbook: site.yml, readonly: true}
        """))
    reg = collect(load_runner_config(ext.root / "testrunner.yml"))
    assert reg.actions["w/guard"].readonly and reg.actions["w/nope"].readonly
    assert not reg.actions["w/p"].readonly                     # control: a playbook changes things
    assert reg.actions["w/p2"].readonly                        # a step may say so itself
