"""testrunner's own log: the trace holds the DECISIONS, every command is in it, the
terminal shows it only when asked — and logging can never change or break a run."""
import ast
import asyncio
import logging
import re
from pathlib import Path

import pytest

from testrunner.proc import Proc

RUNNER = """
modules: [m.py]
states:
  s:
    values: [a, b]
    initial: a
    validate: {outcome: true}
    model_only: true
    transitions: [{from: "*", to: b, by: flip, on: passed}]
actions:
  sh-ok:
    run: 'echo shell-said-hello'
"""
MOD = """
import logging
from testrunner import action
log = logging.getLogger(__name__)

@action(requires={"s": "a"}, produces={"s": "b"})
async def flip(proc):
    log.info("subscriber decided: flip it")
    r = await proc.run(["bash", "-c", "echo probe-answer-42"], echo=False)
    assert r.ok
    await proc.run(["bash", "-c", "exit 3"], echo=False)

@action()
def gated(params):
    pass
"""
CONFIG = """
params: {flavour: plain}
plan:
  path:
    - flip
    - {action: gated, when: {params.flavour: fancy}}
    - sh-ok
"""


def make(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", CONFIG)


def fresh(sub):
    """Forget the system: the next run starts from the initial state again."""
    import shutil
    shutil.rmtree(sub.root / "state", ignore_errors=True)


def trace(sub) -> str:
    return (sub.root / "state/logs/testrunner-last.log").read_text()


def test_a_run_leaves_a_trace_with_its_decisions(sub, capsys):
    make(sub)
    code, out = sub.run(capsys=capsys)
    assert code == 0
    t = trace(sub)
    assert "trace: " in out                               # the operator is told where it is
    # config selection and why
    assert re.search(r"run config: no --config.*the default .*default\.yml", t)
    # requires judged against the state, and what the outcome produced: the claim and its confirmation
    assert "flip: requires {'s': 'a'} against state {'s': 'a'} -> holds" in t
    assert re.search(r"flip: s a -> b \(claimed by flip on passed; confirmed by outcome outcome: true\)", t)
    # a when: explained key by key, with the value the config holds
    assert "gated: when — params.flavour: have 'plain', want 'fancy' -> NO" in t
    assert "gated: skipped — when does not hold" in t
    # the walker's decision after each step
    assert "after flip ended passed: verdict ok; next gated" in t
    # the subscriber's own line, tagged with its step, its module name readable
    assert re.search(r"INFO +\[flip\] m \(m\.py:\d+\): subscriber decided: flip it", t)


def test_every_command_is_in_the_trace_start_and_end(sub, capsys):
    make(sub)
    sub.run(capsys=capsys)
    t = trace(sub)
    starts = re.findall(r"cmd#(\d+) start: ", t)
    ends = re.findall(r"cmd#(\d+) end: ", t)
    assert len(starts) == 3 and sorted(starts) == sorted(ends)      # flip x2 + sh-ok
    # a failing command's end is at INFO (seen with -v), with its exit code
    assert re.search(r"INFO .*cmd#\d+ end: bash exit 3", t)


def test_a_silent_commands_output_is_kept_in_the_trace(sub, capsys):
    # the probe's answer went nowhere else — the trace is the only place it can be read
    make(sub)
    sub.run(capsys=capsys)
    t = trace(sub)
    assert "its output (not shown anywhere else):\n    | probe-answer-42" in t
    # negative control: an echoed + logged command's output is NOT copied into the trace
    assert "    | shell-said-hello" not in t


def test_the_terminal_shows_the_log_only_when_asked(sub, capsys):
    make(sub)
    sub.run(capsys=capsys)
    assert "subscriber decided" not in capsys.readouterr().err
    fresh(sub)
    sub.run("-v")
    err = capsys.readouterr().err
    assert "[info flip m] subscriber decided: flip it" in err
    assert "cmd#" in err and "exit 3" in err               # a failing command's end is INFO ...
    assert " start: " not in err                           # ... the DEBUG detail stays out at -v ...
    fresh(sub)
    capsys.readouterr()
    sub.run("-vv")
    assert "[debug flip testrunner.proc] cmd#" in capsys.readouterr().err   # ... and shows at -vv


def test_log_level_from_the_environment_and_a_bad_level(sub, capsys, monkeypatch):
    make(sub)
    monkeypatch.setenv("TESTRUNNER_LOG_LEVEL", "info")
    sub.run()
    assert "subscriber decided" in capsys.readouterr().err
    code, _ = sub.run("--log-level", "chatty")
    assert code == 2 and "unknown log level" in capsys.readouterr().err


def test_repeated_invocations_do_not_pile_up_handlers(sub, capsys):
    make(sub)
    root_before = list(logging.getLogger().handlers)
    sub.run("-v")
    fresh(sub)
    sub.run("-v")
    assert logging.getLogger().handlers == root_before
    err = capsys.readouterr().err
    # one line per run, not one per handler ever installed
    assert err.count("subscriber decided: flip it") == 2


def test_a_broken_log_call_never_breaks_the_run(tmp_path):
    # logging must not be a new way to fail: a bad format string in a log call is reported
    # by logging itself (stderr), and the step still passes. Run as a real process —
    # pytest's own capture handler deliberately RE-RAISES logging errors, so in-process
    # this would test pytest, not testrunner.
    import subprocess
    import sys
    (tmp_path / "testrunner.yml").write_text("modules: [m.py]\n")
    (tmp_path / "m.py").write_text("import logging\nfrom testrunner import action\n@action()\n"
                                   "def sloppy():\n    logging.getLogger('x').info('%s %s', 'only one')\n")
    for verbosity in ([], ["-vv"]):
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "testrunner"),
                            "--root", str(tmp_path), "--color", "never", *verbosity, "sloppy"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "sloppy: passed" in r.stdout, r.stdout + r.stderr
        assert "Logging error" in r.stderr          # reported, not swallowed silently


def test_status_and_list_write_no_trace(sub, capsys):
    make(sub)
    sub.run("--list", capsys=capsys)
    sub.run("--status", capsys=capsys)
    assert not (sub.root / "state/logs").exists()


def test_old_traces_are_pruned(sub, capsys, monkeypatch):
    import testrunner.log as tl
    monkeypatch.setattr(tl, "KEEP", 2)
    make(sub)
    for _ in range(4):
        sub.run("--plan", capsys=capsys)
    traces = [p for p in (sub.root / "state/logs").glob("testrunner-*.log") if not p.is_symlink()]
    assert len(traces) == 2


def test_proc_logs_nolog_commands_by_program_name_only(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, "testrunner.proc")
    asyncio.run(Proc(tmp_path, tmp_path, nolog=True).run(["bash", "-c", "echo hush-hush"], echo=False))
    text = caplog.text
    assert "cmd#" in text and "bash" in text
    assert "hush-hush" not in text and "echo" not in text
    # negative control: the same command without NOLOG is logged in full
    caplog.clear()
    asyncio.run(Proc(tmp_path, tmp_path).run(["bash", "-c", "echo hush-hush"], echo=False))
    assert "echo hush-hush" in caplog.text


def test_env_given_to_a_command_is_logged_and_none_shows_as_unset(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, "testrunner.proc")
    asyncio.run(Proc(tmp_path, tmp_path).run(["true"], env={"TR_A": "1", "TR_B": None}, echo=False))
    assert "TR_A=1" in caplog.text and "TR_B=<unset>" in caplog.text


def test_no_module_logger_is_shadowed_by_a_log_parameter():
    # a function taking `log=` (a file name) that calls log.debug() would crash the
    # instrumented call itself — twice caught by hand while instrumenting; now a guard
    src = Path(__file__).resolve().parents[1] / "src"
    bad = []
    for f in src.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "log" in {a.arg for a in n.args.args + n.args.kwonlyargs}:
                    uses = [x.lineno for x in ast.walk(n) if isinstance(x, ast.Attribute)
                            and isinstance(x.value, ast.Name) and x.value.id == "log"]
                    if uses:
                        bad.append(f"{f.name}:{n.name} lines {uses}")
    assert not bad, bad


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_the_run_is_the_same_at_every_log_level(sub, capsys, level):
    # logging is observation only: the same plan gives the same outcomes at every level
    make(sub)
    code, out = sub.run("--log-level", level, capsys=capsys)
    lines = [re.sub(r"\(\d+\.\ds\)", "(Ts)", ln) for ln in out.splitlines()
             if ln.startswith(("---- ", "  passed", "  skipped", "==== PASSED", "==== FAILED"))]
    assert code == 0
    assert lines == ["---- flip: passed (Ts)", "---- gated: skipped (when)", "---- sh-ok: passed (Ts)",
                     "  passed   flip", "  skipped  gated", "  passed   sh-ok", "==== PASSED ===="]


def test_null_env_in_a_shell_action_unsets_and_a_null_parameter_is_refused(sub, capsys, monkeypatch):
    # the same class as the ansible "None" bug: YAML null must never become the word None
    monkeypatch.setenv("TR_GONE", "inherited")
    sub.write("testrunner.yml", """
modules: []
actions:
  envy:
    run: 'echo "gone=${{TR_GONE-<unset>}}" > out.txt'
    env: {TR_GONE: ~}
  argy:
    run: ["echo", "{target}"]
""")
    sub.write("configs/default.yml", "params: {target: ~}\n")
    assert sub.run("envy", capsys=capsys)[0] == 0
    assert (sub.root / "out.txt").read_text().strip() == "gone=<unset>"
    code, out = sub.run("argy", capsys=capsys)
    assert code == 1 and "null" in out and "'None'" in out


def test_a_when_key_the_config_does_not_have_is_warned(sub, capsys):
    make(sub)
    sub.write("configs/default.yml", CONFIG.replace("params.flavour: fancy", "params.flavor: fancy"))
    sub.run(capsys=capsys)
    assert re.search(r"WARNING .*gated: when names \['params.flavor'\], which the run config does not contain", trace(sub))
    # negative control: the correctly spelt key gives no warning
    fresh(sub)
    sub.write("configs/default.yml", CONFIG)
    sub.run(capsys=capsys)
    assert "does not contain at all" not in trace(sub)


def test_the_trace_names_the_rules_a_command_is_armed_with(sub, capsys):
    # a prompt that changed wording goes unanswered: the trace must show what was EXPECTED
    sub.write("testrunner.yml", RUNNER + """
  asks:
    run: 'printf "Continue? "; read a; echo got-$a'
    rules: [{expect: 'Proceed\\? ', send: "yes\\r"}, {expect: 'Password: ', send: "hunter2\\r", nolog: true}]
""")
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", "plan: [asks]\n")
    sub.write("testrunner.yml", (sub.root / "testrunner.yml").read_text() + "input: {prompt_idle: 1}\n")
    code, out = sub.run(capsys=capsys)
    assert code != 0                     # the changed prompt went unanswered
    t = trace(sub)
    assert re.search(r"cmd#\d+ rule 1: expect 'Proceed\\\\\? ' -> send 'yes\\r'", t), t[-3000:]
    assert re.search(r"cmd#\d+ rule 2: expect 'Password: ' -> send <NOLOG>", t)
    assert "hunter2" not in t
