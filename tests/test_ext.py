"""ext plugins against stub binaries on PATH — what they run, and what they refuse."""
import asyncio
import json
import os
import stat

import pytest

from testrunner.api import OutcomeError
from testrunner.ext.ansible import Ansible, EmptyScope
from testrunner.ext.vagrant import Vagrant, parse_machine_readable
from testrunner.proc import Proc


def stub(dirpath, name, body):
    p = dirpath / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    rec = tmp_path / "calls.txt"
    # every stub records its argv, one call per line, then behaves per its body
    for name in ("ansible-playbook", "ansible", "ansible-galaxy"):
        stub(d, name, f'printf "%s\\n" "$(basename "$0") $*" >> {rec}\nexit "${{STUB_RC:-0}}"\n')
    stub(d, "ansible-inventory", f'printf "%s\\n" "ansible-inventory $*" >> {rec}\n'
         'echo "[WARNING]: noise first"\n'
         'echo \'{"all": {"children": ["web"]}, "web": {"hosts": ["w1", "w2"], "children": ["db"]}, '
         '"db": {"hosts": ["d1"]}, "empty": {}}\'\n')
    stub(d, "vagrant", f'printf "%s\\n" "vagrant $*" >> {rec}\n'
         'if [ "$1" = status ]; then echo "1,web-1,state,running"; echo "1,web-2,state,not_created"; '
         'echo "1,web-1,provider-name,libvirt"; fi\nexit "${STUB_RC:-0}"\n')
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    return rec


def ans(tmp_path, **kw):
    return Ansible(Proc(tmp_path, tmp_path), cwd=tmp_path, inventory=tmp_path / "inv", **kw)


def test_playbook_argv(tmp_path, bin_dir):
    asyncio.run(ans(tmp_path).playbook("site.yml", "--check", limit=["a", "b"], extra={"x": 1, "y": True}))
    assert bin_dir.read_text().strip() == f"ansible-playbook site.yml --check -i {tmp_path / 'inv'} --limit a,b -e x=1 -e y=true"


@pytest.mark.parametrize("limit", [[], "", ",", ["", ""]])
def test_empty_limit_is_refused_before_anything_runs(tmp_path, bin_dir, limit):
    with pytest.raises(EmptyScope):
        asyncio.run(ans(tmp_path).playbook("site.yml", limit=limit))
    assert not bin_dir.exists()


def test_no_limit_is_allowed(tmp_path, bin_dir):
    # negative control for the refusal: an ABSENT limit is a deliberate whole-inventory run
    asyncio.run(ans(tmp_path).playbook("site.yml"))
    assert "--limit" not in bin_dir.read_text()


def test_failed_playbook_is_a_failed_outcome(tmp_path, bin_dir, monkeypatch):
    monkeypatch.setenv("STUB_RC", "2")
    with pytest.raises(OutcomeError, match="exit 2"):
        asyncio.run(ans(tmp_path).playbook("site.yml"))
    r = asyncio.run(ans(tmp_path).playbook("site.yml", check=False))
    assert r.returncode == 2


def test_group_resolution_walks_children_and_skips_warnings(tmp_path, bin_dir):
    a = ans(tmp_path)
    assert asyncio.run(a.group("web")) == ["w1", "w2", "d1"]
    assert asyncio.run(a.group("empty")) == []
    assert asyncio.run(a.group("nosuch")) == []


def test_container_argv_mounts_and_forwards_by_pattern(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_BACKEND", "zfs")
    monkeypatch.setenv("ANSIBLE_CONFIG", "/x/ansible.cfg")
    monkeypatch.setenv("RIG_IMAGE", "img")
    monkeypatch.setenv("UNRELATED", "1")
    a = ans(tmp_path, image="ctl:1", forward_prefixes=("RIG_", "ANSIBLE_"), never_forward={"RIG_IMAGE"},
            mounts=[tmp_path, tmp_path / "sub"], home=tmp_path / "home")
    argv = a.argv("ansible")
    assert argv[:2] == ["podman", "run"] and argv[-2:] == ["ctl:1", "ansible"]
    assert argv.count("-v") == 1                      # sub is inside tmp_path: one mount
    assert ["-e", "RIG_BACKEND"] == argv[argv.index("RIG_BACKEND") - 1:argv.index("RIG_BACKEND") + 1]
    assert "ANSIBLE_CONFIG" in argv and "RIG_IMAGE" not in argv and "UNRELATED" not in argv
    assert f"HOME={tmp_path / 'home'}" in argv


def test_host_argv_is_just_the_binary(tmp_path):
    assert ans(tmp_path).argv("ansible-galaxy") == ["ansible-galaxy"]


def test_vagrant_status_and_existing(tmp_path, bin_dir):
    vg = Vagrant(Proc(tmp_path, tmp_path), cwd=tmp_path)
    assert asyncio.run(vg.status()) == {"web-1": "running", "web-2": "not_created"}
    assert asyncio.run(vg.existing(["web-2", "web-1", "web-3"])) == ["web-1"]


def test_vagrant_up_never_runs_bare(tmp_path, bin_dir):
    vg = Vagrant(Proc(tmp_path, tmp_path), cwd=tmp_path)
    with pytest.raises(OutcomeError, match="no machine named"):
        asyncio.run(vg.up([]))
    asyncio.run(vg.up(["a", "b"], provider="libvirt", parallel=False))
    assert "vagrant up --no-parallel --provider=libvirt a b" in bin_dir.read_text()


def test_parse_machine_readable_ignores_other_lines():
    assert parse_machine_readable("x\n1,a,state,running\r\n1,a,provider-name,libvirt\n1,,state,x\n") == {"a": "running"}
