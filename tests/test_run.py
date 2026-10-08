"""End to end through the CLI: the toy subscriber, fixtures, abort, background work,
the store. Every pass is paired with the input that must fail."""
import asyncio
import io
import json

import pytest

from testrunner.config import load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler
from testrunner.runner import Runner
from testrunner.store import LockedError, Store
from testrunner.ui import InputBroker


def journal(sub):
    return [json.loads(line) for line in (sub.root / "state" / "journal.jsonl").read_text().splitlines()]


def test_toy_lifecycle_passes_and_state_lives_in_the_subscriber(toy, capsys):
    code, out = toy.run(capsys=capsys)
    assert code == 0, out
    assert json.loads((toy.root / "state" / "state.json").read_text()) == {"service": "running", "data": "seeded"}
    ev = [e["event"] for e in journal(toy)]
    assert ev[:2] == ["scenario_selected", "run_start"] and ev[-1] == "run_end"
    assert ev.count("step_end") == 4


def test_toy_second_run_refused_statically(toy, capsys):
    assert toy.run(capsys=capsys)[0] == 0
    code, out = toy.run(capsys=capsys)
    assert code == 2 and "install: requires" in out and "====" not in out.split("error:")[-1].split("plan of")[0]


def test_branchy_takes_detour_and_reaction(toy, capsys):
    code, out = toy.run("-c", "branchy", capsys=capsys)
    assert code == 0, out
    steps = [e["step"] for e in journal(toy) if e["event"] == "step_end"]
    assert steps == ["uninstall", "install", "start", "early-verify", "seed", "repair", "verify"]


def test_negative_control_expected_failure_passes(toy, capsys):
    assert toy.run("-c", "negative", capsys=capsys)[0] == 0


def test_negative_control_without_expect_fails(toy, capsys):
    toy.write("configs/negative.yml", (toy.root / "configs/negative.yml").read_text()
              .replace("  expect:\n    wrong-rows: {outcome: failed}\n", ""))
    code, out = toy.run("-c", "negative", capsys=capsys)
    assert code == 1 and "wrong-rows ended failed" in out


def test_expected_failure_that_passes_fails_the_run(toy, capsys):
    toy.write("configs/negative.yml", (toy.root / "configs/negative.yml").read_text()
              .replace("rows: 99", "rows: 3"))
    assert toy.run("-c", "negative", capsys=capsys)[0] == 1


def test_with_override_reaches_the_action(toy, capsys):
    code, out = toy.run("uninstall", "lifecycle", "--with", "rows=5", capsys=capsys)
    assert code == 0 and (toy.root / "state" / "sandbox" / "data").read_text() == "xxxxx"


def test_observer_overrides_the_model(toy, capsys):
    # the model says `start` produces running; reality (the observer) says otherwise
    toy.write("actions/liar.py", """
        from testrunner import action
        @action(requires={"service": "installed"}, produces={"service": "running"})
        def fake_start():
            pass
        """)
    code, out = toy.run("uninstall", "install", "fake-start", capsys=capsys)
    assert code == 0 and "observed service='installed'" in out
    assert json.loads((toy.root / "state" / "state.json").read_text())["service"] == "installed"


def test_expect_state_checked_against_reality(toy, capsys):
    toy.write("actions/liar.py", """
        from testrunner import action
        @action(requires={"service": "installed"}, produces={"service": "running"})
        def fake_start():
            pass
        """)
    toy.write("configs/liar.yml", """
        params: {sandbox: state/sandbox}
        plan:
          path: [uninstall, install, fake-start]
          expect: {fake-start: {state: {service: running}}}
        """)
    code, out = toy.run("-c", "liar", capsys=capsys)
    assert code == 1 and "expected state not reached" in out


SIMPLE = """
modules: [mod.py]
states:
  s: {values: [a], initial: a}
"""


def test_fixture_teardown_runs_in_reverse_even_after_failure(sub, capsys):
    sub.write("testrunner.yml", SIMPLE)
    sub.write("mod.py", """
        from testrunner import action, fixture
        LOG = []
        @fixture
        def first():
            LOG.append("up1"); yield 1; LOG.append("down1")
        @fixture
        async def second(first):
            LOG.append("up2"); yield 2; LOG.append("down2")
        @action()
        def boom(second, store):
            raise RuntimeError("bang")
        @action()
        def report(store):
            store.kv("t").put("log", LOG)
        """)
    code, out = sub.run("boom", capsys=capsys)
    assert code == 1 and "RuntimeError: bang" in out
    assert (sub.root / "state" / "logs" / "boom.traceback").exists()
    sub.run("report", capsys=capsys)
    assert json.loads((sub.root / "state/kv/t/log.json").read_text()) == ["up1", "up2", "down2", "down1"]


def test_shadowing_a_builtin_fixture_refused(sub, capsys):
    sub.write("testrunner.yml", SIMPLE)
    sub.write("mod.py", "from testrunner import action, fixture\n@fixture\ndef proc(): return 1\n@action()\ndef a(): pass\n")
    code, _ = sub.run("a", capsys=capsys)
    assert code == 2


def test_fixture_cycle_is_an_error_not_a_hang(sub, capsys):
    sub.write("testrunner.yml", SIMPLE)
    sub.write("mod.py", """
        from testrunner import action, fixture
        @fixture
        def x(y): return 1
        @fixture
        def y(x): return 1
        @action()
        def a(x): pass
        """)
    code, out = sub.run("a", capsys=capsys)
    assert code == 1 and "fixture cycle" in out


def test_fatal_background_failure_fails_the_step(sub, capsys):
    sub.write("testrunner.yml", SIMPLE)
    sub.write("mod.py", """
        import asyncio
        from testrunner import action
        async def probe():
            await asyncio.sleep(0.05); raise RuntimeError("probe lost the node")
        @action()
        async def watched(step):
            step.spawn(probe(), name="probe", wait=True, fatal=True)
            await asyncio.sleep(0.2)
        @action()
        async def unwatched(step):
            step.spawn(probe(), name="probe")          # not fatal: reported nowhere, cancelled
        """)
    code, out = sub.run("watched", capsys=capsys)
    assert code == 1 and "probe lost the node" in out
    assert sub.run("unwatched", capsys=capsys)[0] == 0


def test_operator_abort_stops_the_run(sub):
    sub.write("testrunner.yml", SIMPLE)
    sub.write("mod.py", """
        import asyncio
        from testrunner import action
        @action()
        async def slow(): await asyncio.sleep(30)
        @action()
        def after(): pass
        """)
    rc = load_runner_config(sub.root / "testrunner.yml")
    reg = collect(rc)
    prog = Compiler(rc, reg).compile(None, ["slow", "after"])
    ui = InputBroker(interactive=False, out=io.StringIO())

    async def go():
        runner = Runner(rc, reg, load_run_config(rc), Store(rc.state_dir), ui, echo=io.StringIO(), color="never")
        task = asyncio.ensure_future(runner.run(prog))
        await asyncio.sleep(0.3)
        ui.feed_line("abort")
        return await task
    result = asyncio.run(go())
    assert not result.passed and [s.outcome for s in result.steps] == ["aborted"]


def test_requires_violated_at_runtime_is_errored(sub, capsys):
    sub.write("testrunner.yml", """
        modules: [mod.py]
        states:
          s: {values: [a, b]}
        """)
    sub.write("mod.py", "from testrunner import action\n@action(requires={'s': 'b'})\ndef needs_b(): pass\n")
    code, out = sub.run("needs-b", capsys=capsys)       # s unknown: static check can only warn
    assert code == 1 and "warning:" in out and "errored" in out


def test_toml_runner_and_run_config(sub, capsys):
    sub.write("testrunner.toml", """
        modules = ["mod.py"]
        [states.s]
        values = ["a", "b"]
        initial = "a"
        """)
    sub.write("mod.py", "from testrunner import action\n@action(produces={'s': 'b'})\ndef go(params):\n    assert params['n'] == 2\n")
    sub.write("configs/default.toml", """
        [params]
        n = 2
        [plan]
        path = ["go"]
        """)
    code, out = sub.run(capsys=capsys)
    assert code == 0, out


def test_store_lock_is_exclusive(tmp_path):
    s = Store(tmp_path)
    with s.lock():
        with pytest.raises(LockedError):
            with Store(tmp_path).lock():
                pass
    with Store(tmp_path).lock():      # released afterwards
        pass


def test_journal_skips_a_torn_line(tmp_path):
    s = Store(tmp_path)
    s.journal.append("a")
    with (tmp_path / "journal.jsonl").open("a") as f:
        f.write('{"event": "tor')
    assert [e["event"] for e in s.journal.read()] == ["a"]


def test_status_reports_an_unfinished_step(toy, capsys):
    toy.run(capsys=capsys)
    with (toy.root / "state" / "journal.jsonl").open("a") as f:
        f.write(json.dumps({"at": "x", "event": "run_start"}) + "\n")
        f.write(json.dumps({"at": "x", "event": "step_start", "step": "seed"}) + "\n")
    code, out = toy.run("--status", capsys=capsys)
    assert code == 0 and "STARTED  seed" in out


def test_shell_action_sees_the_runner_environment(sub, capsys):
    sub.write("testrunner.yml", """
        states: {s: {values: [a], initial: a}}
        actions:
          show:
            run: 'printf "%s|%s|%s|%s\\n" "$TR_STEP" "$TR_STEP_WITH" "$TR_CONFIG_FILE" "$TR_PARAMS" > out.txt'
        """)
    sub.write("configs/default.yml", "params: {n: 1}\nplan:\n  path: [{action: show, with: {m: 2}}]\n")
    assert sub.run(capsys=capsys)[0] == 0
    step, step_with, cfg, params = (sub.root / "out.txt").read_text().strip().split("|")
    assert step == "show" and json.loads(step_with) == {"m": 2} and cfg.endswith("default.yml")
    assert json.loads(params) == {"n": 1, "m": 2}
