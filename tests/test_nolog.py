"""NOLOG: a marked value never reaches the log, the terminal, the journal or a
traceback — and an unmarked one is NOT treated specially (the negative controls)."""
import asyncio
import io
import json

import pytest

from testrunner.config import load_file
from testrunner.nolog import MASK, Secrets, unwrap
from testrunner.proc import Proc

SECRET = "s3cr3t-Pa55"

RUNNER = """
modules: [m.py]
states: {s: {values: [a], initial: a}}
actions:
  echo-it:
    run: 'echo "token is {token}"; echo "done"'
  quiet-cmd:
    run: 'echo "the output {token}"'
    nolog: true
"""
MOD = """
from testrunner import action
@action()
def leak(params, run):
    generated = run.nolog("gen-" + "x9x9x9")
    raise RuntimeError(f"token {params['token']} and {generated}")
"""


def everything_written(sub) -> str:
    out = []
    for p in (sub.root / "state").rglob("*"):
        if p.is_file() and p.name != "scenario.json":
            out.append(p.read_text(errors="replace"))
    return "\n".join(out)


def make(sub, value_yaml):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", f"params:\n  token: {value_yaml}\n")


@pytest.mark.parametrize("marked", [f"{{NOLOG: {SECRET}}}", f"!NOLOG {SECRET}"])
def test_marked_value_is_masked_everywhere(sub, capsys, marked):
    make(sub, marked)
    code, out = sub.run("echo-it", "leak", capsys=capsys)
    written = everything_written(sub) + out
    assert code == 1
    assert SECRET not in written and "gen-x9x9x9" not in written
    assert f"token is {MASK}" in written


def test_unmarked_value_is_not_special(sub, capsys):
    # negative control: without NOLOG the same value is logged like anything else
    make(sub, SECRET)
    sub.run("echo-it", capsys=capsys)
    assert f"token is {SECRET}" in everything_written(sub)


def test_the_action_still_receives_the_real_value(sub, capsys):
    make(sub, f"{{NOLOG: {SECRET}}}")
    sub.write("testrunner.yml", RUNNER + "  write-it:\n    run: 'printf %s \"{token}\" > got.txt'\n")
    assert sub.run("write-it", capsys=capsys)[0] == 0
    assert (sub.root / "got.txt").read_text() == SECRET


def test_nolog_action_output_neither_logged_nor_shown(sub, capsys):
    make(sub, "plain-value")
    code, out = sub.run("quiet-cmd", capsys=capsys)
    assert code == 0 and "the output" not in out + everything_written(sub)
    assert "NOLOG" in (sub.root / "state/logs/quiet-cmd.log").read_text()


def test_nolog_rule_answer_not_in_log(tmp_path):
    rules = [{"expect": "Password:", "send": SECRET + "\r", "delay": 0.05, "nolog": True}]
    r = asyncio.run(Proc(tmp_path, tmp_path).run("printf 'Password: '; read -rs a; echo ok",
                                                 rules=rules, log="p.log", prompt_idle=5))
    assert r.ok and SECRET not in (tmp_path / "p.log").read_text()


def test_plain_rule_answer_is_logged(tmp_path):
    # negative control: the same rule without nolog writes its answer to the log
    rules = [{"expect": "Password:", "send": SECRET + "\r", "delay": 0.05}]
    asyncio.run(Proc(tmp_path, tmp_path).run("printf 'Password: '; read -rs a; echo ok",
                                             rules=rules, log="p.log", prompt_idle=5))
    assert SECRET in (tmp_path / "p.log").read_text()


def test_secret_split_across_chunks_still_masked(tmp_path):
    echo = io.StringIO()
    script = f"printf '%s' '{SECRET[:4]}'; sleep 0.3; printf '%s\\n' '{SECRET[4:]}'"
    asyncio.run(Proc(tmp_path, tmp_path, echo=echo, secrets=Secrets([SECRET])).run(script, log="c.log"))
    assert SECRET not in echo.getvalue() and MASK in echo.getvalue()
    assert SECRET not in (tmp_path / "c.log").read_text()


def test_toml_marking(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[params]\ntoken = { NOLOG = "abc" }\n')
    data, paths = unwrap(load_file(f))
    assert data == {"params": {"token": "abc"}} and paths == ["params.token"]


def test_scenario_keeps_the_marking_for_later_runners(sub, capsys):
    make(sub, f"{{NOLOG: {SECRET}}}")
    sub.write("m.py", MOD + "\n@action(produces={'s': 'a'})\ndef change(): pass\n")
    assert sub.run("change", capsys=capsys)[0] == 0           # selects the scenario
    rec = json.loads((sub.root / "state/scenario.json").read_text())
    assert rec["nolog_paths"] == ["params.token"]
    sub.run("echo-it", capsys=capsys)                         # a later runner, from the snapshot
    assert SECRET not in everything_written(sub)
