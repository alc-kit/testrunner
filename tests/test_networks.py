"""A rig's libvirt networks as regions (WP testrunner-state-validation §10 B6c). Each region is binary,
absent / up; a network with the wrong parameters or not active holds neither value (undefined =
failure); virsh that does not answer is "cannot tell"; the rig must say what a correct network is."""
import asyncio

import pytest
import yaml

from test_ext_rig import DEFAULT, rig_root  # noqa: F401  (rig_root is a fixture)
from testrunner.api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS, MARK
from testrunner.ext.rig import Rig, kinds
from testrunner.ext.rig import networks
from testrunner.ext.rig.networks import NET_VALUES, judge_network, read_network

INFO = "Name:           n\nUUID:           x\nActive:         {a}\nPersistent:     {p}\nAutostart:      {s}\nBridge:  b\n"
GONE = "error: failed to get network 'n'\nerror: Network not found: no network with matching name 'n'\n"
XML = "<network><name>n</name></network>"


class Result:
    def __init__(self, rc, text):
        self.returncode, self.text = rc, text


class FakeProc:
    """Answers net-info / net-dumpxml from a table; records every call and its environment."""
    def __init__(self, info, xml=(0, XML)):
        self.info, self.xml, self.calls = info, xml, []

    async def run(self, cmd, env=None, **kw):
        self.calls.append((cmd, env))
        return Result(*(self.info if cmd[1] == "net-info" else self.xml))


class FakeRig:
    def __init__(self, correct=(True, "isolated")):
        self.correct, self.asked = correct, []

    async def network_correct(self, name, live_xml):
        self.asked.append((name, live_xml))
        return self.correct


def held(rig, reading):
    return [v for v in NET_VALUES if asyncio.run(judge_network(rig, "n", reading, v))[0] == HOLDS]


def read(info, xml=(0, XML)):
    proc = FakeProc(info, xml)
    return asyncio.run(read_network(proc, "n")), proc


@pytest.mark.parametrize("name,info,correct,holds", [
    ("not defined", (1, GONE), (True, ""), "absent"),
    ("defined, active, autostart, correct", (0, INFO.format(a="yes", p="yes", s="yes")), (True, "ok"), "up"),
    ("not active", (0, INFO.format(a="no", p="yes", s="yes")), (True, "ok"), None),
    ("no autostart", (0, INFO.format(a="yes", p="yes", s="no")), (True, "ok"), None),
    ("transient", (0, INFO.format(a="yes", p="no", s="no")), (True, "ok"), None),
    ("wrong parameters (a breached isolation)", (0, INFO.format(a="yes", p="yes", s="yes")), (False, "<ip>"), None),
])
def test_at_most_one_value_holds(name, info, correct, holds):
    reading, _ = read(info)
    assert held(FakeRig(correct), reading) == ([holds] if holds else []), name


def test_the_rig_is_asked_with_the_live_definition_only_once_the_network_runs():
    rig = FakeRig()
    reading, _ = read((0, INFO.format(a="yes", p="yes", s="yes")))
    asyncio.run(judge_network(rig, "n", reading, "up"))
    assert rig.asked == [("n", XML)]
    rig = FakeRig()
    reading, _ = read((0, INFO.format(a="no", p="yes", s="yes")))
    asyncio.run(judge_network(rig, "n", reading, "up"))
    assert rig.asked == []


@pytest.mark.parametrize("info,xml", [
    ((1, "error: failed to connect to the hypervisor\nerror: access denied by policy\n"), (0, XML)),
    ((0, "Name: n\n"), (0, XML)),                                        # no flags: unreadable
    ((0, INFO.format(a="yes", p="yes", s="yes")), (1, "error: x\n")),   # the definition unreadable
])
def test_virsh_that_does_not_answer_cannot_tell(info, xml):
    reading, _ = read(info, xml)
    assert all(asyncio.run(judge_network(FakeRig(), "n", reading, v))[0] == CANNOT_TELL for v in NET_VALUES)


def test_the_rig_that_cannot_tell_makes_up_cannot_tell():
    reading, _ = read((0, INFO.format(a="yes", p="yes", s="yes")))
    assert asyncio.run(judge_network(FakeRig((None, "?")), "n", reading, "up"))[0] == CANNOT_TELL


def test_every_virsh_call_runs_untranslated():
    _, proc = read((0, INFO.format(a="yes", p="yes", s="yes")))
    assert [c[0][:2] for c in proc.calls] == [["virsh", "net-info"], ["virsh", "net-dumpxml"]]
    assert all(env == {"LC_ALL": "C"} for _, env in proc.calls)


def test_a_value_that_is_not_a_network_value_does_not_hold():
    reading, _ = read((1, GONE))
    assert asyncio.run(judge_network(FakeRig(), "n", reading, "running"))[0] == DOES_NOT_HOLD


def test_the_kind_is_exported_where_discovery_looks():
    mark = getattr(kinds.libvirt_net_validator, MARK)
    assert mark.name == "libvirt_net" and kinds.libvirt_net_validator is networks.libvirt_net_validator


def test_a_rig_that_does_not_say_what_a_correct_network_is_cannot_tell(rig_root):  # noqa: F811
    rig = Rig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    ok, why = asyncio.run(rig.network_correct("n", XML))
    assert ok is None and "network_correct" in why
