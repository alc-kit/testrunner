"""Timing reports: one self-contained HTML page per run, built from the journal alone.

Everything a report shows is already in the store's journal (step start/end, outcome,
seconds, metrics), so a report can be (re)built for ANY past run — `--report [RUN]` —
and every run writes its own at the end: <state>/reports/<started>-<run>.html, with
reports/latest.html pointing at the newest.

The page: headline numbers; a timeline (when each step ran, coloured by outcome, the
outcome also written out); each step's change against the MEDIAN of earlier runs of
the same step (diverging bars: faster blue, slower red — relative, because step
durations span seconds to tens of minutes); and a table with every number, including
the metrics actions or hooks recorded with step.metric().
No external resources: it opens offline, from a CI artefact, or from a file share.
"""
from __future__ import annotations

import html
import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import Store, atomic_write

HISTORY = 10          # earlier runs a step is compared with (median of the last N)


@dataclass
class StepRun:
    id: str
    action: str
    outcome: str
    start: datetime
    seconds: float
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    id: str
    start: datetime
    config: str = ""
    end: datetime | None = None
    passed: bool | None = None
    why: str = ""
    steps: list[StepRun] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        last = self.end or (self.steps[-1].start if self.steps else self.start)
        if self.end is None and self.steps:
            s = self.steps[-1]
            last = datetime.fromtimestamp(s.start.timestamp() + s.seconds)
        return (last - self.start).total_seconds()


def _t(e: dict) -> datetime:
    """An event's time: the precise `ts` when the journal has it, else `at`."""
    if e.get("ts") is not None:
        return datetime.fromtimestamp(float(e["ts"]), timezone.utc).replace(tzinfo=None)
    return datetime.strptime(e["at"], "%Y-%m-%dT%H:%M:%SZ")


def runs_from_journal(store: Store) -> list[RunRecord]:
    runs: dict[str, RunRecord] = {}
    order: list[str] = []
    current: str | None = None
    open_steps: dict[tuple[str, str], datetime] = {}
    for n, e in enumerate(store.journal.read()):
        ev = e.get("event")
        rid = e.get("run") or current or f"run{n}"
        if ev == "run_start":
            rid = e.get("run") or f"run{n}"
            current = rid
            runs[rid] = RunRecord(rid, _t(e), config=e.get("config", ""))
            order.append(rid)
            continue
        run = runs.get(rid)
        if run is None:
            continue
        if ev == "step_start":
            open_steps[(rid, e["step"])] = (_t(e), e.get("action", e["step"]))
        elif ev == "step_end":
            start, _ = open_steps.pop((rid, e["step"]), (_t(e), None))
            run.steps.append(StepRun(e["step"], e.get("action", e["step"]), e.get("outcome", "?"), start,
                                     float(e.get("seconds") or (_t(e) - start).total_seconds()),
                                     e.get("detail", ""), e.get("metrics") or {}))
        elif ev == "step_skip":
            run.steps.append(StepRun(e["step"], e.get("action", e["step"]), "skipped", _t(e), 0.0,
                                     e.get("why", "")))
        elif ev == "run_end":
            run.end, run.passed, run.why = _t(e), bool(e.get("passed")), e.get("why", "")
    # a step that started and has not ended: still running (or its run was killed)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for (rid, step), (start, action) in open_steps.items():
        run = runs[rid]
        if run.end is None:
            run.steps.append(StepRun(step, action, "running", start, (now - start).total_seconds(),
                                     "no end recorded yet"))
    return [runs[r] for r in order]


def _median_before(runs: list[RunRecord], idx: int, step_id: str) -> tuple[float | None, int]:
    vals = []
    for r in reversed(runs[:idx]):
        for s in r.steps:
            if s.id == step_id and s.outcome == "passed":
                vals.append(s.seconds)
                break
        if len(vals) >= HISTORY:
            break
    return (statistics.median(vals), len(vals)) if vals else (None, 0)


def _dur(sec: float) -> str:
    if sec < 10 and sec != int(sec):
        return f"{sec:.1f}s"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


STATUS = {"passed": "good", "failed": "critical", "errored": "critical", "aborted": "serious",
          "skipped": "muted", "running": "running"}
ICON = {"good": "✓", "critical": "✕", "serious": "!", "warning": "!", "muted": "–", "running": "…"}
NO_CHANGE = 0.01      # under ±1% a step is "unchanged": drawn neutral, not as faster/slower
MIN_SECONDS = 1.0     # shorter steps are not compared: their percentages are noise


def render(runs: list[RunRecord], run_id: str | None = None, title: str = "") -> str:
    if not runs:
        raise ValueError("no runs in the journal")
    idx = len(runs) - 1 if run_id is None else next(
        (i for i, r in enumerate(runs) if r.id == run_id), None)
    if idx is None:
        raise ValueError(f"no run {run_id!r} in the journal")
    run = runs[idx]
    esc = html.escape
    total = run.seconds
    # the whole-run comparison only means something against a run of the SAME steps
    shape = [s.id for s in run.steps]
    prev = next((r for r in reversed(runs[:idx])
                 if r.passed is not None and [s.id for s in r.steps] == shape), None)
    verdict = "running" if run.passed is None else ("passed" if run.passed else "failed")
    vstat = {"passed": "good", "failed": "critical", "running": "muted"}[verdict]
    n_pass = sum(1 for s in run.steps if s.outcome == "passed")

    # ── timeline (SVG) ──
    W, label_w, row_h, bar_h, top = 760, 150, 24, 12, 26
    plot_w = W - label_w - 70
    span = max(total, 1.0)
    H = top + row_h * len(run.steps) + 10
    tick = next(t for t in (30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400) if span / t <= 8)
    svg = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Timeline of {len(run.steps)} steps">']
    t = 0
    while t <= span:
        x = label_w + plot_w * t / span
        svg.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{H - 6}"/>'
                   f'<text class="tick" x="{x:.1f}" y="{top - 10}" text-anchor="middle">{_dur(t) if t else "0"}</text>')
        t += tick
    for i, s in enumerate(run.steps):
        y = top + i * row_h
        off = (s.start - run.start).total_seconds()
        x = label_w + plot_w * off / span
        w = max(plot_w * s.seconds / span, 3)
        st = STATUS.get(s.outcome, "warning")
        tip = esc(json.dumps({"step": s.id, "outcome": s.outcome, "starts": "+" + _dur(off),
                              "took": _dur(s.seconds), **{k: v for k, v in s.metrics.items()}}))
        svg.append(f'<text class="lbl" x="{label_w - 8}" y="{y + bar_h - 1}" text-anchor="end">{esc(s.id)}</text>'
                   f'<rect class="hit" x="{label_w}" y="{y - 4}" width="{plot_w + 70}" height="{row_h}" data-tip="{tip}"/>'
                   f'<rect class="bar s-{st}" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3"/>'
                   f'<text class="val" x="{x + w + 6:.1f}" y="{y + bar_h - 1}">{ICON[st]} {_dur(s.seconds)}</text>')
    svg.append("</svg>")

    # ── change vs median of earlier runs (SVG, diverging) ──
    comp = []
    for s in run.steps:
        if s.outcome != "passed" or s.seconds < MIN_SECONDS:
            continue
        med, n = _median_before(runs, idx, s.id)
        if med and med >= MIN_SECONDS:
            comp.append((s, med, n, (s.seconds - med) / med))
    comp_svg = ""
    if comp:
        lim = max(0.5, min(3.0, max(abs(c[3]) for c in comp)))
        H2 = top + row_h * len(comp) + 10
        mid = label_w + plot_w / 2
        c = [f'<svg class="chart" viewBox="0 0 {W} {H2}" role="img" aria-label="Change against earlier runs">']
        for frac in (-1, -0.5, 0, 0.5, 1):
            x = mid + frac * plot_w / 2
            c.append(f'<line class="{"axis" if frac == 0 else "grid"}" x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{H2 - 6}"/>'
                     f'<text class="tick" x="{x:.1f}" y="{top - 10}" text-anchor="middle">{frac * lim * 100:+.0f}%</text>')
        for i, (s, med, n, ch) in enumerate(comp):
            y = top + i * row_h
            w = min(abs(ch), lim) / lim * plot_w / 2
            x = mid if ch >= 0 else mid - w
            if abs(ch) < NO_CHANGE:
                cls, word = "same", "unchanged"
            else:
                cls = "slower" if ch >= 0 else "faster"
                word = "slower" if ch >= 0 else "faster"
            tip = esc(json.dumps({"step": s.id, "this run": _dur(s.seconds), f"median of {n} earlier": _dur(med),
                                  "change": f"{ch * 100:+.0f}% ({word})"}))
            c.append(f'<text class="lbl" x="{label_w - 8}" y="{y + bar_h - 1}" text-anchor="end">{esc(s.id)}</text>'
                     f'<rect class="hit" x="{label_w}" y="{y - 4}" width="{plot_w + 70}" height="{row_h}" data-tip="{tip}"/>'
                     f'<rect class="bar {cls}" x="{x:.1f}" y="{y}" width="{max(w, 2):.1f}" height="{bar_h}" rx="3"/>'
                     f'<text class="val" x="{(mid + w + 6) if ch >= 0 else (mid - w - 6):.1f}" y="{y + bar_h - 1}" '
                     f'text-anchor="{"start" if ch >= 0 else "end"}">{ch * 100:+.0f}%</text>')
        c.append("</svg>")
        comp_svg = "".join(c)

    # ── table ──
    metric_keys = sorted({k for s in run.steps for k in s.metrics})
    rows = []
    for s in run.steps:
        med, n = _median_before(runs, idx, s.id)
        ch = (f"{(s.seconds - med) / med * 100:+.0f}%"
              if med and med >= MIN_SECONDS and s.seconds >= MIN_SECONDS and s.outcome == "passed" else "")
        st = STATUS.get(s.outcome, "warning")
        rows.append("<tr>" + "".join([
            f"<td>{esc(s.id)}</td>",
            f'<td><span class="pill s-{st}">{ICON[st]}</span> {esc(s.outcome)}</td>',
            f'<td class="num">+{_dur((s.start - run.start).total_seconds())}</td>',
            f'<td class="num">{_dur(s.seconds)}</td>',
            f'<td class="num">{_dur(med) + f" (n={n})" if med else ""}</td>',
            f'<td class="num">{ch}</td>',
            *[f'<td class="num">{esc(str(s.metrics.get(k, "")))}</td>' for k in metric_keys],
            f"<td>{esc(s.detail)}</td>"]) + "</tr>")
    head = "".join(f"<th>{esc(h)}</th>" for h in
                   ["step", "outcome", "start", "took", "median earlier", "change", *metric_keys, "detail"])

    prev_txt = ""
    if prev is not None and prev.seconds > 0 and run.passed is not None:
        d = (total - prev.seconds) / prev.seconds
        prev_txt = (f'<div class="tile"><div class="k">vs previous run of these steps ({esc(prev.id)})</div>'
                    f'<div class="v">{"▲" if d > 0 else "▼"} {abs(d) * 100:.0f}%</div>'
                    f'<div class="k">{_dur(prev.seconds)} then, {"slower" if d > 0 else "faster"} now</div></div>')
    name = Path(run.config).name if run.config else ""
    title = title or f"Run {run.id}"
    legend_t = "".join(f'<span><i class="sw s-{k}"></i>{ICON[k]} {lbl}</span>'
                       for k, lbl in (("good", "passed"), ("critical", "failed / errored"),
                                      ("warning", "named outcome (e.g. degraded)"), ("serious", "aborted"),
                                      ("muted", "skipped"), ("running", "running")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --critical:#d03b3b; --serious:#ec835a; --warning:#fab219; --skip:#c3c2b7;
  --faster:#2a78d6; --slower:#e34948; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --page:#0d0d0d;
  --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --skip:#383835; --faster:#3987e5; --slower:#e66767; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --skip:#383835; --faster:#3987e5; --slower:#e66767; }}
body {{ margin:0; background:var(--page); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:900px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:28px 0 8px; }}
.sub, .k {{ color:var(--ink2); }} .note {{ color:var(--muted); font-size:12px; margin:4px 0 0; }}
.tiles {{ display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }}
.tile {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:10px 14px; min-width:150px; }}
.tile .v {{ font-size:22px; font-weight:600; }} .tile .k {{ font-size:12px; }}
.card {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:12px; overflow-x:auto; }}
svg.chart {{ width:100%; min-width:560px; height:auto; display:block; }}
.grid {{ stroke:var(--grid); stroke-width:1; }} .axis {{ stroke:var(--axis); stroke-width:1; }}
.tick {{ fill:var(--muted); font-size:11px; }} .lbl {{ fill:var(--ink2); font-size:12px; }}
.val {{ fill:var(--ink2); font-size:11px; font-variant-numeric:tabular-nums; }}
.hit {{ fill:transparent; }} .hit:hover + .bar {{ opacity:.8; }}
.s-good {{ fill:var(--good); background:var(--good); }} .s-critical {{ fill:var(--critical); background:var(--critical); }}
.s-serious {{ fill:var(--serious); background:var(--serious); }} .s-warning {{ fill:var(--warning); background:var(--warning); }}
.s-muted {{ fill:var(--skip); background:var(--skip); }}
.faster {{ fill:var(--faster); }} .slower {{ fill:var(--slower); }} .same {{ fill:var(--axis); }}
.s-running {{ fill:var(--surface); stroke:var(--ink2); stroke-width:1.5; stroke-dasharray:4 3;
  background:var(--surface); outline:1.5px dashed var(--ink2); outline-offset:-1.5px; color:var(--ink2) !important; }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; color:var(--ink2); font-size:12px; margin:0 0 8px; }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:5px; vertical-align:-1px; }}
.sw.faster {{ background:var(--faster); }} .sw.slower {{ background:var(--slower); }} .sw.same {{ background:var(--axis); }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th, td {{ text-align:left; padding:5px 8px; border-bottom:1px solid var(--grid); vertical-align:top; }}
th {{ color:var(--ink2); font-weight:600; }} td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.pill {{ display:inline-block; width:16px; height:16px; border-radius:4px; color:#fff; text-align:center;
  font-size:11px; line-height:16px; }}
#tip {{ position:fixed; pointer-events:none; background:var(--surface); color:var(--ink); border:1px solid var(--ring);
  border-radius:8px; padding:6px 9px; font-size:12px; box-shadow:0 4px 14px rgba(0,0,0,.15); display:none; white-space:nowrap; }}
#tip b {{ font-weight:600; }}
</style></head><body><main>
<h1>{esc(title)}</h1>
<div class="sub">{esc(name)} · started {run.start:%Y-%m-%d %H:%M:%S} UTC{(" · " + esc(run.why)) if run.why else ""}</div>
<div class="tiles">
  <div class="tile"><div class="k">result</div><div class="v"><span class="pill s-{vstat}">{ICON[vstat]}</span> {verdict}</div></div>
  <div class="tile"><div class="k">wall time</div><div class="v">{_dur(total)}</div></div>
  <div class="tile"><div class="k">steps passed</div><div class="v">{n_pass} / {len(run.steps)}</div></div>
  {prev_txt}
</div>
<h2>Timeline</h2>
<div class="legend">{legend_t}</div>
<div class="card">{"".join(svg)}</div>
<h2>Each step against earlier runs</h2>
{('<div class="legend"><span><i class="sw faster"></i>faster than the median</span><span><i class="sw slower"></i>slower than the median</span><span><i class="sw same"></i>unchanged (within 1%)</span></div>'
  f'<div class="card">{comp_svg}</div><p class="note">Change against the median of the last {HISTORY} passed runs of the same step; steps under {MIN_SECONDS:.0f}s are not compared. Bars stop at ±{lim * 100:.0f}%; the table has the exact numbers.</p>')
 if comp_svg else '<p class="note">No earlier passed run of these steps in this journal yet.</p>'}
<h2>All numbers</h2>
<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<p class="note">Built from {esc(str(len(runs)))} run(s) in the journal by testrunner.</p>
</main><div id="tip"></div>
<script>
(function () {{
  var tip = document.getElementById("tip");
  document.addEventListener("mousemove", function (e) {{
    var t = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (!t) {{ tip.style.display = "none"; return; }}
    var d = JSON.parse(t.getAttribute("data-tip"));
    tip.textContent = "";
    for (var k in d) {{
      var line = document.createElement("div"), b = document.createElement("b");
      b.textContent = k + " "; line.appendChild(b); line.appendChild(document.createTextNode(String(d[k])));
      tip.appendChild(line);
    }}
    tip.style.display = "block";
    var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + "px"; tip.style.top = (e.clientY + 14) + "px";
  }});
}})();
</script></body></html>
"""


def write(store: Store, run_id: str | None = None) -> Path:
    runs = runs_from_journal(store)
    page = render(runs, run_id)
    run = next(r for r in runs if r.id == run_id) if run_id else runs[-1]
    out = store.path("reports", f"{run.start:%Y%m%dT%H%M%SZ}-{run.id}.html")
    atomic_write(out, page)
    latest = out.parent / "latest.html"
    try:
        tmp = out.parent / ".latest.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(out.name, tmp)
        os.replace(tmp, latest)
    except OSError:
        atomic_write(latest, page)
    return out
