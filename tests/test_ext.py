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


def test_none_in_env_unsets_and_never_becomes_the_string_none(tmp_path, bin_dir, monkeypatch):
    # measured 2026-10-11: a config's unset value reached the inventory as "None"
    monkeypatch.setenv("TR_X_INHERITED", "from-the-shell")
    stub(tmp_path / "bin", "ansible", 'printf "%s\\n" "X=${TR_X-<unset>} I=${TR_X_INHERITED-<unset>}"\n')
    a = ans(tmp_path, env={"TR_X": None, "TR_X_INHERITED": None, "TR_Y": "y"})
    r = asyncio.run(a.adhoc("localhost", "ping"))
    assert "X=<unset> I=<unset>" in r.text
    # container mode: forwarded by NAME only (the engine passes nothing for an unset one),
    # never as a value
    c = ans(tmp_path, env={"ANSIBLE_GONE": None, "ANSIBLE_KEPT": "1"}, image="img")
    argv = c.argv("ansible")
    assert "ANSIBLE_KEPT" in argv and "ANSIBLE_GONE" in argv
    assert not any(a.startswith("ANSIBLE_GONE=") or a == "None" for a in argv)


BROKEN_INVENTORY = r'''echo "[WARNING]: Failed to parse inventory with 'script' plugin: Inventory script returned non-zero exit code 1."
echo "Standard error from inventory script:"
echo "inv: SHAPE=None: nodes.yml declares no such shape"
echo "[WARNING]: No inventory was parsed, only implicit localhost is available"
'''


def test_hostvar_refuses_an_inventory_that_did_not_parse(tmp_path, bin_dir):
    # ansible exits 0 with an empty inventory: the probe must not answer "" as if real
    stub(tmp_path / "bin", "ansible", BROKEN_INVENTORY + 'echo \'localhost | SUCCESS => {\'; echo \'    "msg": ""\'; echo "}"\n')
    with pytest.raises(OutcomeError, match="did not parse.*declares no such shape"):
        asyncio.run(ans(tmp_path).hostvar("n1", "backend"))


def test_hostvar_answers_from_a_healthy_inventory(tmp_path, bin_dir):
    # negative control: same stub shape, no inventory warning -> the value, and "" for undefined
    stub(tmp_path / "bin", "ansible", 'echo \'localhost | SUCCESS => {\'; echo \'    "msg": "zfs"\'; echo "}"\n')
    assert asyncio.run(ans(tmp_path).hostvar("n1", "backend")) == "zfs"
    stub(tmp_path / "bin", "ansible", 'echo \'localhost | SUCCESS => {\'; echo \'    "msg": ""\'; echo "}"\n')
    assert asyncio.run(ans(tmp_path).hostvar("n1", "backend")) == ""


def test_hostvar_refuses_a_failed_probe(tmp_path, bin_dir, monkeypatch):
    monkeypatch.setenv("STUB_RC", "4")
    with pytest.raises(OutcomeError, match="exit 4"):
        asyncio.run(ans(tmp_path).hostvar("n1", "backend"))


def test_group_refuses_an_inventory_that_did_not_parse(tmp_path, bin_dir):
    stub(tmp_path / "bin", "ansible-inventory", BROKEN_INVENTORY + 'echo \'{"all": {"children": []}}\'\n')
    with pytest.raises(OutcomeError, match="did not parse"):
        asyncio.run(ans(tmp_path).group("web"))


def test_console_capture_refuses_by_name_when_a_tool_is_missing(tmp_path, monkeypatch):
    # measured 2026-10-11: no `script` on a Fedora 44 host -> a bare FileNotFoundError
    # five minutes into `up`; now a named refusal, and a check to run BEFORE building
    from testrunner.ext import console
    real_which = console.shutil.which
    monkeypatch.setattr(console.shutil, "which", lambda t: None if t == "script" else real_which(t))
    assert [m for m in console.missing_tools() if m.startswith("script")] and \
        "util-linux-script" in " ".join(console.missing_tools())
    with pytest.raises(console.ConsoleUnavailable, match="script"):
        console.ConsoleCapture(tmp_path).start("n1", "dom-uuid")
    assert not (tmp_path / "n1.pid").exists()


def test_console_tools_present_means_nothing_missing(monkeypatch):
    # negative control: with both tools found, nothing is reported missing
    from testrunner.ext import console
    monkeypatch.setattr(console.shutil, "which", lambda t: f"/usr/bin/{t}")
    assert console.missing_tools() == []
