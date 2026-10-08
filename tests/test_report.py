"""Timing reports: built from the journal alone, for any run; never fail a run."""
import json

from testrunner.report import render, runs_from_journal
from testrunner.store import Store


def test_every_run_writes_a_report_with_its_steps_and_metrics(toy, capsys):
    code, out = toy.run(capsys=capsys)
    assert code == 0 and "timing report:" in out
    latest = toy.root / "state/reports/latest.html"
    page = latest.read_text()
    for step in ("install", "start", "seed", "verify"):
        assert f">{step}<" in page
    assert "<th>rows</th>" in page                     # step.metric() reached the table
    assert "No earlier passed run" in page             # first run: nothing to compare with


def test_second_run_is_compared_with_the_first(toy, capsys):
    assert toy.run(capsys=capsys)[0] == 0
    assert toy.run("uninstall", capsys=capsys)[0] == 0          # releases the scenario
    assert toy.run(capsys=capsys)[0] == 0
    page = (toy.root / "state/reports/latest.html").read_text()
    # toy steps take under a second: the table carries the earlier median, the chart (which
    # skips sub-second steps as noise) does not
    assert "(n=1)" in page and "No earlier passed run" in page


def test_report_for_an_earlier_run_by_id(toy, capsys):
    toy.run(capsys=capsys)
    first = runs_from_journal(Store(toy.root / "state"))[0].id
    code, out = toy.run("--report", first, capsys=capsys)
    assert code == 0 and first in out.strip()
    code, _ = toy.run("--report", "nosuchrun", capsys=capsys)
    assert code == 2


def test_metrics_are_journalled(toy, capsys):
    toy.run(capsys=capsys)
    ends = [json.loads(line) for line in (toy.root / "state/journal.jsonl").read_text().splitlines()
            if '"step_end"' in line]
    assert next(e for e in ends if e["step"] == "seed")["metrics"] == {"rows": 3}


def test_a_broken_report_never_fails_the_run(toy, capsys, monkeypatch):
    import testrunner.report as rep

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rep, "write", boom)
    code, out = toy.run(capsys=capsys)
    assert code == 0 and "no timing report: disk full" in out


def test_unfinished_run_renders_as_running(tmp_path):
    s = Store(tmp_path)
    s.journal.append("run_start", run="r1", config="c.yml")
    s.journal.append("step_start", run="r1", step="a", action="a")
    s.journal.append("step_end", run="r1", step="a", action="a", outcome="passed", seconds=2)
    page = render(runs_from_journal(s))
    assert "running" in page and ">a<" in page


def _fake(store, rid, steps, t0):
    store.journal.append("run_start", run=rid, config="c.yml")
    for i, (sid, sec) in enumerate(steps):
        store.journal.append("step_start", run=rid, step=sid, action=sid)
        store.journal.append("step_end", run=rid, step=sid, action=sid, outcome="passed", seconds=sec)
    store.journal.append("run_end", run=rid, passed=True)


def test_whole_run_compared_only_with_a_run_of_the_same_steps(tmp_path):
    s = Store(tmp_path)
    _fake(s, "a", [("up", 100), ("install", 900)], 0)
    _fake(s, "b", [("up", 100)], 0)                     # a different plan in between
    _fake(s, "c", [("up", 80), ("install", 450)], 0)
    page = render(runs_from_journal(s))
    assert "vs previous run of these steps (a)" in page
    assert "-50%" in page and "-20%" in page             # install and up against their medians


def test_no_whole_run_tile_without_a_matching_earlier_run(tmp_path):
    s = Store(tmp_path)
    _fake(s, "a", [("up", 5)], 0)
    _fake(s, "b", [("up", 80), ("install", 450)], 0)
    assert "vs previous run" not in render(runs_from_journal(s))
