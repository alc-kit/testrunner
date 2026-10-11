"""The run_config hook: a subscriber adjusts each run config AS IT LOADS, so whatever is
planned from it — a step's `when:`, the scenario snapshot — sees the adjusted value. The
case behind it: gate3 keeps GATE3_BACKEND=lvm-thin from the environment and builds
lvm-thin, while `when: {rig.storage.backend: zfs}` read the config's zfs and ran convert."""
import json

import pytest

RUNNER = "modules: [m.py]\nstates: {s: {values: [a, b], initial: a}}\n"
CONFIG = """
    mode: x
    secret: !NOLOG hunter2
    plan:
      path: [{action: only-y, when: {mode: y}}, always]
    """
MODULE = """
    import os
    from testrunner import action, hook
    @hook("run_config")
    def from_env(config, how):
        if os.environ.get("TR_MODE"):
            config["mode"] = os.environ["TR_MODE"]
    @action(produces={"s": "b"})
    def only_y(): print("RAN only-y")
    @action(produces={"s": "b"})
    def always(): print("RAN always")
    """


@pytest.fixture
def hooked(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("configs/default.yml", CONFIG)
    sub.write("m.py", MODULE)
    return sub


def test_when_sees_what_the_hook_made_of_the_config(hooked, capsys, monkeypatch):
    monkeypatch.setenv("TR_MODE", "y")
    code, out = hooked.run(capsys=capsys)
    assert code == 0 and "RAN only-y" in out and "RAN always" in out, out


def test_control_without_the_override_when_reads_the_file(hooked, capsys, monkeypatch):
    monkeypatch.delenv("TR_MODE", raising=False)
    code, out = hooked.run(capsys=capsys)
    assert code == 0 and "RAN only-y" not in out and "RAN always" in out, out


def test_the_scenario_snapshot_records_the_hooked_value(hooked, capsys, monkeypatch):
    monkeypatch.setenv("TR_MODE", "y")
    assert hooked.run(capsys=capsys)[0] == 0
    rec = json.loads((hooked.root / "state" / "scenario.json").read_text())
    assert rec["data"]["mode"] == "y"


def test_a_changed_key_is_logged_and_a_nolog_one_masked(hooked, capsys, monkeypatch):
    hooked.write("m.py", MODULE.replace(
        'config["mode"] = os.environ["TR_MODE"]',
        'config["mode"] = os.environ["TR_MODE"]; config["secret"] = "swordfish"'))
    monkeypatch.setenv("TR_MODE", "y")
    assert hooked.run(capsys=capsys)[0] == 0
    trace = (hooked.root / "state" / "logs" / "testrunner-last.log").read_text()
    assert "from_env: mode 'x' -> 'y'" in trace, trace[-3000:]
    assert "from_env: secret '<NOLOG>' -> '<NOLOG>'" in trace
    assert "swordfish" not in trace and "hunter2" not in trace


def test_a_hook_that_raises_refuses_before_anything_runs(hooked, capsys):
    hooked.write("m.py", MODULE.replace('if os.environ.get("TR_MODE"):',
                                        'raise RuntimeError("GATE3_BACKEND=bogus is not allowed")\n        if 0:'))
    code, _ = hooked.run()
    err = capsys.readouterr().err
    assert code == 2 and "GATE3_BACKEND=bogus is not allowed" in err, err
    assert not (hooked.root / "state" / "scenario.json").exists()


def test_an_async_run_config_hook_is_refused(hooked, capsys):
    hooked.write("m.py", MODULE.replace("def from_env", "async def from_env"))
    code, _ = hooked.run()
    assert code == 2 and "must be a plain function" in capsys.readouterr().err
