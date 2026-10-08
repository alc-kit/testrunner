"""The scenario lock: the first state-changing run commits a directory to its run
config; every other runner there adheres. Each rule is paired with its refusal."""
import json
import multiprocessing

import pytest

from testrunner.store import Store


def scenario(sub):
    p = sub.root / "state" / "scenario.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_first_run_selects_and_later_runs_use_the_snapshot(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    assert scenario(toy)["config_file"].endswith("branchy.yml")
    # edit the selected config on disk: the scenario's snapshot must still apply
    cfg = toy.root / "configs" / "default.yml"
    cfg.write_text(cfg.read_text().replace("rows: 3", "rows: 7"))
    code, out = toy.run("peek", capsys=capsys)
    assert code == 0 and "rows: 3" in out and "changed since the scenario was selected" in out


def test_without_a_scenario_the_edit_would_apply(toy, capsys):
    # negative control for the test above: the same edit, no scenario -> rows: 7
    cfg = toy.root / "configs" / "default.yml"
    cfg.write_text(cfg.read_text().replace("rows: 3", "rows: 7"))
    code, out = toy.run("peek", capsys=capsys)
    assert code == 0 and "rows: 7" in out


def test_explicit_other_config_refused(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    code, out = toy.run("-c", "negative", "verify", capsys=capsys)
    assert code == 2 and scenario(toy)["config_file"].endswith("branchy.yml")


def test_explicit_same_config_accepted(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    assert toy.run("-c", "branchy", "verify", capsys=capsys)[0] == 0


def test_env_selection_of_another_config_refused(toy, capsys, monkeypatch):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    monkeypatch.setenv("TOY_CONFIG", "negative")
    assert toy.run("verify", capsys=capsys)[0] == 2


def test_release_then_another_config_runs(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    assert toy.run("--release", capsys=capsys)[0] == 0
    assert scenario(toy) is None
    assert toy.run("-c", "negative", capsys=capsys)[0] == 0
    assert scenario(toy)["config_file"].endswith("negative.yml")


def test_release_refused_while_a_run_changes_state(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    with Store(toy.root / "state").lock():
        assert toy.run("--release", capsys=capsys)[0] == 2
    assert scenario(toy) is not None


def test_release_when_ends_the_scenario(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    code, out = toy.run("uninstall", capsys=capsys)
    assert code == 0 and "scenario released" in out and scenario(toy) is None


def test_release_when_not_reached_keeps_it(toy, capsys):
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0
    assert toy.run("verify", capsys=capsys)[0] == 0
    assert scenario(toy) is not None


def test_plan_and_readonly_runs_do_not_select(toy, capsys):
    assert toy.run("--plan", capsys=capsys)[0] == 0
    toy.run("peek", capsys=capsys)
    assert scenario(toy) is None


def test_select_without_running(toy, capsys):
    code, out = toy.run("--select", "-c", "negative", capsys=capsys)
    assert code == 0 and scenario(toy)["config_file"].endswith("negative.yml")
    assert not (toy.root / "state" / "journal.jsonl").read_text().count("run_start")


def test_readonly_runs_beside_a_writer_but_a_writer_does_not(toy, capsys):
    assert toy.run(capsys=capsys)[0] == 0
    with Store(toy.root / "state").lock():           # another run is changing state
        assert toy.run("peek", capsys=capsys)[0] == 0
        assert toy.run("verify", capsys=capsys)[0] == 2


def test_readonly_action_cannot_produce_state(sub, capsys):
    sub.write("testrunner.yml", "modules: [m.py]\nstates: {s: {values: [a]}}\n")
    sub.write("m.py", "from testrunner import action\n@action(readonly=True, produces={'s': 'a'})\ndef x(): pass\n")
    assert sub.run("x", capsys=capsys)[0] == 2


def _claim(root, n, q):
    q.put((n, Store(root).claim_scenario({"config_file": f"c{n}", "data": {}})))


def test_parallel_claims_have_exactly_one_winner(tmp_path):
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_claim, args=(tmp_path, n, q)) for n in range(12)]
    for p in procs:
        p.start()
    results = [q.get(timeout=20) for _ in procs]
    for p in procs:
        p.join()
    winners = [n for n, (claimed, _) in results if claimed]
    assert len(winners) == 1
    assert {rec["config_file"] for _, (_, rec) in results} == {f"c{winners[0]}"}


def _capture_how(toy):
    toy.write("actions/how.py", """
        from testrunner import action
        @action(readonly=True)
        def how(run, store):
            store.kv("t").put("how", {"how": run.run_config.how, "scenario": run.run_config.scenario})
        """)


def test_joined_scenario_keeps_how_it_was_selected(toy, capsys):
    _capture_how(toy)
    assert toy.run(capsys=capsys)[0] == 0                       # selected by default
    assert toy.run("how", capsys=capsys)[0] == 0                # a later run joins it
    got = json.loads((toy.root / "state/kv/t/how.json").read_text())
    assert got["how"] == "default" and got["scenario"].startswith("default.yml")


def test_joined_scenario_chosen_explicitly_says_so(toy, capsys):
    _capture_how(toy)
    assert toy.run("-c", "branchy", capsys=capsys)[0] == 0      # selected with --config
    assert toy.run("how", capsys=capsys)[0] == 0
    got = json.loads((toy.root / "state/kv/t/how.json").read_text())
    assert got["how"] == "--config" and "branchy.yml" in got["scenario"]
