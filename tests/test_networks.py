"""A rig's libvirt networks as regions (WP testrunner-state-validation §10 B6c). Each region is binary,
absent / up; a network with the wrong parameters or not active holds neither value (undefined =
failure); virsh that does not answer is "cannot tell"; the rig must say what a correct network is.
One look reads the networks ONCE (one `net-list`, each definition at most once): every virsh call is
an authorization check under polkit, and polkitd ran out of descriptors under the per-value reads."""
import asyncio

import pytest
import yaml

from test_ext_rig import DEFAULT, rig_root  # noqa: F401  (rig_root is a fixture)
from testrunner.api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS, MARK
from testrunner.ext.rig import Rig, kinds
from testrunner.ext.rig import networks
from testrunner.ext.rig.networks import NET_VALUES, NetworksLook, judge_network

HEAD = " Name                    State    Autostart   Persistent\n" + "-" * 58 + "\n"
XML = "<network><name>n</name></network>"


def table(*rows):
    return HEAD + "".join(f" {name:<23} {state:<8} {auto:<11} {pers}\n" for name, state, auto, pers in rows) + "\n"


UP = ("n", "active", "yes", "yes")


class Result:
    def __init__(self, rc, text):
        self.returncode, self.text = rc, text


class FakeProc:
    """Answers net-list / net-dumpxml; records every call and its environment."""
    def __init__(self, listing, xml=(0, XML)):
        self.listing, self.xml, self.calls = listing, xml, []

    async def run(self, cmd, env=None, **kw):
        self.calls.append((cmd, env))
        return Result(*(self.listing if cmd[1] == "net-list" else self.xml))


class FakeRig:
    def __init__(self, correct=(True, "isolated")):
        self.correct, self.asked = correct, []

    async def network_correct(self, name, live_xml):
        self.asked.append((name, live_xml))
        return self.correct


def judged(rig, reading, value, name="n"):
    return asyncio.run(judge_network(rig, name, reading, value))[0]


def read(listing, xml=(0, XML), name="n"):
    proc = FakeProc(listing, xml)
    return asyncio.run(NetworksLook(proc).read(name)), proc


@pytest.mark.parametrize("name,row,correct,holds", [
    ("not defined", None, (True, ""), "absent"),
    ("defined, active, autostart, correct", UP, (True, "ok"), "up"),
    ("not active", ("n", "inactive", "yes", "yes"), (True, "ok"), None),
    ("no autostart", ("n", "active", "no", "yes"), (True, "ok"), None),
    ("transient", ("n", "active", "no", "no"), (True, "ok"), None),
    ("wrong parameters (a breached isolation)", UP, (False, "<ip>"), None),
])
def test_at_most_one_value_holds(name, row, correct, holds):
    reading, _ = read((0, table(*([row] if row else []), ("other", "active", "yes", "yes"))))
    assert [v for v in NET_VALUES if judged(FakeRig(correct), reading, v) == HOLDS] == ([holds] if holds else []), name


def test_one_look_reads_the_list_once_and_each_definition_at_most_once():
    proc = FakeProc((0, table(UP, ("m", "active", "yes", "yes"), ("x", "inactive", "yes", "yes"))))
    look, rig = NetworksLook(proc), FakeRig()

    async def judge_all():
        for name in ("n", "m", "x", "gone"):
            for value in NET_VALUES:
                await judge_network(rig, name, await look.read(name), value)
    asyncio.run(judge_all())
    asked = [c[0][1] for c in proc.calls]
    # 4 networks x 2 values = 8 judgements: one list, and a definition only for n and m (x is not running)
    assert asked.count("net-list") == 1 and sorted(c[0][2] for c in proc.calls if c[0][1] == "net-dumpxml") == ["m", "n"]


def test_the_rig_is_asked_with_the_live_definition_only_once_the_network_runs():
    rig = FakeRig()
    reading, _ = read((0, table(UP)))
    asyncio.run(judge_network(rig, "n", reading, "up"))
    assert rig.asked == [("n", XML)]
    rig = FakeRig()
    reading, proc = read((0, table(("n", "inactive", "yes", "yes"))))
    asyncio.run(judge_network(rig, "n", reading, "up"))
    assert rig.asked == [] and [c[0][1] for c in proc.calls] == ["net-list"]


@pytest.mark.parametrize("listing,xml", [
    ((1, "error: failed to connect to the hypervisor\nerror: access denied by policy\n"), (0, XML)),
    ((0, "Navn  Tilstand  Autostart  Vedvarende\n---\n n  aktiv  ja  ja\n"), (0, XML)),   # a translated table
    ((0, HEAD + " n   active   maybe   yes\n"), (0, XML)),                               # a row it cannot read
    ((0, table(UP)), (1, "error: x\n")),                                                 # the definition unreadable
])
def test_virsh_that_does_not_answer_cannot_tell(listing, xml):
    reading, _ = read(listing, xml)
    assert all(judged(FakeRig(), reading, v) == CANNOT_TELL for v in NET_VALUES), reading


def test_a_name_with_spaces_is_read_from_the_right():
    reading, _ = read((0, table(("my net", "active", "yes", "yes"))), name="my net")
    assert reading["defined"] and reading["Active"] and reading["xml"] == XML


def test_the_rig_that_cannot_tell_makes_up_cannot_tell():
    reading, _ = read((0, table(UP)))
    assert judged(FakeRig((None, "?")), reading, "up") == CANNOT_TELL


def test_every_virsh_call_runs_untranslated():
    _, proc = read((0, table(UP)))
    assert [c[0][:2] for c in proc.calls] == [["virsh", "net-list"], ["virsh", "net-dumpxml"]]
    assert all(env == {"LC_ALL": "C"} for _, env in proc.calls)


def test_a_value_that_is_not_a_network_value_does_not_hold():
    reading, _ = read((0, table()))
    assert judged(FakeRig(), reading, "running") == DOES_NOT_HOLD


def test_the_kind_is_exported_where_discovery_looks_and_the_fixture_is_look_scoped():
    mark = getattr(kinds.libvirt_net_validator, MARK)
    assert mark.name == "libvirt_net" and kinds.libvirt_net_validator is networks.libvirt_net_validator
    assert getattr(networks.libvirt_nets, MARK).scope == "look"


def test_a_rig_that_does_not_say_what_a_correct_network_is_cannot_tell(rig_root):  # noqa: F811
    rig = Rig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    ok, why = asyncio.run(rig.network_correct("n", XML))
    assert ok is None and "network_correct" in why
