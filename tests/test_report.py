"""Timing reports: built from the journal alone, for any run; never fail a run."""
import json

from testrunner.report import render, runs_from_journal
from testrunner.store import Store


def test_no_report_unless_asked(toy, capsys):
    code, out = toy.run(capsys=capsys)
    assert code == 0 and "report:" not in out and not (toy.root / "state/reports").exists()


def test_report_flag_writes_markdown(toy, capsys):
    code, out = toy.run("--report", capsys=capsys)
    assert code == 0 and "report:" in out
    md = (toy.root / "state/reports/latest.md").read_text()
    assert md.startswith("# Test report: default.yml — ✅ PASSED")
    assert "| seed | ✅ passed |" in md and "```mermaid" in md and "| rows |" in md
    assert not (toy.root / "state/reports/latest.html").exists()


def test_markdown_quotes_a_failed_steps_log(toy, capsys):
    toy.write("configs/negative.yml", (toy.root / "configs/negative.yml").read_text()
              .replace("  expect:\n    wrong-rows: {outcome: failed}\n", ""))
    toy.write("actions/noisy.py", """
        from testrunner import action
        @action(requires={"service": "running"})
        async def noisy(proc):
            r = await proc.run("echo LINE-FROM-THE-LOG; printf '\\033[31mred\\033[0m\\n'; exit 4", log="noisy.log")
            return r.ok
        """)
    code, out = toy.run("uninstall", "lifecycle", "noisy", "--report", capsys=capsys)
    md = (toy.root / "state/reports/latest.md").read_text()
    assert code == 1 and "❌ FAILED" in md and "### ❌ noisy: failed" in md
    assert "LINE-FROM-THE-LOG" in md and "\x1b" not in md


def test_bad_report_format_refused_before_running(toy, capsys):
    code, _ = toy.run("--report", "--report-format", "pdf", capsys=capsys)
    assert code == 2 and not (toy.root / "state/journal.jsonl").exists()


def test_html_report_on_request(toy, capsys):
    code, out = toy.run("--report", "--report-format", "html", capsys=capsys)
    assert code == 0
    page = (toy.root / "state/reports/latest.html").read_text()
    for step in ("install", "start", "seed", "verify"):
        assert f">{step}<" in page
    assert "<th>rows</th>" in page                     # step.metric() reached the table
    assert "No earlier passed run" in page             # first run: nothing to compare with


def test_second_run_is_compared_with_the_first(toy, capsys):
    assert toy.run(capsys=capsys)[0] == 0
    assert toy.run("uninstall", capsys=capsys)[0] == 0          # releases the scenario
    assert toy.run("--report", "--report-format", "both", capsys=capsys)[0] == 0
    page = (toy.root / "state/reports/latest.html").read_text()
    # toy steps take under a second: the table carries the earlier median, the chart (which
    # skips sub-second steps as noise) does not
    assert "(n=1)" in page and "No earlier passed run" in page


def test_report_for_an_earlier_run_by_id(toy, capsys):
    toy.run(capsys=capsys)
    first = runs_from_journal(Store(toy.root / "state"))[0].id
    code, out = toy.run("--report-of", first, capsys=capsys)
    assert code == 0 and first in out.strip() and out.strip().endswith(".md")
    code, _ = toy.run("--report-of", "nosuchrun", capsys=capsys)
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
    code, out = toy.run("--report", capsys=capsys)
    assert code == 0 and "no report: disk full" in out


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


# ── state changes and the state machine (WP testrunner-state-validation) ────

def test_both_reports_show_every_state_change_and_what_decided_it(toy, capsys):
    code, out = toy.run("--report", "--report-format", "both", capsys=capsys)
    assert code == 0, out
    md = (toy.root / "state/reports/latest.md").read_text()
    assert "## State changes" in md and "| service | absent | running |" in md      # start and end
    assert "| install | service | absent → installed | passed | outcome (outcome: true) — install ended passed" in md
    assert "## State machine" in md and "### service" in md
    assert "| installed | the service file says installed | outcome: true |" in md
    assert "| * | installed | install | passed |" in md
    assert "read back: observed" in md and "read back: model only" in md
    page = (toy.root / "state/reports/latest.html").read_text()
    assert "<h2>State changes</h2>" in page and "absent → installed" in page
    assert "<h2>State machine</h2>" in page and "the service file says installed" in page


def test_a_variable_that_became_unknown_is_marked_in_the_reports(sub, capsys):
    from test_validate import make_sub
    s = make_sub(sub)
    s.write("unsure", "")
    from conftest import main
    code = main(["--root", str(s.root), "--color", "never", "--non-interactive", "make", "--report",
                 "--report-format", "both"])
    capsys.readouterr()
    assert code == 1
    md = (s.root / "state/reports/latest.md").read_text()
    assert "| make | sys | absent → ❌ unknown | passed |" in md
    assert "up cannot tell (run)" in md and "make (make): cannot determine sys" in md
    assert "| sys | absent | unknown |" in md
    assert "absent → unknown" in (s.root / "state/reports/latest.html").read_text()


def test_control_a_journal_from_before_the_state_machine_renders_without_it(tmp_path):
    # an old journal has no machine and no state events: no empty sections
    store = Store(tmp_path / "state")
    store.journal.append("run_start", run="r1", config="x.yml", steps=["a"])
    store.journal.append("step_start", run="r1", step="a", action="a")
    store.journal.append("step_end", run="r1", step="a", action="a", outcome="passed", seconds=1)
    store.journal.append("run_end", run="r1", passed=True, why="")
    from testrunner.report import render_markdown
    md = render_markdown(runs_from_journal(store))
    assert "## State changes" not in md and "## State machine" not in md
