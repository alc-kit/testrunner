"""--dry-run (WP §6d, T21): walk the laid-out path against the real config and state, run only
what LOOKS (observers, read-only guards, `refuse`), print each command the rest would run, and
lock, claim, store or journal nothing. Stub binaries record every call: a dry run may make only
read-only ones, and the commands it prints must be the ones a real run makes."""
import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from conftest import main

RUNNER = """
modules: [mod.py]
action_files: [walks/*.yml]
states:
  rig: {values: [absent, up, done], initial: absent}
ext: {ansible: {}, vagrant: {}}
paths:
  full: [make-up, grow, described]
default_path: full
"""
MOD = '''
from pathlib import Path
from testrunner import action, fixture, observer, resolver, OutcomeError
from testrunner.ext.ansible import Ansible
from testrunner.ext.vagrant import Vagrant

HERE = Path(__file__).parent

@fixture
def ans(proc, run):
    return Ansible(proc, cwd=run.root, inventory=run.root / "inv")

@fixture
def vg(proc, run):
    return Vagrant(proc, cwd=run.root)

@observer("rig")
def look():
    p = HERE / "rig"
    return p.read_text().strip() if p.exists() else "absent"

@action(requires={"rig": "absent"}, produces={"rig": "up"})
def make_up():
    (HERE / "made").write_text("a python action ran")
    (HERE / "rig").write_text("up")

def describe_it(params):
    return [f"describe {params.get('what', 'it')}"]

@action(dry=describe_it)
def described():
    (HERE / "described-ran").write_text("x")

@resolver("hosts")
def hosts():
    if (HERE / "norig").exists():
        raise OutcomeError("failed", "no rig: nothing to ask")
    return ["h1", "h2"]
'''
WALKS = """
actions:
  grow:
    outcomes: [refused]
    produces: {rig: {up: done}}
    steps:
      - {id: legacy, refuse: "not on a legacy rig", when: {params.legacy: true}}
      - {id: guard, require_hosts: web}
      - id: install
        playbook: site.yml
        limit: "{hosts}"
        extra: {a: "1"}
        rules: {file: drive/r.json, V: x}
        log: install.log
        on: {failed: {end: refused}}
      - {id: create, vagrant: up, machines: [n9], parallel: false}
"""


@pytest.fixture
def dry(sub, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.txt"
    for name in ("ansible-playbook", "vagrant"):
        p = bindir / name
        p.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$(basename "$0") $*" >> {calls}\nexit 0\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    inv = bindir / "ansible-inventory"
    inv.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "ansible-inventory $*" >> {calls}\n'
                   'if [ -n "$NO_HOSTS" ]; then echo \'{"all": {"children": ["web"]}, "web": {}}\'; '
                   'else echo \'{"all": {"children": ["web"]}, "web": {"hosts": ["h1", "h2"]}}\'; fi\n')
    inv.chmod(inv.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("NO_HOSTS", raising=False)
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", MOD)
    sub.write("walks/main.yml", WALKS)
    sub.write("drive/r.json", '[{"expect": "victim @V@\\\\?", "send": "@V@\\r"}]')
    sub.write("configs/default.yml", "params: {legacy: false}\nplan: [full]\n")
    sub.calls = calls
    return sub


def run(sub, capsys, *args):
    code = main(["--root", str(sub.root), "--color", "never", "--non-interactive", *args])
    out = capsys.readouterr()
    return code, out.out + out.err


def store_snapshot(root: Path) -> dict:
    state = root / "state"
    return {p.relative_to(state).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(state.rglob("*")) if p.is_file() and "logs" not in p.parts} if state.exists() else {}


def calls(sub):
    return sub.calls.read_text().splitlines() if sub.calls.exists() else []


def test_a_dry_run_runs_only_what_looks_and_stores_nothing(dry, capsys):
    before = store_snapshot(dry.root)
    code, out = run(dry, capsys, "--dry-run")
    assert code == 0, out
    assert all(c.startswith("ansible-inventory ") for c in calls(dry)), calls(dry)   # the guard only
    assert not (dry.root / "made").exists() and not (dry.root / "described-ran").exists()
    assert store_snapshot(dry.root) == before                 # no state, journal, scenario
    logs = dry.root / "state" / "logs"
    assert list(logs.glob("testrunner-dry-*.log")) and not (logs / "testrunner-last.log").exists()
    assert "DRY RUN" in out and "nothing was run, locked or stored" in out


def test_python_actions_are_marked_or_described(dry, capsys):
    code, out = run(dry, capsys, "--dry-run", "-w", "what=the-thing")
    assert code == 0, out
    assert "(dry) python action make-up (not run): assumed passed" in out
    assert "(dry) would: describe the-thing" in out                                  # its dry= describer
    assert re.search(r"not run .*: make-up$", out, re.M)
    assert "the model's state at the end: {'rig': 'done'}" in out                    # effects applied, in memory


def test_the_printed_commands_are_the_ones_a_real_run_makes(dry, capsys):
    code, out = run(dry, capsys, "--dry-run")
    assert code == 0, out
    printed = [m.group(1).split("   (in ")[0] for m in re.finditer(r"\(dry\) would run: (.*)", out)]
    assert "answers 'victim x\\\\?' -> 'x\\r'" in out                                # the filled rules
    dry.calls.unlink()
    code, out = run(dry, capsys)                                                       # the real run
    assert code == 0, out
    executed = [c for c in calls(dry) if not c.startswith("ansible-inventory ")]
    inv = str(dry.root / "inv")
    assert printed == executed == [f"ansible-playbook site.yml -i {inv} --limit h1,h2 -e a=1",
                                   "vagrant up --no-parallel n9"]


def test_dry_outcome_takes_the_branch(dry, capsys):
    code, out = run(dry, capsys, "--dry-run", "--dry-outcome", "grow/install=failed")
    assert code != 0                                          # grow ended refused: the plan expected passed
    assert "(dry) grow/install: assumed failed (--dry-outcome)" in out
    assert "the walk ends refused" in out
    assert "vagrant up" not in out                            # the walk ended before `create`


def test_guards_and_refuse_really_run(dry, capsys, monkeypatch):
    monkeypatch.setenv("NO_HOSTS", "1")
    code, out = run(dry, capsys, "--dry-run")
    assert code != 0 and "targets the group 'web', which resolves to NO hosts" in out
    monkeypatch.delenv("NO_HOSTS")
    code, out = run(dry, capsys, "--dry-run", "-w", "legacy=true")
    assert code != 0 and "not on a legacy rig" in out


def test_without_a_rig_a_resolver_is_shown_unrendered_and_the_walk_goes_on(dry, capsys):
    dry.write("norig", "")
    code, out = run(dry, capsys, "--dry-run")
    assert code == 0, out
    assert "--limit {hosts}" in out or "--limit '{hosts}'" in out
    assert "shown unrendered" in out and "grow/install: {hosts}" in out


def test_dry_outcome_needs_dry_run(dry, capsys):
    code, out = run(dry, capsys, "--dry-outcome", "grow=failed")
    assert code != 0 and "--dry-outcome needs --dry-run" in out
