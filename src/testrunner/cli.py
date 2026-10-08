"""Command line, pytest-style: positional arguments select WHAT runs; flags change HOW.

  testrunner                      run the run config's plan
  testrunner up..join verify      run these steps (actions, paths, ranges), in this order
  testrunner --plan [steps]       compile + validate, print the walk; run nothing
  testrunner --list               what the subscriber exports
  testrunner --status             what the store says happened last
  testrunner --scenario           the scenario this directory is committed to
  testrunner --select -c NAME     commit this directory to a scenario, run nothing
  testrunner --release            end the scenario (refused while a run changes state)
  testrunner --report [steps]     run, then write a test report (markdown; --report-format)
  testrunner --report-of RUN      (re)build the report of an earlier run from the journal
  --config NAME|FILE   --with key=value   --color auto|always|never   --root DIR

The first state-changing run in a directory selects its SCENARIO (the resolved run
config); every later or parallel runner there adheres to it — see scenario.py.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import (ConfigError, deep_merge, find_runner_config, load_run_config,
                     load_runner_config, parse_with)
from .discovery import collect
from .fixtures import FixtureError
from .planner import Compiler, initial_state, simulate
from .runner import Runner
from .scenario import describe, new_run_id, select
from .store import LockedError, Store
from .term import Style, color_enabled
from .ui import InputBroker

EXIT_FAILED, EXIT_USAGE = 1, 2


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testrunner", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("steps", nargs="*", help="actions, paths or ranges (a..b); none = the plan")
    p.add_argument("--config", "-c", help="run config name (configs/<name>) or file")
    p.add_argument("--with", "-w", dest="with_", action="append", default=[], metavar="KEY=VALUE",
                   help="override a parameter for this run (repeatable)")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="where to look for testrunner.yml")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--non-interactive", action="store_true",
                   help="never ask the operator; an unanswered prompt fails its step")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="validate and print the walk, run nothing")
    g.add_argument("--list", action="store_true", help="list actions, paths, fixtures, observers")
    g.add_argument("--status", action="store_true", help="the last run, from the store")
    g.add_argument("--scenario", action="store_true", help="show the scenario in force")
    g.add_argument("--select", action="store_true", help="select the scenario, run nothing")
    g.add_argument("--release", action="store_true", help="end the scenario")
    g.add_argument("--report-of", metavar="RUN",
                   help="build the report of an earlier run (a run id, or 'last') from the journal")
    p.add_argument("--report", action="store_true",
                   help="write a test report for this run when it ends (only then is one written)")
    p.add_argument("--report-format", default="md", metavar="md|html|both",
                   help="the report's format (default: md)")
    p.add_argument("--version", action="version", version=f"testrunner {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    style = Style(color_enabled(args.color, sys.stdout))
    try:
        rc = load_runner_config(find_runner_config(args.root))
        store = Store(rc.state_dir)
        if args.status:
            return status(store, style)
        if args.scenario:
            rec = store.get_scenario()
            print(f"scenario: {describe(rec)}" if rec else f"no scenario selected in {store.root}")
            return 0
        if args.release:
            return release(store, style)
        formats = ("md", "html") if args.report_format == "both" else (args.report_format,)
        if args.report_format not in ("md", "html", "both"):
            # refused BEFORE anything runs: a typo must not cost a two-hour run its report
            raise ConfigError(f"--report-format {args.report_format!r}: want md, html or both")
        if args.report_of is not None:
            from . import report
            try:
                for path in report.write(store, None if args.report_of == "last" else args.report_of, formats):
                    print(path)
            except ValueError as e:
                print(style.bad(f"ERROR: {e}"), file=sys.stderr)
                return EXIT_USAGE
            return 0
        reg = collect(rc)
        if args.list:
            return listing(rc, reg)
        run_id = new_run_id()
        if args.select:
            sel = select(rc, store, args.config, claim=True, run_id=run_id)
            for n in sel.notes:
                print(style.warn(f"warning: {n}"))
            print(("selected " if sel.claimed else "already committed to ") + f"scenario: {describe(sel.scenario)}")
            return 0
        compiler = Compiler(rc, reg)
        with_ = parse_with(args.with_)
        sel = select(rc, store, args.config, claim=False, run_id=run_id)
        prog = compiler.compile(sel.run_config.plan, args.steps or None, with_)
        readonly = all(reg.actions[s.action].readonly for s in prog.steps())
        if sel.scenario is None and not args.plan and not readonly and prog.main:
            sel = select(rc, store, args.config, claim=True, run_id=run_id)
            if not sel.claimed:    # a parallel runner selected first: compile against ITS config
                prog = compiler.compile(sel.run_config.plan, args.steps or None, with_)
        run_config = sel.run_config
        for n in sel.notes:
            print(style.warn(f"warning: {n}"))
        if sel.claimed:
            print(style.dim(f"scenario selected: {describe(sel.scenario)}"))
    except (ConfigError, FixtureError) as e:
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    if not prog.main:
        print(style.bad(f"ERROR: nothing to run: give steps, or a run config with a plan "
                        f"({run_config.file or 'no run config found'})"), file=sys.stderr)
        return EXIT_USAGE
    data = deep_merge(run_config.data, {rc.params_section: prog.params})
    v = simulate(rc, reg, prog, initial_state(rc, store.get_state()), data)
    src = f"{run_config.file.name if run_config.file else '(no run config)'} ({run_config.how})"
    for n in prog.notes:
        print(style.dim(f"note: {n}"))
    for w in v.warnings:
        print(style.warn(f"warning: {w}"))
    for e in v.errors:
        print(style.bad(f"error: {e}"))
    if args.plan:
        print(style.banner(f"plan of {src}: {len(v.walk)} step(s) on the expected walk"))
        for i, sid in enumerate(v.walk, 1):
            print(f"  {i:>3}  {sid}")
        for sid, by in prog.reactions.items():
            for outcome, r in by.items():
                tgt = r.target if r.kind == "goto" else ", ".join(s.id for s in prog.segments.get(r.segment or "", []))
                print(f"       on {sid} {outcome}: {r.kind}{' ' + tgt if tgt else ''}")
        return EXIT_USAGE if v.errors else 0
    if v.errors:
        print(style.bad("ERROR: the plan cannot run as written (see above); nothing was started"), file=sys.stderr)
        return EXIT_USAGE
    print(style.banner(f"plan of {src}: {' > '.join(v.walk)}"))
    ui = InputBroker(interactive=False if args.non_interactive else None)
    try:
        runner = Runner(rc, reg, run_config, store, ui, color=args.color, run_id=run_id,
                        readonly=readonly, report_formats=formats if args.report else ())
    except FixtureError as e:
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    try:
        result = asyncio.run(runner.run(prog))
    except LockedError as e:
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print(style.bad("interrupted"), file=sys.stderr)
        return 130
    return 0 if result.passed else EXIT_FAILED


def release(store: Store, style: Style) -> int:
    rec = store.get_scenario()
    if rec is None:
        print(f"no scenario selected in {store.root}")
        return 0
    try:
        with store.lock():
            store.release_scenario()
            store.journal.append("scenario_released", by="--release")
    except LockedError as e:
        print(style.bad(f"ERROR: a state-changing run is in progress; not releasing ({e})"), file=sys.stderr)
        return EXIT_USAGE
    print(f"released scenario: {describe(rec)}")
    return 0


def listing(rc, reg) -> int:
    print("actions:")
    for a in sorted(reg.actions.values(), key=lambda a: a.name):
        contract = []
        if a.requires:
            contract.append(f"requires {a.requires}")
        if a.produces:
            contract.append(f"produces {a.produces}")
        if a.outcomes:
            contract.append(f"outcomes {list(a.outcomes)}")
        print(f"  {a.name:<20} {a.kind:<6} {a.doc}")
        if contract:
            print(f"  {'':<20} {'; '.join(contract)}")
    if rc.paths:
        print("paths:" + (f"  (default: {rc.default_path})" if rc.default_path else ""))
        for name, steps in rc.paths.items():
            print(f"  {name}: {steps}")
    if rc.states:
        print("states:")
        for var, spec in rc.states.items():
            obs = "  (observed)" if var in reg.observers else ""
            print(f"  {var}: {spec['values']}  initial {spec.get('initial')}{obs}")
    if reg.fixtures:
        print("fixtures:")
        for f in reg.fixtures.values():
            print(f"  {f.name:<20} {f.scope:<5} {f.source}")
    return 0


def status(store: Store, style: Style) -> int:
    events = list(store.journal.read())
    starts = [i for i, e in enumerate(events) if e["event"] == "run_start"]
    if not starts:
        print(f"no runs recorded in {store.root}")
        return 0
    last = events[starts[-1]:]
    head = last[0]
    print(style.banner(f"last run started {head['at']}  config {head.get('config') or '-'}"))
    open_step = None
    for e in last[1:]:
        if e["event"] == "step_start":
            open_step = e
        elif e["event"] == "step_end":
            open_step = None
            line = f"  {e['at']}  {e['outcome']:<8} {e['step']}" + (f" — {e['detail']}" if e.get("detail") else "")
            print(style.ok(line) if e["outcome"] == "passed" else style.bad(line) if e["outcome"] in ("failed", "errored", "aborted") else line)
        elif e["event"] == "step_skip":
            print(style.dim(f"  {e['at']}  skipped  {e['step']}"))
        elif e["event"] == "run_end":
            print(style.ok("  PASSED") if e["passed"] else style.bad(f"  FAILED: {e.get('why', '')}"))
    if open_step is not None and last[-1]["event"] != "run_end":
        print(style.warn(f"  {open_step['at']}  STARTED  {open_step['step']} — no end recorded "
                         f"(still running, or the run was killed)"))
    print(f"state: {store.get_state()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
