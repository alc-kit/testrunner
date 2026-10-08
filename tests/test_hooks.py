import pytest

from testrunner import hook


def test_unknown_hook_refused():
    with pytest.raises(ValueError, match="unknown hook"):
        hook("configure")


def test_hooks_fire_in_order(sub, capsys):
    sub.write("testrunner.yml", "modules: [m.py]\nstates: {s: {values: [a], initial: a}}\n")
    sub.write("m.py", """
        from testrunner import action, hook
        SEEN = []
        @hook("run_start")
        def a(run): SEEN.append("run_start")
        @hook("step_start")
        async def b(step): SEEN.append("step_start:" + step.id)
        @hook("step_end")
        def c(result): SEEN.append("step_end:" + result.outcome)
        @hook("run_end")
        def d(run, result): run.store.kv("h").put("seen", SEEN + ["run_end"])
        @action()
        def x(): pass
        """)
    assert sub.run("x", capsys=capsys)[0] == 0
    import json
    assert json.loads((sub.root / "state/kv/h/seen.json").read_text()) == [
        "run_start", "step_start:x", "step_end:passed", "run_end"]
