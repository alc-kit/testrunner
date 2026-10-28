"""Failure modes: classified, routed, retried (WP testrunner-state-validation §10 B1c; user
2026-10-28): "the outcome of failure(s) needs to be fed into another decision matrix. The
classification of failures from the validator/observer needs to match on one of a number of steps.
Like we can repeat test steps, we can also retry on a number of listed failure modes."

- several end results declared for one (value, action, failure outcome) are its MODES;
- after the failure, every mode's validator is asked in one look: exactly one must hold;
- the plan's rules (outcome + state) are the decision matrix, and every mode must be routed;
- retry on listed modes is `retry: if: {state: {var: [modes]}}`."""
import json

RUNNER = """
modules: [m.py]
states:
  sys:
    values: [absent, up, quorum, split]
    validate: {python: sys-is}
    model_only: true
    machine: |
      [*]    --> absent
      absent --> up     : make [passed]
      absent --> quorum : make [failed]
      absent --> split  : make [failed]
      absent --> absent : make [errored]
      absent --> absent : make [aborted]
      absent --> absent : make [skipped]
      quorum --> up     : make [passed]
      quorum --> quorum : make [failed]
      quorum --> quorum : make [errored]
      quorum --> quorum : make [aborted]
      quorum --> quorum : make [skipped]
      quorum --> up     : fix-quorum [passed]
      quorum --> quorum : fix-quorum [failed]
      quorum --> quorum : fix-quorum [errored]
      quorum --> quorum : fix-quorum [aborted]
      quorum --> quorum : fix-quorum [skipped]
"""
MOD = """
from pathlib import Path
from testrunner import action, validator

HERE = Path(__file__).parent

def knob(name):
    return (HERE / name).read_text().strip() if (HERE / name).exists() else ""

@validator()
def sys_is(claim):
    s = (HERE / "system").read_text().strip() if (HERE / "system").exists() else "absent"
    if knob("ambiguous") and claim.value in ("quorum", "split"):
        return True, "the probe cannot tell quorum from split"
    if knob("unsure-split") and claim.value == "split":
        return None, "the split probe could not reach the node"
    return s == claim.value, f"the system reads {s!r}"

@action(requires={"sys": ["absent", "quorum"]},
        produces={"sys": {"absent": "up", "quorum": "up"}},
        produces_on={"failed": {"sys": {"absent": ["quorum", "split"]}}})
def make():
    n = int(knob("makes") or 0) + 1
    (HERE / "makes").write_text(str(n))
    leaves = (knob("make-leaves") or "up").split()
    (HERE / "system").write_text(leaves[min(n, len(leaves)) - 1])
    if leaves[min(n, len(leaves)) - 1] != "up":
        raise RuntimeError("make failed")

@action(requires={"sys": "quorum"}, produces={"sys": {"quorum": "up"}})
def fix_quorum():
    (HERE / "system").write_text("up")
    (HERE / "fixed").touch()

@action(requires={"sys": "up"})
def use():
    (HERE / "used").touch()
"""
# the decision matrix: quorum is repaired, split ends the test
PLAN = """plan:
  path: [make, use]
  on:
    make:
      - when: {outcome: passed}
        then: continue
      - when: {outcome: failed, state: {sys: quorum}}
        then: [fix-quorum]
      - when: {outcome: failed, state: {sys: split}}
        then: stop
"""


def make_sub(sub, plan=PLAN):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", plan)
    return sub


def stored(sub):
    return json.loads((sub.root / "state" / "state.json").read_text())


def ev(sub, name):
    j = sub.root / "state" / "journal.jsonl"
    return [e for e in (json.loads(x) for x in j.read_text().splitlines()) if e["event"] == name]


def test_a_quorum_failure_is_classified_and_repaired(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "quorum")
    code, out = s.run(capsys=capsys)
    assert code == 0, out
    assert (s.root / "fixed").exists() and (s.root / "used").exists()
    ch = [e for e in ev(s, "state_changed") if e["step"] == "make"]
    assert ch[0]["to"] == "quorum" and ch[0]["note"] == "failure mode quorum (of quorum, split)"


def test_a_split_failure_is_classified_and_routed_to_stop(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "split")
    code, out = s.run(capsys=capsys)
    assert code == 1 and stored(s)["sys"] == "split"          # classified: the state IS split
    assert not (s.root / "fixed").exists() and not (s.root / "used").exists()


def test_a_failure_matching_no_mode_fails_the_test(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "smoke")
    code, out = s.run(capsys=capsys)
    assert code == 1 and "matched NONE of its failure modes" in out
    assert stored(s)["sys"] == "absent"                       # the value from before


def test_two_modes_holding_at_once_fail_the_test(sub, capsys):
    s = make_sub(sub)
    s.write("make-leaves", "quorum")
    s.write("ambiguous", "1")
    code, out = s.run(capsys=capsys)
    assert code == 1 and "2 of its modes hold at once (quorum, split" in out
    assert stored(s)["sys"] == "absent" and not (s.root / "fixed").exists()



def test_a_mode_that_cannot_tell_fails_the_test_even_if_another_holds(sub, capsys):
    # quorum holds, but split could not be looked at: split might be the truth, so the failure
    # cannot be classified, and the test fails (undefined is a failure)
    s = make_sub(sub)
    s.write("make-leaves", "quorum")
    s.write("unsure-split", "1")
    code, out = s.run(capsys=capsys)
    assert code == 1 and "could not be classified: a mode cannot tell" in out
    assert stored(s)["sys"] == "absent" and not (s.root / "fixed").exists()

def test_retry_on_a_listed_failure_mode(sub, capsys):
    plan = """plan:
  path:
    - {action: make, retry: {times: 1, if: {state: {sys: [quorum]}}}}
    - use
  on:
    make:
      - when: {outcome: passed}
        then: continue
      - when: {outcome: failed, state: {sys: quorum}}
        then: [fix-quorum]
      - when: {outcome: failed, state: {sys: split}}
        then: stop
"""
    s = make_sub(sub, plan)
    s.write("make-leaves", "quorum up")                         # fails into quorum, then succeeds
    code, out = s.run(capsys=capsys)
    assert code == 0, out
    assert (s.root / "makes").read_text() == "2" and not (s.root / "fixed").exists()
    assert ev(s, "step_retry")


def test_control_an_unlisted_mode_is_not_retried(sub, capsys):
    plan = """plan:
  path:
    - {action: make, retry: {times: 1, if: {state: {sys: [quorum]}}}}
    - use
  on:
    make:
      - when: {outcome: passed}
        then: continue
      - when: {outcome: failed, state: {sys: quorum}}
        then: [fix-quorum]
      - when: {outcome: failed, state: {sys: split}}
        then: stop
"""
    s = make_sub(sub, plan)
    s.write("make-leaves", "split up")
    code, out = s.run(capsys=capsys)
    assert code == 1 and (s.root / "makes").read_text() == "1" and not ev(s, "step_retry")


def test_an_unrouted_mode_refuses_the_plan(sub, capsys):
    from conftest import main
    plan = PLAN.replace("""      - when: {outcome: failed, state: {sys: split}}
        then: stop
""", "")
    make_sub(sub, plan)
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", "--plan"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code != 0 and "ending failed in failure mode sys=split, which no rule of this step routes" in text


def test_a_mode_list_on_a_passed_claim_is_refused(sub, capsys):
    from conftest import main
    s = make_sub(sub)
    s.write("m.py", MOD.replace('produces={"sys": {"absent": "up", "quorum": "up"}}',
                                'produces={"sys": {"absent": ["up", "quorum"], "quorum": "up"}}'))
    code = main(["--root", str(s.root), "--color", "never", "--non-interactive", "--list"])
    err = capsys.readouterr().err
    assert code != 0 and "a list of end results (failure modes) is only for failed, errored, aborted" in err
