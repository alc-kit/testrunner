"""Validators confirm every claimed state change (WP testrunner-state-validation §3.2–§3.3,
user 2026-10-27): a claim is committed only when its validator HOLDS; a claim that does not hold,
or a validator that cannot tell, fails the step; after a step that claims nothing (or whose
claim did not hold) the value before and every declared target are asked, and exactly one may
hold, otherwise the variable is UNKNOWN and the step fails with the whole story. After a step the
ACTION ends failed, errored or aborted, only a DECLARED expected end result is checked
(test_failure_end_result.py); with none declared (these tests), no validator is asked and the value
from before is kept. Each rule has its negative control."""
import json

import pytest

from conftest import main
from testrunner.api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS
from testrunner.proc import Result

# The system under test is a file holding its state; `make` writes what the knobs say.
RUNNER = """
modules: [m.py]
states:
  sys:
    initial: absent
    values:
      absent: {validate: @ABSENT@}
      up:     {validate: @UP@}
    transitions:
      - {from: absent, to: up, by: make, on: passed}
      - {from: "*", to: absent, by: wipe, on: passed}
    model_only: true
scenario: {release_when: {sys: absent}}
"""
# exit 0 holds, 1 does not hold, anything else cannot tell
PROBE = '{run: "s=$(cat {run.root}/system 2>/dev/null || echo absent); test -f {run.root}/unsure && exit 3; test $s = %s"}'
MOD = """
from pathlib import Path
from testrunner import action, validator

HERE = Path(__file__).parent

def knob(name):
    return (HERE / name).read_text().strip() if (HERE / name).exists() else ""

@action(produces={"sys": {"absent": "up"}})
def make():
    if knob("make-leaves"):
        (HERE / "system").write_text(knob("make-leaves"))
    else:
        (HERE / "system").write_text("up")
    if knob("make-fails"):
        raise RuntimeError("make failed")

@action(produces={"sys": "absent"})
def wipe():
    (HERE / "system").unlink(missing_ok=True)

@validator()
def sys_is(claim, run):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    return s == claim.value, f"the file says {s} (asked about {claim.value} as {claim.role}, step {claim.step})"
"""


def make_sub(sub, absent=PROBE % "absent", up=PROBE % "up", mod=MOD, runner=RUNNER):
    sub.write("testrunner.yml", runner.replace("@ABSENT@", absent).replace("@UP@", up))
    sub.write("m.py", mod)
    sub.write("configs/default.yml", "params: {}\n")
    return sub


def run(sub, capsys, *steps):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *steps])
    cap = capsys.readouterr()
    out = cap.out + cap.err
    j = sub.root / "state" / "journal.jsonl"
    ev = [json.loads(ln) for ln in j.read_text().splitlines()] if j.exists() else []
    st = sub.root / "state" / "state.json"
    return code, out, ev, (json.loads(st.read_text()) if st.exists() else {})


def events(ev, name):
    return [e for e in ev if e["event"] == name]


# ── a claim is committed only when it holds ─────────────────────────────────

def test_a_confirmed_claim_is_committed_and_journalled_with_its_validator(sub, capsys):
    code, out, ev, state = run(make_sub(sub), capsys, "make")
    assert code == 0, out
    assert state["sys"] == "up"
    ch = events(ev, "state_changed")
    assert len(ch) == 1 and ch[0]["from"] == "absent" and ch[0]["to"] == "up" and ch[0]["claimed_by"] == "make"
    assert ch[0]["confirmed_by"]["kind"] == "run" and ch[0]["confirmed_by"]["answer"] == HOLDS
    assert "sys: absent -> up  (confirmed by run" in out


def test_a_claim_that_does_not_hold_fails_a_passed_step(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "half")                     # passed, but the system is not up
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1
    end = [e for e in events(ev, "step_end") if e["step"] == "make"][0]
    assert end["outcome"] == "failed"
    assert "cannot determine sys" in end["detail"]     # neither absent nor up holds now
    assert "sys=up (claimed): does not hold — run" in end["detail"]
    assert state["sys"] is None and not events(ev, "state_changed")
    assert "scenario released" not in out


def test_a_refuted_claim_where_the_value_before_still_holds_keeps_it(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "absent")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"
    end = [e for e in events(ev, "step_end") if e["step"] == "make"][0]
    assert "claimed sys=up, which did not hold; sys is absent" in end["detail"]


def test_a_validator_that_cannot_tell_fails_the_step_and_leaves_the_variable_unknown(sub, capsys):
    s = make_sub(sub)
    s.write("unsure", "")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] is None
    unk = events(ev, "state_unknown")
    assert unk and unk[0]["var"] == "sys" and unk[0]["was"] == "absent"
    why = unk[0]["why"]
    # the whole story (D6): claim, every answer and why, what is blocked, where to look, what next
    assert "make (make): cannot determine sys — the step ended passed and claimed sys=up; it was absent" in why
    assert "a validator could not tell" in why and "cannot tell — run" in why and "exit 3" in why
    assert "sys is now UNKNOWN" in why and "the scenario is not released" in why
    assert "make.validate.sys.up.log" in why and "next: inspect the system" in why


def test_control_without_a_validator_that_looks_a_passed_claim_is_accepted(sub, capsys):
    s = make_sub(sub, absent="{outcome: true}", up="{outcome: true}")
    s.write("make-leaves", "half")                     # nothing looks: the step's word is taken
    code, out, ev, state = run(s, capsys, "make")
    assert code == 0 and state["sys"] == "up"
    assert events(ev, "state_changed")[0]["confirmed_by"]["kind"] == "outcome"


# ── after a failure, nothing is assumed ─────────────────────────────────────

def test_a_failure_with_no_end_result_declared_keeps_the_value_from_before(sub, capsys):
    # make builds the system, then fails: NOT judged (user 2026-10-28) — the value from before
    # stays, no validator is asked, and the release (decided on what IS) still keeps the scenario
    s = make_sub(sub, absent="{python: sys-is}", up="{python: sys-is}")
    s.write("make-fails", "1")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"
    kept = events(ev, "state_kept")
    assert kept and kept[0]["var"] == "sys" and kept[0]["value"] == "absent" and kept[0]["outcome"] == "failed"
    assert not events(ev, "state_changed") and not events(ev, "state_checked") and not events(ev, "state_unknown")
    assert "scenario released" not in out                # the system IS up: the release look sees it


def test_an_errored_step_with_no_end_result_declared_keeps_it_too(sub, capsys):
    # errored (here: the action returns an outcome it does not declare) is a failure like failed
    mod = MOD.replace("""    if knob("make-fails"):
        raise RuntimeError("make failed")""", """    if knob("make-fails"):
        raise RuntimeError("make failed")
    if knob("make-strays"):
        from testrunner import Outcome
        return Outcome("sideways", "an outcome make does not declare")""")
    s = make_sub(sub, absent="{python: sys-is}", up="{python: sys-is}", mod=mod)
    s.write("make-strays", "1")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"
    kept = events(ev, "state_kept")
    assert kept and kept[0]["outcome"] == "errored" and not events(ev, "state_changed")


def test_control_a_passed_step_is_still_judged(sub, capsys):
    # negative control for the rule above: the same step PASSING is settled by its validator
    s = make_sub(sub, absent="{python: sys-is}", up="{python: sys-is}")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 0 and state["sys"] == "up"
    ch = events(ev, "state_changed")
    assert ch and ch[0]["confirmed_by"]["kind"] == "python" and not events(ev, "state_kept")


def test_a_failure_asks_no_validator_even_one_that_would_hold(sub, capsys):
    # const holds for any value: if a failure were judged, both values would hold at once (the
    # old "2 values hold" unknown). Not judged, it simply keeps the value from before.
    s = make_sub(sub, absent="{const: true}", up="{const: true}")
    s.write("make-fails", "1")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"
    assert not events(ev, "state_unknown") and events(ev, "state_kept")


def test_control_const_confirms_a_passed_claim(sub, capsys):
    code, out, ev, state = run(make_sub(sub, absent="{const: true}", up="{const: true}"), capsys, "make")
    assert code == 0 and state["sys"] == "up"


# ── kinds ──────────────────────────────────────────────────────────────────

def test_a_python_validator_gets_the_claim_and_fixtures(sub, capsys):
    s = make_sub(sub, absent="{python: sys-is}", up="{python: sys-is}")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 0 and state["sys"] == "up"
    assert "the file says up (asked about up as claimed, step make)" in events(ev, "state_changed")[0]["confirmed_by"]["why"]
    s.write("make-leaves", "absent")                    # negative control
    (s.root / "state" / "state.json").unlink()
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"


def test_an_outcome_validator_can_name_a_walks_inner_steps(sub, capsys):
    # the golden-image case: "the build and the fetch did not fail" confirms a PASSED walk's claim
    sub.write("testrunner.yml", """
action_files: [walks.yml]
states:
  sys:
    initial: absent
    values:
      absent: {validate: {outcome: [compile, fetch]}}     # before the step: not both passed
      up:     {validate: {outcome: [compile, fetch]}}
    transitions:
      - {from: absent, to: up, by: build, on: passed}
    model_only: true
""")
    sub.write("walks.yml", """
actions:
  build:
    produces: {sys: {absent: up}}
    steps:
      - {id: compile, run: "true"}
      - {id: fetch, run: "test ! -f fetch-fails"}
      - {id: tidy, run: "test ! -f tidy-fails"}
""")
    sub.write("configs/default.yml", "params: {}\n")
    code, out, ev, state = run(sub, capsys, "build")
    assert code == 0 and state["sys"] == "up", out
    assert events(ev, "state_changed")[0]["confirmed_by"]["kind"] == "outcome"
    (sub.root / "state" / "state.json").unlink()
    sub.write("tidy-fails", "")                         # compile + fetch passed, the walk failed after:
    code, out, ev, state = run(sub, capsys, "build")    # a failed walk with no end result declared keeps the value
    assert code == 1 and state["sys"] == "absent"
    assert events(ev, "state_kept")[-1]["outcome"] == "failed"


def test_validators_that_would_contradict_each_other_are_not_asked_after_a_failure(sub, capsys):
    # absent's validator says "a failed step changed nothing", up's says "compile and fetch
    # passed": after a later failure both would hold (the machine is undefined there). A failure
    # has no declared end result, so neither is asked, and the value from before is kept.
    sub.write("testrunner.yml", """
action_files: [walks.yml]
states:
  sys:
    initial: absent
    values:
      absent: {validate: {outcome: true}}
      up:     {validate: {outcome: [compile, fetch]}}
    transitions:
      - {from: absent, to: up, by: build, on: passed}
    model_only: true
""")
    sub.write("walks.yml", """
actions:
  build:
    produces: {sys: {absent: up}}
    steps:
      - {id: compile, run: "true"}
      - {id: fetch, run: "true"}
      - {id: tidy, run: "false"}
""")
    sub.write("configs/default.yml", "params: {}\n")
    code, out, ev, state = run(sub, capsys, "build")
    assert code == 1 and state["sys"] == "absent"
    assert not events(ev, "state_unknown") and events(ev, "state_kept")


def test_an_outcome_validator_naming_inner_steps_of_a_non_walk_is_refused(sub, capsys):
    code, out, _, _ = run(make_sub(sub, up="{outcome: [compile]}"), capsys, "--list")
    assert code != 0 and "'make' (a transition to or from it) is not a walk action" in out


@pytest.mark.parametrize("bad,why", [
    ("{const: false}", "const takes only true"),
    ("{python: nobody}", "no @validator named 'nobody'"),
    ("{run: x, retries: 2}", "unknown key(s) retries"),
    ("{run: x, const: true}", "want exactly one validator kind"),
    ("{playbook: v.yml}", "want exactly one validator kind"),      # ext.ansible is not enabled
])
def test_a_malformed_validator_is_refused_before_anything_runs(sub, capsys, bad, why):
    code, out, _, _ = run(make_sub(sub, up=bad), capsys, "--list")
    assert code != 0 and why in out


def test_a_validator_template_sees_the_steps_params(sub, capsys):
    s = make_sub(sub, up='{run: "test {param.want} = ok"}')
    s.write("configs/default.yml", "params: {want: ok}\n")
    code, out, ev, state = run(s, capsys, "make")
    assert code == 0 and state["sys"] == "up"
    (s.root / "state" / "state.json").unlink()
    code, out, ev, state = run(s, capsys, "make", "--with", "want=nope")     # negative control
    assert code == 1


def test_a_dry_run_asks_no_validator(sub, capsys):
    s = make_sub(sub)
    s.write("unsure", "")                               # a validator would answer "cannot tell"
    code, out, ev, _ = run(s, capsys, "make", "--dry-run")
    assert code == 0, out
    assert not events(ev, "state_unknown") and not events(ev, "state_changed")


# ── the observer and the validators ─────────────────────────────────────────

OBSERVED = MOD + """
from testrunner import observer
@observer("sys")
def look():
    return (HERE / "seen").read_text().strip() if (HERE / "seen").exists() else (
        (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent")
"""


def test_an_observer_contradicting_a_value_nothing_looked_at_wins_and_fails_the_step(sub, capsys):
    s = make_sub(sub, absent="{outcome: true}", up="{outcome: true}", mod=OBSERVED,
                 runner=RUNNER.replace("    model_only: true\n", ""))
    s.write("seen", "absent")                           # make passes; the observer sees nothing built
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] == "absent"
    assert "the observer reads sys='absent', the model says 'up' — the step's claim did not hold" in out


def test_an_observer_contradicting_a_validator_that_looked_leaves_it_unknown(sub, capsys):
    s = make_sub(sub, mod=OBSERVED, runner=RUNNER.replace("    model_only: true\n", ""))
    s.write("seen", "absent")                           # the validator looked and saw up: two looks disagree
    code, out, ev, state = run(s, capsys, "make")
    assert code == 1 and state["sys"] is None
    assert "two looks disagree" in events(ev, "state_unknown")[0]["why"]


def test_control_an_observer_that_agrees_changes_nothing(sub, capsys):
    s = make_sub(sub, mod=OBSERVED, runner=RUNNER.replace("    model_only: true\n", ""))
    code, out, ev, state = run(s, capsys, "make")
    assert code == 0 and state["sys"] == "up" and not events(ev, "state_unknown")


# ── ext.ansible's kinds: the exit code and the recap give the answer ────────

class FakeAns:
    def __init__(self, rc, text):
        self.r = Result(["ansible"], rc, text.encode())
        self.calls = []

    async def playbook(self, *args, **kw):
        self.calls.append((args, kw))
        return self.r

    async def adhoc(self, *args, **kw):
        self.calls.append((args, kw))
        return self.r


RECAP = "PLAY RECAP\nh1 : ok=2    changed={c}    unreachable=0    failed=0    skipped={s}    rescued=0\n"


@pytest.mark.parametrize("rc,text,answer,why", [
    (0, RECAP.format(c=0, s=0), HOLDS, "exit 0"),
    (2, RECAP.format(c=0, s=0), DOES_NOT_HOLD, "a task failed"),
    (4, "", CANNOT_TELL, "unreachable"),
    (0, RECAP.format(c=1, s=0), CANNOT_TELL, "would CHANGE something"),
    (0, "", CANNOT_TELL, "no PLAY RECAP"),
    (1, "", CANNOT_TELL, "exit 1"),
])
def test_a_playbook_validator_runs_with_check_and_maps_the_exit(rc, text, answer, why):
    import asyncio
    from testrunner.ext.ansible import playbook_validator
    from testrunner.validate import Claim
    ans = FakeAns(rc, text)
    got = asyncio.run(playbook_validator({"playbook": "v.yml"}, ans, Claim("x", "y", "claimed")))
    assert got[0] == answer and why in got[1]
    assert ans.calls[0][0][:2] == ("v.yml", "--check")


@pytest.mark.parametrize("text,answer,why", [
    ("h1 | SUCCESS => {}", HOLDS, "exit 0"),
    ('h1 | SUCCESS => {"skipped": true, "msg": "Command would have run if not in check mode"}', CANNOT_TELL,
     "SKIPPED"),
    ("h1 | CHANGED => {}", CANNOT_TELL, "would CHANGE"),
    ("", CANNOT_TELL, "no host answered"),
])
def test_an_adhoc_validator_that_skipped_under_check_cannot_tell(text, answer, why):
    import asyncio
    from testrunner.ext.ansible import adhoc_validator
    from testrunner.validate import Claim
    ans = FakeAns(0, text)
    spec = {"adhoc": {"pattern": "all", "module": "command", "args": "pveversion"}}
    got = asyncio.run(adhoc_validator(spec, ans, Claim("x", "y", "claimed")))
    assert got[0] == answer and why in got[1]
    assert "--check" in ans.calls[0][0]
