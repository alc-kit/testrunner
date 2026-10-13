"""Arguments after `--`: testrunner keeps its hands off them and hands them to the steps;
ext.ansible gives them to every playbook, and a forwarded --limit NARROWS a step's own."""
import asyncio
import json
import os
import shutil
import stat
import subprocess

import pytest

from testrunner.api import OutcomeError
from testrunner.cli import split_forwarded
from testrunner.ext.ansible import Ansible, EmptyScope, Forwarded, ForwardRefused
from testrunner.proc import Proc


# ── the CLI split ──
@pytest.mark.parametrize("argv, own, fwd", [
    (["up", "verify"], ["up", "verify"], []),
    (["up", "--", "-vvv", "--limit", "n1"], ["up"], ["-vvv", "--limit", "n1"]),
    (["--", "-e", "x=1", "--", "y"], [], ["-e", "x=1", "--", "y"]),    # only the FIRST -- splits
    (["up", "--"], ["up"], []),
])
def test_split_forwarded(argv, own, fwd):
    assert split_forwarded(argv) == (own, fwd)


# ── what a playbook gets ──
@pytest.mark.parametrize("argv, limit, args", [
    (["-vvv"], None, ["-vvv"]),
    (["--limit", "n1,n2"], "n1,n2", []),
    (["--limit=n1"], "n1", []),
    (["-l", "n1"], "n1", []),
    (["-ln1"], "n1", []),
    (["--lim", "n1"], "n1", []),                 # argparse takes a prefix: so must we (measured)
    (["-vl", "n1"], "n1", ["-v"]),               # bundled flags carrying the limit (measured)
    (["-vvln1", "-D"], "n1", ["-v", "-v", "-D"]),
    (["-e", "x=1", "-e", "--check"], None, ["-e", "x=1", "-e", "--check"]),   # a VALUE, not an option
    (["--extra-vars={\"a\": 1}", "-t", "net", "--diff"], None, ["--extra-vars={\"a\": 1}", "-t", "net", "--diff"]),
])
def test_forwarded_parse(argv, limit, args):
    f = Forwarded(argv)
    assert (f.limit, f.args) == (limit, args)


@pytest.mark.parametrize("argv, why", [
    (["-C"], "changed nothing"), (["--check"], "changed nothing"), (["-vC"], "changed nothing"),
    (["--che"], "changed nothing"),              # a prefix of --check is --check
    (["--syntax-check"], "run nothing"), (["--list-hosts"], "run nothing"), (["--list-t"], "ambiguous"),
    (["-i", "other"], "inventory"), (["--inventory=x"], "inventory"),
    (["--limit", "a", "-l", "b"], "twice"), (["--limit", ""], "empty"), (["--limit"], "wants a value"),
    (["site.yml"], "not an option"), (["--no-such"], "not an ansible-playbook option"),
])
def test_forwarded_refusals(argv, why):
    with pytest.raises(ForwardRefused) as e:
        Forwarded(argv)
    assert why in str(e.value) and e.value.outcome.name == "errored"


def stub(dirpath, name, body):
    p = dirpath / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """Stub ansible binaries; `ansible --list-hosts` answers with $STUB_HOSTS (none: no hosts)."""
    d = tmp_path / "bin"
    d.mkdir()
    rec = tmp_path / "calls.txt"
    stub(d, "ansible-playbook", f'printf "%s\\n" "ansible-playbook $*" >> {rec}\n')
    stub(d, "ansible", f'printf "%s\\n" "ansible $*" >> {rec}\n'
         'if [ -z "$STUB_HOSTS" ]; then echo "[ERROR]: Specified inventory, host pattern and/or --limit leaves us '
         'with no hosts to target."; exit 1; fi\n'
         'set -- $STUB_HOSTS; echo "  hosts ($#):"; for h; do echo "    $h"; done\n')
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    monkeypatch.delenv("STUB_HOSTS", raising=False)
    return rec


def ans(tmp_path, forwarded):
    proc = Proc(tmp_path, tmp_path, forwarded=tuple(forwarded))
    return Ansible(proc, cwd=tmp_path, inventory=tmp_path / "inv")


def test_forwarded_args_come_last_so_a_forwarded_e_wins(tmp_path, calls):
    asyncio.run(ans(tmp_path, ["-vvv", "-e", "x=2"]).playbook("site.yml", limit=["a"], extra={"x": 1}))
    assert calls.read_text().splitlines() == [
        f"ansible-playbook site.yml -i {tmp_path / 'inv'} --limit a -e x=1 -vvv -e x=2"]


def test_a_forwarded_limit_narrows_the_steps_own(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("STUB_HOSTS", "n2")
    asyncio.run(ans(tmp_path, ["--limit", "n2,n9", "-v"]).playbook("site.yml", limit=["n1", "n2", "cache"]))
    assert calls.read_text().splitlines() == [
        f"ansible -i {tmp_path / 'inv'} n1,n2,cache --list-hosts --limit n2,n9",     # ansible decides
        f"ansible-playbook site.yml -i {tmp_path / 'inv'} --limit n2 -v"]


def test_narrowed_to_nothing_refuses_and_runs_no_playbook(tmp_path, calls):
    with pytest.raises(EmptyScope) as e:
        asyncio.run(ans(tmp_path, ["--limit", "n3"]).playbook("site.yml", limit=["n1"]))
    assert "narrowed by the forwarded --limit n3" in e.value.outcome.detail
    assert "ansible-playbook" not in calls.read_text()


def test_an_empty_host_list_with_exit_0_refuses_too(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("STUB_HOSTS", " ")          # answers "hosts (0):" and exits 0
    with pytest.raises(EmptyScope) as e:
        asyncio.run(ans(tmp_path, ["--limit", "n3"]).playbook("site.yml", limit=["n1"]))
    assert "narrowed by the forwarded --limit n3" in e.value.outcome.detail     # says WHY it is empty
    assert "ansible-playbook" not in calls.read_text()


def test_a_playbook_without_a_limit_is_not_narrowed(tmp_path, calls):
    # its own hosts: (a tang, the founder): an operator's node limit would skip the play, exit 0
    asyncio.run(ans(tmp_path, ["--limit", "n2", "-v"]).playbook("tang.yml"))
    assert calls.read_text().splitlines() == [f"ansible-playbook tang.yml -i {tmp_path / 'inv'} -v"]


def test_adhoc_and_probes_never_get_forwarded_args(tmp_path, calls):
    asyncio.run(ans(tmp_path, ["-vvv", "--limit", "n2"]).adhoc("n1", "ping"))
    assert calls.read_text().splitlines() == [f"ansible -i {tmp_path / 'inv'} n1 -m ping"]


def test_without_forwarded_args_nothing_changes(tmp_path, calls):
    # negative control for the tests above: the same calls, no `--`
    asyncio.run(ans(tmp_path, []).playbook("site.yml", limit=["n1", "n2"]))
    assert calls.read_text().splitlines() == [f"ansible-playbook site.yml -i {tmp_path / 'inv'} --limit n1,n2"]


def test_a_refused_argument_refuses_when_the_controller_is_built(tmp_path):
    # i.e. as the step's fixtures resolve, before the step does anything
    with pytest.raises(OutcomeError, match="--check"):
        ans(tmp_path, ["--check"])


# ── against the real ansible (not in CI: no ansible there) ──
REAL_INV = "[nodes]\nn1\nn2\nn3\n[cache]\nc1\n"
REAL_PB = ("- hosts: nodes\n  gather_facts: false\n  connection: local\n  tasks:\n"
           "    - debug: msg=\"ran on {{ inventory_hostname }}\"\n")


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")
@pytest.mark.parametrize("forwarded, step_limit, ran", [
    (["--limit", "n2,c1"], ["n1", "n2"], ["n2"]),
    (["-l", "nodes:!n1"], ["n1", "n3"], ["n3"]),
    ([], ["n1", "n3"], ["n1", "n3"]),          # negative control
])
def test_narrowing_against_real_ansible(tmp_path, forwarded, step_limit, ran):
    (tmp_path / "inv.ini").write_text(REAL_INV)
    (tmp_path / "pb.yml").write_text(REAL_PB)
    a = Ansible(Proc(tmp_path, tmp_path, forwarded=tuple(forwarded)), cwd=tmp_path, inventory=tmp_path / "inv.ini")
    r = asyncio.run(a.playbook("pb.yml", limit=step_limit, echo=False))
    assert sorted(set(__import__("re").findall(r"ran on (\w+)", r.text))) == ran


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")
def test_appending_a_limit_would_have_replaced_the_steps_own(tmp_path):
    # the measurement this design rests on: ansible keeps only the LAST --limit
    (tmp_path / "inv.ini").write_text(REAL_INV)
    (tmp_path / "pb.yml").write_text(REAL_PB)
    r = subprocess.run(["ansible-playbook", "pb.yml", "-i", "inv.ini", "--limit", "n1", "--limit", "n3"],
                       cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert "ran on n3" in r.stdout and "ran on n1" not in r.stdout


# ── end to end through the CLI ──
RUNNER = """
modules: [m.py]
actions:
  sh:
    run: 'echo "TRF=$TR_FORWARDED" > sh.out'
"""
MOD = """
import json
from testrunner import action

@action()
async def look(proc, run):
    (run.root / "look.out").write_text(json.dumps([list(proc.forwarded), list(run.forwarded)]))
"""


def make(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("m.py", MOD)


def test_forwarded_reach_actions_shell_steps_and_the_journal(sub, capsys):
    make(sub)
    code, out = sub.run("look", "sh", "--", "-vv", "--limit", "n1", capsys=capsys)
    assert code == 0, out
    assert "forwarded to the steps: -vv --limit n1" in out
    fwd = ["-vv", "--limit", "n1"]
    assert json.loads((sub.root / "look.out").read_text()) == [fwd, fwd]
    assert json.loads((sub.root / "sh.out").read_text().split("=", 1)[1]) == fwd
    starts = [json.loads(ln) for ln in (sub.root / "state" / "journal.jsonl").read_text().splitlines()
              if '"run_start"' in ln]
    assert starts[-1]["forwarded"] == fwd


def test_no_dashdash_forwards_nothing(sub, capsys):
    make(sub)
    code, out = sub.run("look", capsys=capsys)
    assert code == 0 and "forwarded" not in out
    assert json.loads((sub.root / "look.out").read_text()) == [[], []]


def test_plan_shows_them(sub, capsys):
    make(sub)
    code, out = sub.run("--plan", "look", "--", "-v", capsys=capsys)
    assert code == 0 and "forwarded to the steps: -v" in out


@pytest.mark.parametrize("mode", ["--status", "--list", "--scenario"])
def test_modes_that_run_nothing_refuse_them(sub, capsys, mode):
    make(sub)
    code, _ = sub.run(mode, "--", "-v")
    assert code == 2
    assert f"{mode} runs none" in capsys.readouterr().err
