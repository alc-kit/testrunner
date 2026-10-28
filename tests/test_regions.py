"""The generic regions, vms and support (WP testrunner-state-validation §10 B5a). Each value both ways;
the expected nodes come from the declared data when there is any (D9: 'up' after a late node was
added and the founder removed), else the catalogue."""
import asyncio

import pytest
import yaml

from test_ext_rig import DEFAULT, FakeProvider, no_vagrant, rig_root  # noqa: F401  (rig_root is a fixture)
from testrunner.ext.rig import Rig
from testrunner.ext.rig import regions
from testrunner.ext.rig.regions import judge_support, judge_vms, read_machines


def reading(states, expected=("n1", "n2", "n3"), support=("cache", "hub"), answers=None, domains=()):
    answers = {n: True for n, s in states.items() if s == "running"} if answers is None else answers
    return {"expected": list(expected), "support": list(support), "states": dict(states), "answers": answers,
            "domains": list(domains), "cluster_machines": ["n1", "n2", "n3", "n4"]}


ALL = {"n1": "running", "n2": "running", "n3": "running", "n4": "not_created", "cache": "running", "hub": "running"}


@pytest.mark.parametrize("states,answers,domains,holds", [
    ({**ALL}, None, ["myrig_n1", "myrig_n2", "myrig_n3"], "running"),
    ({**ALL, "n1": "shutoff", "n2": "shutoff", "n3": "shutoff"}, {}, ["myrig_n1", "myrig_n2", "myrig_n3"], "halted"),
    ({**ALL, "n3": "shutoff"}, None, ["myrig_n1", "myrig_n2", "myrig_n3"], "partial"),
    ({**ALL, "n3": "not_created"}, None, ["myrig_n1", "myrig_n2"], "partial"),
    ({**ALL}, {"n1": True, "n2": True, "n3": False}, ["myrig_n1", "myrig_n2", "myrig_n3"], "partial"),   # no network
    ({n: "not_created" for n in ALL}, {}, [], "absent"),
    ({n: "not_created" for n in ALL}, {}, ["myrig_n2"], "partial"),     # vagrant forgot it, the domain is left
])
def test_exactly_one_vms_value_holds(states, answers, domains, holds):
    r = reading(states, answers=answers, domains=domains)
    held = [v for v in regions.VMS_VALUES if judge_vms(r, v)[0]]
    assert held == [holds], (held, judge_vms(r, holds))


def test_running_is_judged_against_the_expected_data():
    # after addnode/rmnode: n1 removed (its VM still runs), n4 added
    states = {**ALL, "n4": "running"}
    assert judge_vms(reading(states, expected=["n2", "n3", "n4"]), "running")[0]
    states["n4"] = "not_created"                                    # control: the expected n4 is missing
    assert not judge_vms(reading(states, expected=["n2", "n3", "n4"]), "running")[0]


@pytest.mark.parametrize("states,answers,want", [
    ({**ALL}, None, {"up"}),
    ({**ALL, "cache": "not_created", "hub": "not_created"}, None, {"absent"}),
    ({**ALL, "cache": "shutoff", "hub": "shutoff"}, {}, {"halted"}),     # a rig's shutdown
    ({**ALL, "hub": "not_created"}, None, {"partial"}),
    ({**ALL, "hub": "shutoff"}, None, {"partial"}),
    ({**ALL}, {"cache": True, "hub": False}, {"partial"}),          # runs, does not answer
])
def test_support(states, answers, want):
    r = reading(states, answers=answers)
    assert {v for v in regions.SUPPORT_VALUES if judge_support(r, v)[0]} == want


def test_read_machines_reads_the_provider_once_and_never_pings(rig_root, tmp_path, monkeypatch):
    no_vagrant(tmp_path, monkeypatch)
    fake = FakeProvider({"cache": "running", "hub": "running", "n1": "running", "n2": "running",
                         "n3": "shutoff", "n4": "not_created", "arb": "not_created"})

    class OwnRig(Rig):
        def machines(self, proc, params=None):
            return fake

        async def domains_left(self, proc):
            return ["myrig_n1", "myrig_n2", "myrig_n3"]
    rig = OwnRig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")
    asked = []

    async def tcp(ip, port=22, timeout=3.0):
        asked.append((ip, port))
        return True
    monkeypatch.setattr(regions, "answers", tcp)                   # Rig.reachable's default
    r = asyncio.run(read_machines(rig, None))
    assert fake.calls == [("status",)]                               # one reading
    assert r["expected"] == ["n1", "n2", "n3"] and r["support"] == ["cache", "hub"]
    assert sorted(asked) == sorted([("192.0.2.9", 22), ("192.0.2.10", 22), ("192.0.2.51", 22), ("192.0.2.52", 22)])
    assert judge_vms(r, "partial")[0] and not judge_vms(r, "running")[0]
    assert asyncio.run(read_machines(rig, ["n1", "n2"]))["expected"] == ["n1", "n2"]   # the declared data wins


def test_answers_really_connects():
    # control for the TCP check itself: a port nobody listens on does not answer
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        yes = await regions.answers("127.0.0.1", port, timeout=1)
        server.close()
        await server.wait_closed()
        no = await regions.answers("127.0.0.1", port, timeout=1)
        return yes, no
    assert asyncio.run(go()) == (True, False)


def test_a_subscriber_that_imports_the_regions_has_them_registered(sub):
    # what the runner REALLY registers (the first version of this test only checked the marks on
    # the functions, and so could not see that ext.rig's kinds module never registers validators)
    from testrunner.config import load_runner_config
    from testrunner.discovery import collect
    sub.write("testrunner.yml", "modules: [m.py]\n")
    sub.write("m.py", "from testrunner.ext.rig.regions import machines_reading, support, vms  # noqa: F401\n")
    reg = collect(load_runner_config(sub.root / "testrunner.yml"))
    assert {"vms", "support"} <= set(reg.validators)
    assert reg.fixtures["machines_reading"].scope == "look"


def test_control_an_extension_alone_registers_no_region_validator(sub):
    # `ext: {rig: ...}` without the import: nothing — the subscriber opts in, explicitly
    from testrunner.config import load_runner_config
    from testrunner.discovery import collect
    sub.write("testrunner.yml", "modules: [m.py]\n")
    sub.write("m.py", "\n")
    reg = collect(load_runner_config(sub.root / "testrunner.yml"))
    assert "vms" not in reg.validators and "machines_reading" not in reg.fixtures


def test_a_rig_can_override_reachable(rig_root, tmp_path, monkeypatch):
    # the seam a sandbox of fakes needs (user 2026-10-28): the probe asks the RIG, not the socket
    no_vagrant(tmp_path, monkeypatch)
    fake = FakeProvider({"cache": "running", "hub": "running", "n1": "running", "n2": "running", "n3": "running",
                         "n4": "not_created", "arb": "not_created"})

    class OwnRig(Rig):
        def machines(self, proc, params=None):
            return fake

        async def domains_left(self, proc):
            return ["myrig_n1", "myrig_n2", "myrig_n3"]

        async def reachable(self, ip, port=22, timeout=3.0):
            return ip != "192.0.2.53"                                # n3 runs but does not answer
    rig = OwnRig(rig_root, yaml.safe_load(DEFAULT), rig_root / "configs" / "default.yml", "default")

    async def never(*a, **k):
        raise AssertionError("the probe used the socket, not the rig's reachable()")
    monkeypatch.setattr(regions, "answers", never)
    r = asyncio.run(read_machines(rig, None))
    assert judge_vms(r, "partial")[0] and not judge_vms(r, "running")[0]
