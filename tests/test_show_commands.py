"""--show-commands: every command, before it runs, as a line a person can paste into a
shell to rerun it by hand. Proven by RUNNING the printed lines, not by reading them."""
import re
import subprocess

SECRET = "pa55-w0rd-xyz"
RUNNER = """
modules: [m.py]
actions:
  greet:
    run: ["bash", "-c", "echo \\"hello $WHO from $(basename $PWD)\\" > greet.out"]
    env: {WHO: "the {who}"}
    cwd: sub
  hush:
    run: 'echo token {token}'
    nolog: true
"""
MOD = """
from testrunner import action

@action()
async def probe(proc):
    r = await proc.run(["bash", "-c", "echo probe-answer-42 > probe.out"], echo=False)
    assert r.ok
"""


def make(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)
    sub.write("configs/default.yml", f"params:\n  who: world\n  token: {{NOLOG: {SECRET}}}\n")
    (sub.root / "sub").mkdir()


def shown(out: str) -> list[str]:
    return [ln[2:] for ln in out.splitlines() if ln.startswith("$ ")]


def test_every_command_is_shown_and_the_line_reruns_it(sub, capsys):
    make(sub)
    code, out = sub.run("--show-commands", "greet", "probe", capsys=capsys)
    assert code == 0
    lines = shown(out)
    assert len(lines) == 2, out
    greet, probe = lines
    assert greet.startswith(f"cd {sub.root / 'sub'} && ") and "WHO='the world'" in greet
    assert probe.endswith("# output not shown (see the trace)")
    # the proof: paste each line into a shell (from elsewhere) and get the same result
    for f in ("sub/greet.out", "probe.out"):
        (sub.root / f).unlink()
    for line in lines:
        subprocess.run(["bash", "-c", line], check=True, cwd="/")
    assert (sub.root / "sub/greet.out").read_text().strip() == "hello the world from sub"
    assert (sub.root / "probe.out").read_text().strip() == "probe-answer-42"


def test_not_shown_without_the_flag(sub, capsys):
    # negative control
    make(sub)
    code, out = sub.run("greet", capsys=capsys)
    assert code == 0 and not shown(out)


def test_the_environment_variable_turns_it_on(sub, capsys, monkeypatch):
    make(sub)
    monkeypatch.setenv("TESTRUNNER_SHOW_COMMANDS", "1")
    code, out = sub.run("greet", capsys=capsys)
    assert code == 0 and len(shown(out)) == 1


def test_a_nolog_action_shows_only_its_program(sub, capsys):
    make(sub)
    code, out = sub.run("--show-commands", "hush", capsys=capsys)
    assert code == 0 and SECRET not in out
    (line,) = shown(out)
    assert line.startswith("bash …") and "NOLOG" in line and "token" not in line
