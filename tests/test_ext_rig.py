"""testrunner.ext.rig: a vagrant + ansible test rig described by its runner config
(`ext: {rig: ...}`), its node catalogue and its config schema. An invented rig: cluster kind
`node`, prefix MYRIG, two support boxes (one optional), three nodes and a late one, two
scenarios, roles. Every rule is paired with the input that must be refused."""
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from testrunner import OutcomeError
from testrunner.config import ConfigError
from testrunner.ext.rig import config as rc_engine
from testrunner.ext.rig import nodes as rn
from testrunner.ext.rig import drive_rules
from testrunner.ext.rig.core import Catalogue, Rig, render_rules
from testrunner.ext.rig.settings import load_settings, settings_from

SRC = Path(__file__).resolve().parents[1] / "src"

RUNNER = """
modules: []
ext:
  rig:
    name: myrig
    prefix: MYRIG
    cluster_kind: node
    cluster_noun: Widget
    stale_files: [secrets/recovery_key.txt]
"""
SCHEMA = """
config_env: MYRIG_CONFIG
default_link: myrig.yml
sections: [rig, harness, run]
keys:
  rig.networking: {type: str, allowed: [flat, vlan], env: MYRIG_NETWORKING}
  rig.shape: {type: str, allowed_from: {file: lifecycle/nodes.yml, key: shapes}, env: MYRIG_SHAPE}
  rig.cache.enabled: {type: bool, env: MYRIG_CACHE}
  rig.source: {type: path, env: MYRIG_SOURCE}
  rig.nodes: {type: list, env: MYRIG_NODES}
  rig.console: {type: str, allowed: [log, "off"], env: MYRIG_CONSOLE}
  harness.keep: {type: bool, env: MYRIG_KEEP, when_false: unset}
  run.verbose: {type: bool, env: MYRIG_VERBOSE}
rules:
  - {when: {rig.shape: set}, require: {rig.networking: vlan}, why: "a shape needs vlan networking"}
"""
DEFAULT = """
rig: {networking: flat, shape: null, cache: {enabled: true}, source: null, nodes: null, console: "off"}
harness: {keep: false}
run: {verbose: false}
plan: []
"""
CATALOGUE = """
machines:
  cache: {kind: support, ip: 192.0.2.9,  mem: 512, cpu: 1, disks: 0, size: 0, optional: cache}
  hub:   {kind: support, ip: 192.0.2.10, mem: 512, cpu: 1, disks: 0, size: 0}
  n1:    {kind: node, ip: 192.0.2.51, ip6: "2001:db8::51", mem: 1024, cpu: 1, disks: 2, size: 4}
  n2:    {kind: node, ip: 192.0.2.52, mem: 1024, cpu: 1, disks: 2, size: 4}
  n3:    {kind: node, ip: 192.0.2.53, mem: 1024, cpu: 1, disks: 2, size: 4}
  n4:    {kind: node, ip: 192.0.2.54, mem: 1024, cpu: 1, disks: 2, size: 4, late: true}
  arb:   {kind: support, ip: 192.0.2.60, mem: 512, cpu: 1, disks: 0, size: 0}
roles:
  founder: n1
  victims: [n2, n3]
scenarios:
  three: [cache, hub, n1, n2, n3, n4]
  pair: [cache, hub, n1, n2, arb]
default_scenario: three
shapes: {wide: {}, narrow: {}}
"""


@pytest.fixture
def rig_root(tmp_path):
    root = tmp_path / "myrig"
    for rel, text in (("testrunner.yml", RUNNER), ("configs/schema.yml", SCHEMA), ("configs/default.yml", DEFAULT),
                      ("lifecycle/nodes.yml", CATALOGUE)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for k in [k for k in os.environ if k.startswith("MYRIG_")]:
        os.environ.pop(k)
    return root


def tool(root, module, *args, env=None):
    """A tool's command line, as a Vagrantfile or a shell calls it."""
    e = {**os.environ, "PYTHONPATH": str(SRC), **(env or {})}
    return subprocess.run([sys.executable, "-m", f"testrunner.ext.rig.{module}", "--root", str(root), *args],
                          capture_output=True, text=True, env=e)


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_come_from_the_runner_config(rig_root):
    st = load_settings(rig_root)
    assert (st.prefix, st.cluster_kind, st.cluster_noun, st.stale_files) == ("MYRIG", "node", "Widget",
                                                                             ("secrets/recovery_key.txt",))
    assert st.kinds == ("node", "support") and st.schema_file == rig_root / "configs" / "schema.yml"


@pytest.mark.parametrize("spec,msg", [
    ({"colour": "red"}, "unknown key"),
    ({"cluster_kind": "support"}, "an identifier other than `support`"),
    ({"cluster_kind": "two words"}, "an identifier"),
    ({"inventory": {"conditional": "x"}}, r"inventory: want \{group"),
    ({"stale_files": "one"}, "stale_files: want a list"),
])
def test_bad_settings_are_refused(tmp_path, spec, msg):
    with pytest.raises(ConfigError, match=msg):
        settings_from(tmp_path, spec)


def test_the_runner_config_refuses_a_bad_ext(rig_root):
    (rig_root / "testrunner.yml").write_text("modules: []\next: {rig: 3}\n")
    with pytest.raises(ConfigError, match="ext: want"):
        load_settings(rig_root)


# ── the config engine ────────────────────────────────────────────────────────

def test_a_config_resolves_to_the_rigs_environment(rig_root):
    r = tool(rig_root, "config", "env", "--file", str(rig_root / "configs/default.yml"), "--how", "default")
    assert r.returncode == 0, r.stderr
    assert "export MYRIG_NETWORKING=flat" in r.stdout and "export MYRIG_CACHE=1" in r.stdout
    assert "unset MYRIG_KEEP" not in r.stdout                # unset only when it was set


def test_a_path_is_made_absolute_against_the_rig_root(rig_root):
    st = load_settings(rig_root)
    schema = rc_engine.load_schema(st)
    env = rc_engine.env_of({"rig.source": "src/tree"}, schema)
    assert env["MYRIG_SOURCE"] == str(rig_root / "src" / "tree")


@pytest.mark.parametrize("yaml_text,msg", [
    ("extends: default\nrig: {networking: star}", "must be one of flat, vlan"),
    ("extends: default\nrig: {shape: round}", "must be one of narrow, wide"),       # allowed_from the catalogue
    ("extends: default\nrig: {shape: wide}", "a shape needs vlan networking"),      # a rule
    ("extends: default\nrigs: {}", "unknown top-level key"),
    ("extends: default\nrig: {cache: {enabld: true}}", "unknown key(s) rig.cache.enabld"),
])
def test_bad_configs_are_refused(rig_root, yaml_text, msg):
    f = rig_root / "configs" / "bad.yml"
    f.write_text(yaml_text + "\n")
    r = tool(rig_root, "config", "show", "--file", str(f))
    assert r.returncode == 2 and msg in r.stderr, r.stderr


def test_select_uses_the_rigs_link_and_env(rig_root):
    r = tool(rig_root, "config", "select", env={"MYRIG_CONFIG": ""})
    assert r.stdout.strip().endswith("configs/default.yml\tdefault")
    (rig_root / "configs" / "other.yml").write_text("extends: default\n")
    (rig_root / "myrig.yml").symlink_to("configs/other.yml")
    assert tool(rig_root, "config", "select").stdout.strip().endswith("configs/other.yml\tsymlink")
    assert tool(rig_root, "config", "select", env={"MYRIG_CONFIG": "default"}).stdout.strip().endswith("\tenv")


def test_two_rigs_in_one_process_keep_their_own_roots(rig_root, tmp_path):
    other = tmp_path / "other"
    for rel in ("testrunner.yml", "configs/schema.yml", "lifecycle/nodes.yml"):
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text((rig_root / rel).read_text())
    a, b = (rc_engine.load_schema(load_settings(r)) for r in (rig_root, other))
    assert rc_engine.env_of({"rig.source": "x"}, a)["MYRIG_SOURCE"] == str(rig_root / "x")
    assert rc_engine.env_of({"rig.source": "x"}, b)["MYRIG_SOURCE"] == str(other / "x")


# ── the node catalogue ───────────────────────────────────────────────────────

def test_list_and_the_cluster_kind(rig_root):
    assert tool(rig_root, "nodes", "list").stdout.split() == ["cache", "hub", "n1", "n2", "n3"]
    assert tool(rig_root, "nodes", "list", "--kind", "node").stdout.split() == ["n1", "n2", "n3"]
    assert tool(rig_root, "nodes", "list", "--scenario", "pair", "--off", "cache").stdout.split() == \
        ["hub", "n1", "n2", "arb"]
    r = tool(rig_root, "nodes", "list", "--kind", "pve")
    assert r.returncode == 2 and "--kind must be one of node, support" in r.stderr


def test_shell_names_follow_the_cluster_kind(rig_root):
    out = dict(ln.split("=", 1) for ln in tool(rig_root, "nodes", "shell").stdout.splitlines())
    assert out["RIG_NODE_NODES"] == "'n1 n2 n3'" and out["RIG_LATE_NODES"] == "n4"
    assert out["RIG_ALL_NODE_NODES"] == "'n1 n2 n3 n4'" and out["RIG_CATALOGUE_NODE"] == "'n1 n2 n3 n4'"
    assert out["RIG_ROLE_victims"] == "'n2 n3'" and out["RIG_FEATURE_cache"] == "cache"


def test_json_is_clean_on_stdout_and_stderr(rig_root):
    """A Vagrantfile reads the json with stderr mixed in: nothing else may be printed."""
    r = subprocess.run(f"{sys.executable} -m testrunner.ext.rig.nodes --root {rig_root} json 2>&1",
                       shell=True, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert [m["name"] for m in json.loads(r.stdout)["machines"]] == ["cache", "hub", "n1", "n2", "n3", "n4"]


@pytest.mark.parametrize("edit,msg", [
    (("machines", "n2", "kind", "vm"), ".kind must be one of node, support"),
    (("machines", "hub", "late", True), "is late but not a Widget node"),
    (("roles", "founder", None, "hub"), "which is not a Widget node"),
])
def test_validation_speaks_the_rigs_terms(rig_root, edit, msg):
    import yaml
    data = yaml.safe_load(CATALOGUE)
    top, name, field, value = edit
    if field is None:
        data[top][name] = value
    else:
        data[top][name][field] = value
    (rig_root / "lifecycle" / "nodes.yml").write_text(yaml.safe_dump(data))
    r = tool(rig_root, "nodes", "validate")
    assert r.returncode == 1 and msg in r.stderr, r.stderr


def test_the_inventory_check_is_off_unless_configured(rig_root):
    import yaml
    data = yaml.safe_load(CATALOGUE)
    data["cluster_hosts"] = {"n1": {"ansible_host": "192.0.2.99"}}       # disagrees, and lacks n2-n4
    (rig_root / "lifecycle" / "nodes.yml").write_text(yaml.safe_dump(data))
    assert tool(rig_root, "nodes", "validate").returncode == 0
    (rig_root / "testrunner.yml").write_text(RUNNER + "    inventory: {group: cluster_hosts, v6_var: host_v6}\n")
    r = tool(rig_root, "nodes", "validate")
    assert r.returncode == 1 and "cluster_hosts (n1) and the machines of kind node" in r.stderr
    assert "n1: inventory ansible_host 192.0.2.99 disagrees" in r.stderr


# ── drive rules ──────────────────────────────────────────────────────────────

def test_drive_rules_render_and_refuse(tmp_path):
    t = tmp_path / "t.json"
    t.write_text(json.dumps([{"expect": "remove @VICTIM@\\?", "send": "@VICTIM@\r"}]))
    assert render_rules(t, VICTIM="n2") == [{"expect": "remove n2\\?", "send": "n2\r"}]
    with pytest.raises(OutcomeError, match="needs VICTIM"):
        render_rules(t)
    with pytest.raises(OutcomeError, match="does not use OTHER"):
        render_rules(t, VICTIM="n2", OTHER="x")
    t.write_text(json.dumps([{"expect": "go\\?", "send": "yes"}]))
    with pytest.raises(OutcomeError, match="no trailing"):
        render_rules(t)
    r = subprocess.run([sys.executable, "-m", "testrunner.ext.rig.drive_rules", "check", str(t)],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert r.returncode != 0
    assert callable(drive_rules.main)


# ── the library ──────────────────────────────────────────────────────────────

def rig_of(root, config=None, how="default"):
    import yaml
    return Rig(root, config or yaml.safe_load(DEFAULT), root / "configs" / "default.yml", how)


def test_the_catalogue_speaks_the_cluster_kind(rig_root):
    cat = Catalogue(load_settings(rig_root), "pair")
    assert cat.cluster() == ["n1", "n2"] and cat.cluster_nodes() == ["n1", "n2"]
    assert cat.catalogue_cluster() == ["n1", "n2", "n3", "n4"] and cat.role("victims") == ["n2", "n3"]
    assert cat.up_machines(["n2"], off=("cache",)) == ["hub", "n2", "arb"]
    with pytest.raises(OutcomeError, match="rig.nodes names hub"):
        cat.up_machines(["hub"])


def test_the_rig_takes_its_identity_from_the_settings(rig_root):
    rig = rig_of(rig_root)
    assert (rig.PREFIX, rig.NAME, rig.STALE_FILES) == ("MYRIG", "myrig", ("secrets/recovery_key.txt",))
    assert rig.e("NETWORKING") == "flat" and rig.config_env()["MYRIG_CACHE"] == "1"


def test_a_chosen_config_refuses_a_contradicting_environment(rig_root, monkeypatch):
    monkeypatch.setenv("MYRIG_NETWORKING", "vlan")
    with pytest.raises(OutcomeError, match="MYRIG_NETWORKING=vlan in the environment"):
        rig_of(rig_root, how="--config").config_env()
    assert rig_of(rig_root, how="default").config_env()["MYRIG_NETWORKING"] == "vlan"   # control: kept


def stub_vagrant(tmp_path, monkeypatch, states):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    lines = "\\n".join(f"1,{n},state,{s}" for n, s in states.items())
    (bindir / "vagrant").write_text(f"#!/bin/sh\nprintf '{lines}\\n'\n")
    (bindir / "vagrant").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def test_live_nodes_ask_every_cluster_machine_and_warn_only_for_the_scenarios(rig_root, tmp_path, monkeypatch,
                                                                              caplog):
    from testrunner.proc import Proc
    rig = rig_of(rig_root)                               # default scenario `three`: n1-n4
    stub_vagrant(tmp_path, monkeypatch, {"hub": "running", "n1": "running", "n2": "shutoff", "n4": "not_created"})
    with caplog.at_level(logging.DEBUG):
        live = asyncio.run(rig.live_nodes(rig.vagrant(Proc(tmp_path, tmp_path / "logs"))))
    assert live == ["n1", "n2"]
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("vagrant does not define ['n3'], which scenario" in w for w in warned), warned
    assert any("live Widget nodes" in r.getMessage() for r in caplog.records)


def test_a_node_outside_the_scenario_is_not_a_warning(rig_root, tmp_path, monkeypatch, caplog):
    from testrunner.proc import Proc

    class PairRig(Rig):                                  # a rig in scenario `pair`: n1, n2 only
        def scenario(self):
            return "pair"

    import yaml
    rig = PairRig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    stub_vagrant(tmp_path, monkeypatch, {"hub": "running", "n1": "running", "n2": "running", "arb": "running"})
    with caplog.at_level(logging.DEBUG):
        live = asyncio.run(rig.live_nodes(rig.vagrant(Proc(tmp_path, tmp_path / "logs"))))
    assert live == ["n1", "n2"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any("not in this scenario: ['n3', 'n4']" in r.getMessage() for r in caplog.records)


def test_an_empty_scope_is_refused(rig_root, tmp_path, monkeypatch):
    from testrunner.proc import Proc
    rig = rig_of(rig_root)
    stub_vagrant(tmp_path, monkeypatch, {"hub": "running", "n1": "not_created"})
    with pytest.raises(OutcomeError, match="no Widget node in this rig is created"):
        asyncio.run(rig.live_limit(rig.vagrant(Proc(tmp_path, tmp_path / "logs"))))


def test_stale_files_come_from_the_settings(rig_root, tmp_path, monkeypatch):
    from testrunner.proc import Proc
    rig = rig_of(rig_root)
    for n in ("n1", "n2"):
        f = rig_root / "inventory" / "host_vars" / n / "secrets" / "recovery_key.txt"
        f.parent.mkdir(parents=True)
        f.write_text("old")
    stub_vagrant(tmp_path, monkeypatch, {"n1": "running", "n2": "not_created"})
    asyncio.run(rig.purge_stale_node_state(rig.vagrant(Proc(tmp_path, tmp_path / "logs"))))
    assert (rig_root / "inventory/host_vars/n1/secrets/recovery_key.txt").exists()       # exists: kept
    assert not (rig_root / "inventory/host_vars/n2/secrets/recovery_key.txt").exists()   # re-created: purged


def test_the_package_imports_nothing_until_asked():
    """`python -m testrunner.ext.rig.nodes` must not find its module pre-imported (runpy then
    warns on stderr, into a Vagrantfile's json)."""
    r = subprocess.run([sys.executable, "-W", "error", "-c",
                        "import sys, testrunner.ext.rig; print(sorted(m for m in sys.modules if m.startswith('testrunner.ext.rig.')))"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stdout + r.stderr


def test_the_generated_ansible_cfg_skips_a_pycache_in_the_inventory(rig_root, tmp_path):
    # Python leaves inventory/__pycache__ once anything imports an inventory script; ansible
    # then warns it cannot parse it (lab qdevice run b0e914d8, 2026-10-12). The generated
    # config tells ansible to skip it, so the warning never reaches the inventory probe.
    import shutil
    import yaml

    class R(Rig):
        @property
        def ansible_dir(self):
            return rig_root / "ansible"
    R(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml").ensure_config()
    cfg = (rig_root / "ansible.cfg").read_text()
    assert "\ninventory_ignore_patterns = __pycache__\n" in cfg
    exe = shutil.which("ansible-inventory")
    if not exe:
        pytest.skip("ansible-inventory not installed: the behaviour half needs it")
    inv = tmp_path / "inv"
    (inv / "__pycache__").mkdir(parents=True)
    (inv / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"")
    (inv / "hosts.yml").write_text("all:\n  hosts:\n    h1:\n")
    out = {}
    for name, path in (("generated", rig_root / "ansible.cfg"), ("bare", tmp_path / "bare.cfg")):
        if name == "bare":
            path.write_text("[defaults]\n")
        r = subprocess.run([exe, "-i", str(inv), "--list"], capture_output=True, text=True,
                           env={**os.environ, "ANSIBLE_CONFIG": str(path)})
        out[name] = r.stdout + r.stderr
    assert "h1" in out["generated"] and "__pycache__" not in out["generated"]
    # negative control: the same inventory WITHOUT the setting does warn, so the check above can fail
    assert "Unable to parse" in out["bare"] and "__pycache__" in out["bare"]


# ── mode (d): a rig without vagrant — its own machine provider (WP §6c.6) ────────────────────

class FakeProvider:
    """A provider of the rig's own: no vagrant, no libvirt."""
    def __init__(self, states):
        self.states, self.calls = dict(states), []

    async def status(self):
        self.calls.append(("status",))
        return dict(self.states)

    async def up(self, machines, **kw):
        self.calls.append(("up", tuple(machines), tuple(sorted(kw))))
        self.states.update({m: "running" for m in machines})

    async def destroy(self, machines=None, **kw):
        self.calls.append(("destroy", machines))
        for m in machines or list(self.states):
            self.states[m] = "not_created"


def no_vagrant(tmp_path, monkeypatch):
    """A `vagrant` that FAILS loudly: a test that passes with it did not need vagrant."""
    bindir = tmp_path / "nobin"
    bindir.mkdir(exist_ok=True)
    (bindir / "vagrant").write_text("#!/bin/sh\necho 'vagrant was called' >&2\nexit 99\n")
    (bindir / "vagrant").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def test_a_rig_with_its_own_provider_never_runs_vagrant(rig_root, tmp_path, monkeypatch):
    from testrunner.ext.machines import MachineProvider
    from testrunner.proc import Proc
    no_vagrant(tmp_path, monkeypatch)
    fake = FakeProvider({"n1": "running", "n2": "running", "n3": "not_created"})
    assert isinstance(fake, MachineProvider)

    class OwnRig(Rig):
        def machines(self, proc, params=None):
            return fake
    import yaml
    rig = OwnRig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    p = Proc(tmp_path, tmp_path / "logs")
    assert asyncio.run(rig.live_nodes(rig.machines(p))) == ["n1", "n2"]
    assert asyncio.run(rig.observe_rig({"rig": "installed"})) == "installed"      # the observer asks the provider
    with pytest.raises(OutcomeError, match="needs n3"):
        asyncio.run(rig.require_nodes(rig.machines(p), "stage", "n3"))
    fake.states = {n: "not_created" for n in fake.states}
    assert asyncio.run(rig.observe_rig({"rig": "installed"})) == "absent"
    assert all(c[0] == "status" for c in fake.calls)


def test_control_the_default_provider_is_vagrant(rig_root, tmp_path, monkeypatch):
    from testrunner.ext.vagrant import Vagrant
    from testrunner.proc import Proc
    no_vagrant(tmp_path, monkeypatch)
    rig = rig_of(rig_root)
    assert isinstance(rig.machines(Proc(tmp_path, tmp_path / "logs")), Vagrant)
    with pytest.raises(OutcomeError, match="vagrant status. failed"):           # and it really runs vagrant
        asyncio.run(rig.observe_rig({"rig": "installed"}))


def test_the_machines_walk_kind_uses_the_rigs_provider(rig_root, tmp_path, monkeypatch):
    from testrunner.ext.rig import kinds as k
    from testrunner.proc import Proc
    no_vagrant(tmp_path, monkeypatch)
    fake = FakeProvider({"n4": "not_created"})

    class OwnRig(Rig):
        def machines(self, proc, params=None):
            return fake
    import yaml
    rig = OwnRig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    p = Proc(tmp_path, tmp_path / "logs")
    asyncio.run(k.machines_step({"machines": "up", "names": ["n4"], "parallel": False}, rig, p, {}))
    asyncio.run(k.machines_step({"machines": "destroy", "names": "all"}, rig, p, {}))
    assert fake.calls == [("up", ("n4",), ("parallel",)), ("destroy", None)]
    for bad, msg in (({"machines": "up"}, "no machine named"), ({"machines": "destroy"}, "names: all"),
                     ({"machines": "halt", "names": ["n4"]}, "not up or destroy")):
        with pytest.raises(OutcomeError, match=msg):
            asyncio.run(k.machines_step(bad, rig, p, {}))
