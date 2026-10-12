"""Retry, conditioned on gathered evidence (approved 2026-10-12): after an attempt that did not
end as the plan expected (and was not aborted, nor skipped), the step runs again when ALL of
`if` hold and NONE of `unless` match, at most `times` more, `delay` seconds apart. Evidence:
outcome, detail (regex), output (regex on what the attempt printed), the OBSERVED state, and a
read-only probe action. Every decision is journaled with the evidence that drove it."""
import json
import time

import pytest

from conftest import main
from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler

RUNNER = """
modules: [mod.py]
states:
  s: {values: [absent, up], initial: absent}
paths:
  full: [flaky, after]
  defined: [{action: flaky, retry: {times: 2}}, after]
default_path: full
"""
MOD = """
from pathlib import Path
from testrunner import action, observer

HERE = Path(__file__).parent

def knob(name, default=""):
    p = HERE / name
    return p.read_text().strip() if p.exists() else default

@observer("s")
def look():
    return "up" if (HERE / "system").exists() else "absent"

@action()
async def flaky(proc):
    n = int(knob("count", "0")) + 1
    (HERE / "count").write_text(str(n))
    await proc.run(["sh", "-c", "echo " + knob("say", "E: Could not get lock /var/lib/dpkg/lock")])
    if n <= int(knob("fails", "0")):
        raise RuntimeError(knob("detail", "apt failed"))

@action()
def after():
    pass

@action(readonly=True)
def probe_ok():
    if not (HERE / "probe-passes").exists():
        raise RuntimeError("the mirror does not answer")

@action()
def not_readonly():
    pass
"""


@pytest.fixture
def subscriber(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("configs/default.yml", "plan: [full]\n")
    return sub


def knobs(sub, **kw):
    for k, v in kw.items():
        sub.write(k.replace("_", "-"), str(v))


def run(sub, capsys, edit="", *args):
    sub.write("configs/c.yml", "extends: default\n" + edit)
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", "-c", "c", *args])
    out = capsys.readouterr().out
    events = [json.loads(ln) for ln in (sub.root / "state" / "journal.jsonl").read_text().splitlines()] \
        if (sub.root / "state" / "journal.jsonl").exists() else []
    return code, out, events


def ends(events, step="flaky"):
    return [(e.get("attempt"), e["outcome"]) for e in events if e["event"] == "step_end" and e["step"] == step]


def decisions(events):
    return [(e["event"], e["why"]) for e in events if e["event"] in ("step_retry", "step_no_retry")]


RETRY2 = "edit:\n  - retry: flaky\n    times: 2\n"


# ── retrying ─────────────────────────────────────────────────────────────────

def test_a_failing_step_is_retried_until_it_passes(subscriber, capsys):
    knobs(subscriber, fails=2)
    code, out, ev = run(subscriber, capsys, RETRY2)
    assert code == 0, out
    assert ends(ev) == [("1/3", "failed"), ("2/3", "failed"), ("3/3", "passed")]
    assert [d[1] for d in decisions(ev)] == ["retry 2/3: outcome failed", "retry 3/3: outcome failed"]
    assert "flaky attempt 3/3: passed" in out


def test_retries_used_up_the_run_fails_and_says_so(subscriber, capsys):
    knobs(subscriber, fails=9)
    code, out, ev = run(subscriber, capsys, RETRY2)
    assert code == 1 and len(ends(ev)) == 3
    assert decisions(ev)[-1] == ("step_no_retry", "no retry: attempt 3/3 was the last (failed)")


def test_control_without_retry_one_attempt(subscriber, capsys):
    knobs(subscriber, fails=1)
    code, _, ev = run(subscriber, capsys, "")
    assert code == 1 and ends(ev) == [(None, "failed")] and decisions(ev) == []


def test_retry_set_on_the_step_in_a_path(subscriber, capsys):
    knobs(subscriber, fails=1)
    subscriber.write("configs/default.yml", "plan: [defined]\n")
    code, _, ev = run(subscriber, capsys, "")
    assert code == 0 and ends(ev) == [("1/3", "failed"), ("2/3", "passed")]


# ── evidence ─────────────────────────────────────────────────────────────────

def test_output_evidence_matches(subscriber, capsys):
    knobs(subscriber, fails=1)
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {output: 'Could not get lock'}\n")
    assert code == 0 and "output matched 'Could not get lock'" in decisions(ev)[0][1]


def test_output_evidence_does_not_match_no_retry(subscriber, capsys):
    knobs(subscriber, fails=1, say="E: Unable to locate package nope")
    code, out, ev = run(subscriber, capsys, RETRY2 + "    if: {output: 'Could not get lock'}\n")
    assert code == 1 and len(ends(ev)) == 1
    assert decisions(ev) == [("step_no_retry", "no retry after attempt 1/3 (failed): outcome failed; "
                                               "output did not match 'Could not get lock'")]
    assert "no retry after attempt 1/3" in out


def test_detail_evidence(subscriber, capsys):
    knobs(subscriber, fails=1, detail="Temporary failure resolving deb.debian.org")
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {detail: 'Temporary failure resolving'}\n")
    assert code == 0 and "detail matched" in decisions(ev)[0][1]


def test_observed_state_evidence(subscriber, capsys):
    knobs(subscriber, fails=1)
    # the system is absent (observed), the retry wants it up: no retry
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {state: {s: up}}\n")
    assert code == 1 and "observed state is not {'s': 'up'}" in decisions(ev)[0][1]


def test_control_observed_state_holds(subscriber, capsys):
    knobs(subscriber, fails=1, system="")
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {state: {s: up}}\n")
    assert code == 0 and "observed state is {'s': 'up'}" in decisions(ev)[0][1]


def test_probe_that_passes_allows_the_retry_and_is_journaled(subscriber, capsys):
    knobs(subscriber, fails=1, probe_passes="")
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {probe: probe-ok}\n")
    assert code == 0 and "probe probe-ok passed" in decisions(ev)[0][1]
    assert ("flaky~probe1", "passed") in [(e["step"], e["outcome"]) for e in ev if e["event"] == "step_end"]


def test_probe_that_fails_prevents_the_retry(subscriber, capsys):
    knobs(subscriber, fails=1)
    code, _, ev = run(subscriber, capsys, RETRY2 + "    if: {probe: probe-ok}\n")
    assert code == 1 and "probe probe-ok failed" in decisions(ev)[0][1] and len(ends(ev)) == 1


def test_unless_vetoes(subscriber, capsys):
    knobs(subscriber, fails=1, say="No space left on device")
    code, _, ev = run(subscriber, capsys, RETRY2 + "    unless: {output: 'No space left'}\n")
    assert code == 1 and decisions(ev) == [("step_no_retry", "no retry after attempt 1/3: unless output matched "
                                                             "'No space left'")]


def test_control_unless_not_matching_lets_it_retry(subscriber, capsys):
    knobs(subscriber, fails=1)
    code, _, ev = run(subscriber, capsys, RETRY2 + "    unless: {output: 'No space left'}\n")
    assert code == 0 and len(ends(ev)) == 2


def test_a_step_that_ended_as_expected_is_never_retried(subscriber, capsys):
    knobs(subscriber, fails=9)
    subscriber.write("configs/default.yml", "plan:\n  path: [full]\n  expect: {flaky: {outcome: failed}}\n")
    code, _, ev = run(subscriber, capsys, RETRY2)
    assert code == 0 and len(ends(ev)) == 1 and decisions(ev) == []


def test_the_delay_is_waited(subscriber, capsys):
    knobs(subscriber, fails=1)
    t = time.monotonic()
    code, _, ev = run(subscriber, capsys, "edit:\n  - {retry: flaky, times: 1, delay: 0.3}\n")
    assert code == 0 and time.monotonic() - t >= 0.3
    assert next(e for e in ev if e["event"] == "step_retry")["delay"] == 0.3


def test_the_edit_is_named_in_the_step_source(subscriber, capsys):
    code, out, _ = run(subscriber, capsys, RETRY2, "--plan")
    assert code == 0 and "edits: retry flaky x2" in out


def test_the_report_shows_each_attempt(subscriber, capsys):
    knobs(subscriber, fails=1)
    run(subscriber, capsys, RETRY2)
    code = main(["--root", str(subscriber.root), "--color", "never", "--report-of", "last"])
    path = capsys.readouterr().out.strip().splitlines()[0]
    report = (subscriber.root / path).read_text()
    assert code == 0 and "| flaky (attempt 1/3) |" in report and "| flaky (attempt 2/3) |" in report
    assert "### ❌ flaky (attempt 1/3): failed" in report            # the failed attempt, named
    html = main(["--root", str(subscriber.root), "--color", "never", "--report-of", "last", "--report-format", "html"])
    page = (subscriber.root / capsys.readouterr().out.strip().splitlines()[0]).read_text()
    assert html == 0 and "<td>flaky (attempt 2/3)</td>" in page


# ── refused before anything runs ────────────────────────────────────────────

@pytest.mark.parametrize("edit,msg", [
    ("  - {retry: flaky, times: 0}\n", r"retry.times: want a whole number >= 1"),
    ("  - {retry: flaky, delay: -1}\n", r"retry.delay: want seconds >= 0"),
    ("  - {retry: flaky, if: {output: '('}}\n", r"retry.if.output: not a regular expression"),
    ("  - {retry: flaky, if: {colour: red}}\n", r"retry.if takes detail, outcome, output, probe, state"),
    ("  - {retry: flaky, if: {probe: nope}}\n", r"if.probe 'nope' is not an action"),
    ("  - {retry: flaky, if: {probe: not-readonly}}\n", r"must be a read-only action"),
    ("  - {retry: flaky, if: {state: {s: sideways}}}\n", r"if.state s='sideways' is not a declared state value"),
    ("  - {retry: flaky, if: {outcome: [odd]}}\n", r"if.outcome \['odd'\]: action 'flaky' never ends so"),
    ("  - {retry: flaky, if: {outcome: [aborted]}}\n", r"an aborted step is never retried"),
    ("  - {retry: nope, times: 1}\n", r"edit\[0\]: retry: step 'nope' is not a step on the walk"),
])
def test_bad_retry_specs_are_refused(subscriber, edit, msg):
    subscriber.write("configs/bad.yml", "extends: default\nedit:\n" + edit)
    rc = load_runner_config(subscriber.root / "testrunner.yml")
    cfg = load_run_config(rc, "bad")
    with pytest.raises(ConfigError, match=msg):
        Compiler(rc, collect(rc)).compile(cfg.plan, None, None, cfg.plan_of, cfg.data.get("edit"))
