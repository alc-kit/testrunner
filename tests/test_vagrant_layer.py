"""testrunner.ext.rig's Vagrant layer (WP §6c, T19/T20): the default Vagrantfile a rig loads and
extends with hooks, or replaces entirely. Every case loads a Vagrantfile with record.rb (a stub
`Vagrant` that records every configuration call) in a throwaway rig — no vagrant, no VM."""
import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

VAGRANT = Path(__file__).resolve().parents[1] / "src" / "testrunner" / "ext" / "rig" / "vagrant"
LIB, RECORD = VAGRANT / "rig.rb", VAGRANT / "record.rb"

pytestmark = pytest.mark.skipif(shutil.which("ruby") is None and not os.environ.get("CI"),
                                reason="needs ruby (CI always has it: a missing ruby FAILS there)")

CATALOGUE = {"machines": [
    {"name": "cache", "kind": "support", "ip": "192.0.2.5", "mem": 512, "cpu": 1, "disks": 0, "size": 0,
     "optional": "apt_cache"},
    {"name": "node-1", "kind": "pve", "ip": "192.0.2.11", "mem": 2048, "cpu": 2, "disks": 2, "size": 4,
     "overrides": {"mem": "node_mem"}},
    {"name": "node-2", "kind": "pve", "ip": "192.0.2.12", "mem": 2048, "cpu": 2, "disks": 2, "size": 4,
     "late": True},
]}
DEFAULT = 'require_relative "testrunner/src/testrunner/ext/rig/vagrant/rig"\nRigVagrant.configure(root: __dir__, network: "toy-net")\n'


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "toy"
    (root / "bin").mkdir(parents=True)
    rn = root / "bin" / "rig-nodes"
    # `json --scenario small` drops node-2 (a rig shape); anything else is the whole catalogue
    small = {"machines": [m for m in CATALOGUE["machines"] if m["name"] != "node-2"]}
    rn.write_text("#!/usr/bin/env python3\nimport sys\n"
                  f"print({json.dumps(json.dumps(small))} if sys.argv[1:] == ['json', '--scenario', 'small'] "
                  f"else {json.dumps(json.dumps(CATALOGUE))})\n")
    rn.chmod(rn.stat().st_mode | stat.S_IEXEC)
    (root / "secrets").mkdir()
    (root / "secrets" / "harness_key.pub").write_text("ssh-ed25519 AAAAtest toy\n")
    (root / "provision").mkdir()
    (root / "provision" / "squid.conf").write_text("http_port 3128\n")
    (root / "testrunner.yml").write_text("ext:\n  rig: {name: toy, prefix: TOY}\n")
    lib = root / "testrunner" / "src" / "testrunner" / "ext" / "rig" / "vagrant"
    lib.mkdir(parents=True)
    shutil.copy(LIB, lib / "rig.rb")
    return root


def record(root, vagrantfile=DEFAULT, env=None, home=None):
    (root / "Vagrantfile").write_text(vagrantfile)
    e = {"PATH": os.environ["PATH"], "HOME": str(home or root / "home"), "LANG": "C.UTF-8", **(env or {})}
    r = subprocess.run(["ruby", str(RECORD), "Vagrantfile"], cwd=root, env=e, capture_output=True, text=True)
    return r, (json.loads(r.stdout) if r.returncode == 0 else None)


def defined(log):
    return [(c[2][0], c[3].get("autostart")) for c in log if c[0] == "config.vm" and c[1] == "define"]


def calls(log, machine, method):
    return [c for c in log if c[0].startswith(f"config.vm.define[{machine}]") and c[1] == method]


def inline(log, machine):
    return [c[3].get("inline", "") for c in calls(log, machine, "provision") if c[2] == ["shell"]]


# ── (a) the default ──────────────────────────────────────────────────────────

def test_default_builds_the_catalogue(rig):
    r, log = record(rig)
    assert r.returncode == 0, r.stderr
    assert defined(log) == [("cache", "true"), ("node-1", "true"), ("node-2", "false")]   # late: no autostart
    net = calls(log, "node-1", "network")[0]
    assert net[3] == {"ip": "192.0.2.11", "libvirt__network_name": "toy-net", "libvirt__dhcp_enabled": "false",
                      "libvirt__forward_mode": "nat"}
    lv = {c[1]: c[2] for c in log if c[0] == "config.vm.define[node-1].vm.provider[libvirt]"}
    assert lv["memory="] == ["2048"] and lv["nic_model_type="] == ["virtio"]
    disks = [c for c in log if c[0] == "config.vm.define[node-1].vm.provider[libvirt]" and c[1] == "storage"]
    assert [d[3]["size"] for d in disks] == ["4G", "4G"]                     # the catalogue's two data disks
    pve = inline(log, "node-1")
    assert "names=1" in pve[0] and "console=1" in pve[0]                     # pve: names + console first
    assert "Acquire::http::Proxy \"http://192.0.2.5:3128\"" in pve[1]         # then the cache client
    assert "ssh-ed25519 AAAAtest toy" in pve[2]                               # then root login
    assert "http_port 3128" in inline(log, "cache")[-1]                        # the squid box, from squid.conf
    assert not any("Acquire::" in s for s in inline(log, "cache"))            # never a client of itself


def test_prefix_and_name_come_from_testrunner_yml(rig):
    r, log = record(rig, env={"TOY_NODE_MEM": "3072", "TOY_NIC_MODEL": "igb"})
    lv = {c[1]: c[2] for c in log if c[0] == "config.vm.define[node-1].vm.provider[libvirt]"}
    assert lv["memory="] == ["3072"]                         # overrides: {mem: node_mem} -> TOY_NODE_MEM
    assert lv["machine_type="] == ["q35"] and lv["nic_model_type="] == ["igb"]
    lv2 = {c[1]: c[2] for c in log if c[0] == "config.vm.define[node-2].vm.provider[libvirt]"}
    assert lv2["memory="] == ["2048"]                        # control: no overrides, catalogue value
    assert "toy: apt cache is serving" in inline(log, "cache")[-1]       # label = ext.rig name


def test_a_bad_knob_is_refused_by_name(rig):
    r, _ = record(rig, env={"TOY_NIC_MODEL": "rtl8139"})
    assert r.returncode != 0 and "TOY_NIC_MODEL must be one of virtio, e1000e, igb (got 'rtl8139')" in r.stderr
    r, _ = record(rig, env={"TOY_VIRTIO_CTRL_VLAN": "off", "TOY_NIC_MODEL": "igb"})
    assert r.returncode != 0 and "needs TOY_NIC_MODEL=virtio" in r.stderr


def test_cache_off_and_a_missing_squid_conf_never_fail_the_vagrantfile(rig):
    r, log = record(rig, env={"TOY_APT_CACHE": "0"})
    assert r.returncode == 0 and [d[0] for d in defined(log)] == ["node-1", "node-2"]
    (rig / "provision" / "squid.conf").unlink()
    r, log = record(rig)
    assert r.returncode == 0, r.stderr                       # a teardown must still load the Vagrantfile
    assert "squid.conf not found" in r.stderr and "apt cache DISABLED" in r.stderr
    assert [d[0] for d in defined(log)] == ["node-1", "node-2"]
    assert not any("Acquire::" in s for s in inline(log, "node-1"))


def test_no_network_name_and_no_hook_is_refused(rig):
    r, _ = record(rig, 'require_relative "testrunner/src/testrunner/ext/rig/vagrant/rig"\n'
                       'RigVagrant.configure(root: __dir__)\n')
    assert r.returncode != 0 and "no network name" in r.stderr


# ── (b) the default with hooks ───────────────────────────────────────────────

HOOKS = textwrap.dedent('''\
    require_relative "testrunner/src/testrunner/ext/rig/vagrant/rig"
    RigVagrant.configure(root: __dir__, cache_prefix: "SHARED", label: "t") do |rig|
      rig.machines { |nodes| nodes.reject { |n, _| n == "node-2" } }
      rig.autostart { |name, spec| true }
      rig.networks do |node, name, spec|
        node.vm.network :private_network, libvirt__network_name: "mgmt", mac: "52:54:00:00:00:01", auto_config: false
        node.vm.network :private_network, libvirt__network_name: "extra", auto_config: false if spec[:kind] == "pve"
      end
      rig.domain { |lv, name, spec| lv.qemuargs value: "-hooked" }
      rig.provision(:after_names) do |node, name, spec|
        node.vm.provision "shell", privileged: true, inline: "echo after-names #{name}"
      end
      rig.provision(:after_key) { |node, name, spec| node.vm.provision "shell", inline: "echo after-key" }
    end
''')


def test_hooks_change_only_what_they_name(rig):
    r, log = record(rig, HOOKS, env={"SHARED_APT_CACHE_HTTP": "0"})
    assert r.returncode == 0, r.stderr
    assert defined(log) == [("cache", "true"), ("node-1", "true")]          # machines + autostart hooks
    nets = [c[3]["libvirt__network_name"] for c in calls(log, "node-1", "network")]
    assert nets == ["mgmt", "extra"]                                         # the default network is replaced
    q = [c[3]["value"] for c in log if c[0] == "config.vm.define[node-1].vm.provider[libvirt]" and c[1] == "qemuargs"]
    assert q[-1] == "-hooked" and "ipmi-bmc-sim,id=bmc0" in q                # domain hook AFTER the defaults
    order = inline(log, "node-1")
    assert "names=1" in order[0] and order[1] == "echo after-names node-1" and "Acquire::" in order[2]
    assert "chpasswd" in order[3] and order[4] == "echo after-key"
    assert "deb.debian.org" not in order[2]                                  # cache_prefix: SHARED_APT_CACHE_HTTP=0
    assert "t: apt cache is serving" in inline(log, "cache")[-1]


def test_an_unknown_provision_position_is_refused(rig):
    bad = HOOKS.replace("rig.provision(:after_key)", "rig.provision(:somewhere)")
    r, _ = record(rig, bad)
    assert r.returncode != 0 and "provision position :somewhere is not one of" in r.stderr


# ── (c) a Vagrantfile of the rig's own: the library is not involved ──────────

def test_a_custom_vagrantfile_replaces_everything(rig):
    custom = 'Vagrant.configure("2") do |config|\n  config.vm.box = "other/box"\n  config.vm.define "solo"\nend\n'
    (rig / "testrunner" / "src" / "testrunner" / "ext" / "rig" / "vagrant" / "rig.rb").unlink()   # not even present
    r, log = record(rig, custom)
    assert r.returncode == 0, r.stderr
    assert [c[1:3] for c in log if c[1] != "vm"] == [["box=", ["other/box"]], ["define", ["solo"]]]


# ── controls: the recorder sees a library change ─────────────────────────────

@pytest.mark.parametrize("old,new,what", [
    ('        lv.qemuargs value: "isa-ipmi-kcs,bmc=bmc0"\n', "", "the IPMI device"),
    ("@hooks[:autostart] ? @hooks[:autostart].call(name, spec) : !spec[:late]", "true", "late autostart"),
    ("spec[field.to_sym] = v if v > 0", "nil", "resource overrides"),
])
def test_control_a_library_mutation_changes_the_record(rig, old, new, what):
    r, before = record(rig, env={"TOY_NODE_MEM": "3072"})
    lib = rig / "testrunner" / "src" / "testrunner" / "ext" / "rig" / "vagrant" / "rig.rb"
    text = lib.read_text()
    assert text.count(old) == 1, what
    lib.write_text(text.replace(old, new))
    r, after = record(rig, env={"TOY_NODE_MEM": "3072"})
    assert r.returncode == 0 and after != before, what


def test_catalogue_args_pick_a_shape_of_the_rig(rig):
    vf = DEFAULT.replace('network: "toy-net")', 'network: "toy-net", catalogue_args: ["--scenario", "small"])')
    r, log = record(rig, vf)
    assert r.returncode == 0, r.stderr
    assert [d[0] for d in defined(log)] == ["cache", "node-1"]
    r, log = record(rig)                                                    # control: no args, the whole table
    assert [d[0] for d in defined(log)] == ["cache", "node-1", "node-2"]
