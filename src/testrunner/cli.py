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
  testrunner up..verify -- -vvv --limit n1 -e x=1
                                  everything after `--` goes to the steps' tool, untouched by
                                  testrunner (ext.ansible: every playbook; a --limit narrows
                                  each step's own, it never replaces it)
  --config NAME|FILE   --with key=value   --color auto|always|never   --root DIR
  -v | -vv | --log-level LEVEL   show testrunner's own log (decisions) on stderr
  TESTRUNNER_PROG="$0"       a wrapper names itself (export it in run.sh): this help and every hint use it
  --show-commands      print every command before it runs, as a line you can paste into a
                       shell (also: TESTRUNNER_SHOW_COMMANDS=1)

Every run and --plan also writes its full DEBUG trace — every decision the runner took,
every command it started — to <state_dir>/logs/testrunner-<run>.log (log.py).

The first state-changing run in a directory selects its SCENARIO (the resolved run
config); every later or parallel runner there adheres to it — see scenario.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import re
import shlex
import sys
from pathlib import Path

from . import __version__
from .config import (PROG_ENV, ConfigError, deep_merge, find_runner_config, load_run_config,
                     load_runner_config, parse_with, program_name)
from .discovery import collect
from .fixtures import FixtureError
from .log import ENV_LEVEL, Logs, level_of
from .nolog import Secrets, values_at
from .planner import Compiler, initial_state, matrix, matrix_text, reaction_text, simulate
from .runner import Runner
from .scenario import describe, new_run_id, select
from .store import LockedError, Store
from .term import Style, color_enabled
from .ui import InputBroker
from .validate import spec_text

EXIT_FAILED, EXIT_USAGE = 1, 2

log = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    prog = program_name()
    description, epilog = __doc__.split("\n\n", 1)
    if prog != "testrunner":
        # the examples name the command the operator types; the explanations keep their column
        def example(m: re.Match) -> str:
            line = f"  {prog}{m.group(1)}"
            column = len(m.group(0)) - len(m.group(3))
            return line + " " * max(column - len(line), 2) + m.group(3) if m.group(3) else line
        epilog = re.sub(r"^  testrunner((?: \S+)*?)( {2,}(\S.*))?$",
                        lambda m: example(m) if m.group(2) else f"  {prog}{m.group(1)}", epilog, flags=re.M)
        description += f"\n\n{prog} runs testrunner ({PROG_ENV}={prog})."
    p = argparse.ArgumentParser(prog=prog, description=description,
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=epilog)
    p.add_argument("steps", nargs="*", help="actions, paths or ranges (a..b); none or `all` = the plan")
    p.add_argument("--config", "-c", help="run config name (configs/<name>) or file")
    p.add_argument("--with", "-w", dest="with_", action="append", default=[], metavar="KEY=VALUE",
                   help="override a parameter for this run (repeatable)")
    w = p.add_argument_group("editing the walk for this run (notes, never errors, when a name is not on it)")
    start = w.add_mutually_exclusive_group()
    start.add_argument("--from", dest="from_", metavar="STEP",
                       help="start the walk (the plan, or the steps given) at STEP")
    start.add_argument("--resume", action="store_true",
                       help="start at the step the last run stopped at (refused if it passed)")
    w.add_argument("--skip", action="append", default=[], metavar="STEP", help="leave STEP out (repeatable)")
    w.add_argument("--repeat", action="append", default=[], metavar="STEP@after:ID",
                   help="run STEP again after (or @before:) ID (repeatable)")
    w.add_argument("--insert", action="append", default=[], metavar="ACTION@after:ID",
                   help="insert ACTION after (or @before:) ID (repeatable)")
    w.add_argument("--retry", action="append", default=[], metavar="STEP=TIMES[:DELAY]",
                   help="retry STEP up to TIMES more times on failed/errored (evidence conditions: edit: in a config)")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="where to look for testrunner.yml")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--non-interactive", action="store_true",
                   help="never ask the operator; an unanswered prompt fails its step")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="validate and print the walk, run nothing")
    g.add_argument("--dry-run", action="store_true",
                   help="walk the plan against the real config and state: run only what looks (observers, "
                        "read-only guards), print each command the rest would run; lock, claim and store nothing")
    p.add_argument("--dry-outcome", action="append", default=[], metavar="STEP=OUTCOME",
                   help="with --dry-run: assume STEP (an inner step too: walk/step) ends with OUTCOME, to "
                        "explore that branch (repeatable)")
    p.add_argument("--matrix", action="store_true",
                   help="also print the state/event matrix: per step and outcome, the state in and after, and what "
                        "comes next")
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
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="show testrunner's own log on stderr: -v decisions (INFO), -vv detail (DEBUG)")
    p.add_argument("--log-level", metavar="LEVEL",
                   help=f"the stderr log level (DEBUG, INFO, WARNING, ERROR; beats -v and ${ENV_LEVEL}). "
                        "The trace file is always DEBUG")
    p.add_argument("--show-commands", action="store_true",
                   default=os.environ.get("TESTRUNNER_SHOW_COMMANDS", "").lower() in ("1", "true", "yes", "on"),
                   help="print every command before it runs, as a copy-pasteable shell line "
                        "(cd + environment + argv; also TESTRUNNER_SHOW_COMMANDS=1)")
    p.add_argument("--version", action="version", version=f"testrunner {__version__}")
    return p


def use_config_actions(reg, rc, run_config, style) -> None:
    """Register the run config's own walk actions (`actions:`, `uses:`) before its plan compiles,
    and say what replaced what."""
    from . import walks
    walks.build_config(reg, rc, run_config.walk_actions())
    for n in run_config.notes:
        print(style.dim(f"note: {n}"))


def cli_edits(args: argparse.Namespace) -> list[dict]:
    """The one-run walk edits, as `edit:` entries (applied after the config's)."""
    out: list[dict] = []

    def placed(spec: str, flag: str) -> tuple[str, str, str]:
        name, sep, where = spec.partition("@")
        rel, sep2, anchor = where.partition(":")
        if not (name and sep and sep2 and anchor and rel in ("after", "before")):
            raise ConfigError(f"{flag} {spec!r}: want NAME@after:ID or NAME@before:ID")
        return name, rel, anchor

    for s in args.skip:
        out.append({"skip": s})
    for spec in args.repeat:
        name, rel, anchor = placed(spec, "--repeat")
        out.append({"repeat": name, rel: anchor})
    for spec in args.insert:
        name, rel, anchor = placed(spec, "--insert")
        out.append({"insert": [name], rel: anchor})
    for spec in args.retry:
        name, sep, rest = spec.partition("=")
        times, _, delay = rest.partition(":")
        try:
            out.append({"retry": name, "times": int(times), "delay": float(delay or 0)})
        except ValueError:
            raise ConfigError(f"--retry {spec!r}: want STEP=TIMES or STEP=TIMES:DELAY") from None
        if not (name and sep):
            raise ConfigError(f"--retry {spec!r}: want STEP=TIMES or STEP=TIMES:DELAY")
    return out


def resume_point(store: Store) -> str:
    """The step the last run stopped at: the last step it started (probes aside)."""
    events = list(store.journal.read())
    starts = [i for i, e in enumerate(events) if e["event"] == "run_start"]
    if not starts:
        raise ConfigError(f"--resume: no runs recorded in {store.root}")
    last = events[starts[-1]:]
    end = next((e for e in last if e["event"] == "run_end"), None)
    if end is not None and end.get("passed"):
        raise ConfigError(f"--resume: the last run ({last[0].get('run', '?')}) passed; there is nothing to resume")
    # an `always` step ran BECAUSE the walk stopped earlier: the stop is where to resume
    started = [e["step"] for e in last if e["event"] == "step_start" and "~probe" not in e["step"]
               and not e.get("always")]
    if not started:
        raise ConfigError(f"--resume: the last run ({last[0].get('run', '?')}) started no step")
    why = (end or {}).get("why") or "no end recorded (killed?)"
    log.info("--resume: the last run %s stopped at %s (%s)", last[0].get("run", "?"), started[-1], why)
    return started[-1]


def terminal_level(args: argparse.Namespace) -> int | None:
    """--log-level > -v/-vv > $TESTRUNNER_LOG_LEVEL > off."""
    if args.log_level:
        return level_of(args.log_level)
    if args.verbose:
        return logging.DEBUG if args.verbose > 1 else logging.INFO
    return level_of(os.environ.get(ENV_LEVEL))


def split_forwarded(argv: list[str]) -> tuple[list[str], list[str]]:
    """testrunner's own arguments, and those after the FIRST `--` (for the steps' tool)."""
    if "--" not in argv:
        return argv, []
    i = argv.index("--")
    return argv[:i], argv[i + 1:]


def main(argv: list[str] | None = None) -> int:
    own, forwarded = split_forwarded(list(sys.argv[1:] if argv is None else argv))
    args = parser().parse_args(own)
    args.forwarded = forwarded
    style = Style(color_enabled(args.color, sys.stdout))
    try:
        level = terminal_level(args)
    except ValueError as e:
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    logs = Logs(level)
    try:
        code = _main(args, style, logs)
        log.info("exit %d", code)
        return code
    except BaseException as e:
        if not isinstance(e, (SystemExit, KeyboardInterrupt)):
            log.exception("testrunner stopped on an unexpected error")
            if logs.file:
                print(style.dim(f"trace: {logs.file}"), file=sys.stderr)
        raise
    finally:
        logs.close()


def _main(args: argparse.Namespace, style: Style, logs: Logs) -> int:
    log.info("testrunner %s, python %s, pid %d, cwd %s", __version__, platform.python_version(),
             os.getpid(), Path.cwd())
    log.debug("argv: %s", shlex.join(sys.argv))
    log.debug("parsed: %s", {k: v for k, v in vars(args).items() if v not in (None, False, [], 0)})
    run_id = new_run_id()
    try:
        found = find_runner_config(args.root)
        log.info("runner config %s (searched from %s)", found, args.root)
        rc = load_runner_config(found)
        log.debug("runner config: root %s, state_dir %s, modules %s, pythonpath %s, states %s, "
                  "paths %s, default_path %s, configs %s (env %s, link %s, default %s)",
                  rc.root, rc.state_dir, [str(m) for m in rc.modules], [str(p) for p in rc.pythonpath],
                  {k: v["values"] for k, v in rc.states.items()}, list(rc.paths), rc.default_path,
                  rc.configs_dir, rc.config_env, rc.config_link, rc.default_config)
        store = Store(rc.state_dir)
        if args.forwarded:
            log.info("forwarded to the steps (after --): %s", shlex.join(args.forwarded))
            looks = [f for f, on in (("--status", args.status), ("--scenario", args.scenario), ("--list", args.list),
                                     ("--select", args.select), ("--release", args.release),
                                     ("--report-of", args.report_of is not None)) if on]
            if looks:
                raise ConfigError(f"arguments after `--` are for the steps, and {looks[0]} runs none: "
                                  f"{shlex.join(args.forwarded)}")
        if not (args.status or args.scenario or args.list or args.report_of is not None):
            # everything that runs, plans, selects or releases leaves a trace
            trace = logs.open_file(store.root / "logs", run_id, dry=args.dry_run)
            log.info("run id %s; this trace: %s", run_id, trace)
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
        if args.select:
            sel = select(rc, store, args.config, claim=True, run_id=run_id)
            for n in sel.notes:
                print(style.warn(f"warning: {n}"))
            print(("selected " if sel.claimed else "already committed to ") + f"scenario: {describe(sel.scenario)}")
            return 0
        compiler = Compiler(rc, reg)
        with_ = parse_with(args.with_)
        if with_:
            log.info("--with for this run: %s", with_)
        sel = select(rc, store, args.config, claim=False, run_id=run_id)
        logs.secrets = Secrets(values_at(sel.run_config.data, sel.run_config.nolog_paths))
        use_config_actions(reg, rc, sel.run_config, style)
        edits_now = cli_edits(args)
        start_at = resume_point(store) if args.resume else args.from_
        if args.resume:
            print(style.dim(f"resuming at {start_at} (where the last run stopped)"))
        prog = compiler.compile(sel.run_config.plan, args.steps or None, with_, sel.run_config.plan_of,
                                sel.run_config.data.get("edit"), edits_now, start_at)
        readonly = all(reg.actions[s.action].readonly for s in prog.steps())
        log.info("%s: %s", "read-only run (no writer lock, no state written)" if readonly
                 else "state-changing run", ", ".join(f"{s.id}={reg.actions[s.action].kind}"
                                                      f"{'/ro' if reg.actions[s.action].readonly else ''}"
                                                      for s in prog.main) or "no steps")
        if args.dry_outcome and not args.dry_run:
            raise ConfigError("--dry-outcome needs --dry-run")
        dry_outcomes = {}
        for spec in args.dry_outcome:
            sid, sep, outcome = spec.partition("=")
            if not (sid and sep and outcome):
                raise ConfigError(f"--dry-outcome {spec!r}: want STEP=OUTCOME")
            dry_outcomes[sid] = outcome
        if sel.scenario is None and not args.plan and not args.dry_run and not readonly and prog.main:
            log.info("no scenario in force and this run changes state: selecting one")
            sel = select(rc, store, args.config, claim=True, run_id=run_id)
            if not sel.claimed:    # a parallel runner selected first: compile against ITS config
                log.warning("a parallel runner selected the scenario first; recompiling against %s",
                            describe(sel.scenario))
                use_config_actions(reg, rc, sel.run_config, style)
                prog = compiler.compile(sel.run_config.plan, args.steps or None, with_, sel.run_config.plan_of,
                                        sel.run_config.data.get("edit"), edits_now, start_at)
        elif sel.scenario is None:
            log.info("no scenario selected and none claimed (%s)",
                     "--plan" if args.plan else "--dry-run" if args.dry_run else
                     "read-only" if readonly else "nothing to run")
        run_config = sel.run_config
        logs.secrets = Secrets(values_at(run_config.data, run_config.nolog_paths))
        log.info("run config %s (chosen by %s)%s", run_config.file or "(none)", run_config.how,
                 f"; joined scenario {run_config.scenario}" if run_config.scenario else "")
        for n in sel.notes:
            print(style.warn(f"warning: {n}"))
        if sel.claimed:
            print(style.dim(f"scenario selected: {describe(sel.scenario)}"))
    except (ConfigError, FixtureError) as e:
        log.error("refused before anything ran: %s", e)
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    if not prog.main:
        print(style.bad(f"ERROR: nothing to run: give steps{', `all`' if rc.all_fallback else ''}, or a run "
                        f"config with a plan ({run_config.file or 'no run config found'})"), file=sys.stderr)
        return EXIT_USAGE
    data = deep_merge(run_config.data, {rc.params_section: prog.params})
    state0 = initial_state(rc, store.get_state())
    log.info("state from the store: %s -> starting from %s", store.get_state() or "{}", state0)
    ui = InputBroker(interactive=False if args.non_interactive else None)
    try:
        runner = Runner(rc, reg, run_config, store, ui, color=args.color, run_id=run_id,
                        readonly=readonly, report_formats=formats if args.report else (),
                        show_commands=args.show_commands, forwarded=tuple(args.forwarded),
                        dry=args.dry_run, dry_outcomes=dry_outcomes)
    except FixtureError as e:
        log.error("refused before anything ran: %s", e)
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    logs.secrets = runner.secrets          # one object: run.nolog() values are masked here too
    if not args.plan and reg.observers:
        # reality first: a system this store never saw is judged by what it IS
        log.info("observing reality before the plan is judged: %s", ", ".join(reg.observers))
        try:
            seen = asyncio.run(runner.observe(prog, state0))
            log.info("observed %s", seen)
            for var, val in seen.items():
                if val != state0.get(var):
                    print(style.dim(f"observed {var}={val!r} (the store said {state0.get(var)!r})"))
            state0 = seen
        except Exception as e:  # noqa: BLE001 — an observer that cannot look must not block a run
            log.warning("observing failed; the plan is judged by the stored state", exc_info=True)
            print(style.warn(f"warning: could not observe the current state ({e}); judging the plan "
                             f"by the stored state"))
    v = simulate(rc, reg, prog, state0, data)
    log.info("static check from %s: walk %s; %d error(s), %d warning(s)", state0, " > ".join(v.walk),
             len(v.errors), len(v.warnings))
    src = (f"{run_config.file.name if run_config.file else '(no run config)'} ({run_config.how}"
           + (f"; scenario {run_config.scenario}" if run_config.scenario else "") + ")")
    for n in prog.notes:
        print(style.dim(f"note: {n}"))
    for w in v.warnings:
        print(style.warn(f"warning: {w}"))
    for e in v.errors:
        print(style.bad(f"error: {e}"))
    if args.plan:
        print(style.banner(f"steps: {prog.source}"))
        print(f"run config {src}: {len(v.walk)} step(s) on the expected walk")
        if args.forwarded:
            print(f"forwarded to the steps: {runner.secrets.text(shlex.join(args.forwarded))}")
        for i, sid in enumerate(v.walk, 1):
            print(f"  {i:>3}  {sid}")
        for sid, by in prog.reactions.items():
            for outcome, r in by.items():
                tgt = r.target if r.kind == "goto" else ", ".join(s.id for s in prog.segments.get(r.segment or "", []))
                print(f"       on {sid} {outcome}: {r.kind}{' ' + tgt if tgt else ''}")
        for sid, rules in prog.rules.items():
            for r in rules:
                print(f"       on {sid} {r.text}: {reaction_text(prog, r.reaction)}")
            print(f"       on {sid} otherwise: stop (failed)")
        if args.matrix:
            print(style.banner("state/event matrix (* = the expected outcome)"))
            print("\n".join(matrix_text(matrix(prog, reg))))
        return EXIT_USAGE if v.errors else 0
    if v.errors:
        log.error("the static check refused the plan: %s", "; ".join(v.errors))
        print(style.bad("ERROR: the plan cannot run as written (see above); nothing was started"), file=sys.stderr)
        return EXIT_USAGE
    if args.dry_run:
        print(style.banner("DRY RUN — only what looks runs; nothing is changed, locked, claimed or stored"))
    print(style.banner(f"steps: {prog.source}"))
    print(f"run config {src}: {' > '.join(v.walk)}")
    if args.forwarded:
        print(f"forwarded to the steps: {runner.secrets.text(shlex.join(args.forwarded))}")
    if args.matrix:
        print("\n".join(matrix_text(matrix(prog, reg))))
    if logs.file:
        print(style.dim(f"trace: {_rel(logs.file)}  (every decision; -v/-vv shows it here)"))
    try:
        result = asyncio.run(runner.run(prog))
    except LockedError as e:
        log.error("%s", e)
        print(style.bad(f"ERROR: {e}"), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        log.warning("interrupted (Ctrl-C)")
        print(style.bad("interrupted"), file=sys.stderr)
        return 130
    if not result.passed and logs.file:
        print(style.dim(f"trace of this run (every decision, every command): {_rel(logs.file)}"))
    return 0 if result.passed else EXIT_FAILED


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


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
        if a.inner_of is not None:      # a walk's inner step: part of its walk, never named alone
            continue
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
        for var, sv in rc.states.items():
            how = (f"observed in order {sv.order}" if sv.order else "observed" if var in reg.observers
                   else "model only" if sv.model_only else "constant" if not sv.transitions else "?")
            print(f"  {var}: {sv.values}  initial {sv.initial}  ({how})")
            for value in sv.values:
                v = sv.validators.get(value)
                print(f"    {value:<14} {sv.criteria.get(value, '(no criterion)')}"
                      + (f"  — validate {{{spec_text(v)}}}" if v else ""))
            for t in sv.transitions:
                print(f"    {t.frm} -> {t.to}  by {t.by} on {t.on}")
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
