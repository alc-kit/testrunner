"""A rig's libvirt networks as regions (WP testrunner-state-validation §10 B6c; user 2026-10-28:
"building the networking setup is a prerequisite for actually making the vagrant boxes").

One validator KIND, one region per network, and each region is binary:

    validate: {libvirt_net: <network name>}     on a variable whose values are [absent, up]

    absent  the network is not defined
    up      the network is defined, persistent, active and autostart, AND it has the parameters
            the rig says are correct for it (Rig.network_correct: e.g. "no host address, no
            forward" for an isolated LAN)

A network that exists with the wrong parameters, or that is defined but not active, holds
NEITHER value: a derived observer then reads "no value holds", and a step that claimed `up`
fails. That is deliberate (undefined = failure): a broken isolation is not a state to continue
from. Whether a system can live with only some of its networks is decided by each step's
`requires` (the matrix), never by a `partial` value.

The network is named in the runner config, not derived from the variable's name. Every
`virsh` call runs with LC_ALL=C (virsh translates its labels; measured on a Danish host).

**One reading per look** (user 2026-10-28): the networks are read once per look, by the
`libvirt_nets` fixture — one `virsh net-list --all` for all of them, and each live definition at
most once. A rig opts in by importing the fixture in one of its modules:

    from testrunner.ext.rig.networks import libvirt_nets  # noqa: F401
"""
from __future__ import annotations

import logging
from typing import Any

from ...api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS, fixture, validator_kind
from ...proc import Proc

log = logging.getLogger(__name__)

NET_VALUES = ("absent", "up")
FLAGS = ("Persistent", "Active", "Autostart")
LISTED = {"active": True, "inactive": False, "yes": True, "no": False}


class NetworksLook:
    """ONE look at the host's libvirt networks, shared by every network and value judged in it:
    one `virsh net-list --all` (every network's state, autostart, persistence), and each network's
    live definition (`virsh net-dumpxml`) read at most once, and only once the network is
    defined, persistent, active and autostart. Each virsh call is a libvirt connection — and,
    under polkit, an authorization check (measured 2026-10-28: polkitd ran out of descriptors
    after hours of looks) — so a look costs 1 + (networks asked about) calls, never 2 per value."""

    def __init__(self, proc: Proc) -> None:
        self.proc = proc
        self._table: dict[str, dict[str, bool]] | str | None = None
        self._xml: dict[str, str | None] = {}

    async def table(self) -> dict[str, dict[str, bool]] | str:
        """{name: {Active, Autostart, Persistent}}, or why virsh gave no readable answer."""
        if self._table is None:
            self._table = await self._list()
        return self._table

    async def _list(self) -> dict[str, dict[str, bool]] | str:
        r = await self.proc.run(["virsh", "net-list", "--all"], env={"LC_ALL": "C"}, echo=False)
        if r.returncode != 0:
            return f"virsh net-list --all failed (exit {r.returncode}): {r.text.strip()[-200:]}"
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        if not lines or lines[0].split() != ["Name", "State", "Autostart", "Persistent"]:
            return f"virsh net-list --all printed no table it can read: {r.text.strip()[:200]!r}"
        table = {}
        for ln in lines[1:]:
            if set(ln) == {"-"}:
                continue
            cells = ln.rsplit(None, 3)          # from the right: a name may hold spaces
            if len(cells) != 4 or not {cells[1], cells[2], cells[3]} <= set(LISTED):
                return f"virsh net-list --all printed a row it cannot read: {ln!r}"
            table[cells[0]] = {"Active": LISTED[cells[1]], "Autostart": LISTED[cells[2]],
                               "Persistent": LISTED[cells[3]]}
        return table

    async def read(self, name: str) -> dict:
        """One network: {defined: False}, or {defined: True, <flag>: bool, xml?: str}, or {error: why}."""
        table = await self.table()
        if isinstance(table, str):
            return {"error": table}
        if name not in table:
            return {"defined": False}
        reading: dict[str, Any] = {"defined": True, **table[name]}
        if all(reading[f] for f in FLAGS):
            if name not in self._xml:
                x = await self.proc.run(["virsh", "net-dumpxml", name], env={"LC_ALL": "C"}, echo=False)
                self._xml[name] = x.text if x.returncode == 0 else None
            if self._xml[name] is None:
                return {"error": f"virsh net-dumpxml {name} failed"}
            reading["xml"] = self._xml[name]
        return reading


@fixture(scope="look")
def libvirt_nets(rig):
    """The rig's libvirt networks, read once per look (NetworksLook). A rig whose runner config
    uses `validate: {libvirt_net: ...}` imports this in one of its modules (an extension's
    fixtures are the subscriber's call): `from testrunner.ext.rig.networks import libvirt_nets`."""
    return NetworksLook(Proc(rig.root, rig.logs))


async def judge_network(rig: Any, name: str, reading: dict, value: str) -> tuple[str, str]:
    """libvirt_net=<value> on this reading: (HOLDS | DOES_NOT_HOLD | CANNOT_TELL, why)."""
    if value not in NET_VALUES:
        return DOES_NOT_HOLD, f"{value!r} is not a libvirt_net value ({', '.join(NET_VALUES)})"
    if "error" in reading:
        return CANNOT_TELL, reading["error"]
    if value == "absent":
        return (DOES_NOT_HOLD, f"{name} is defined") if reading["defined"] else (HOLDS, f"{name} is not defined")
    if not reading["defined"]:
        return DOES_NOT_HOLD, f"{name} is not defined"
    missing = [f.lower() for f in FLAGS if not reading[f]]
    if missing:
        return DOES_NOT_HOLD, f"{name} is defined but not {', '.join(missing)}"
    ok, why = await rig.network_correct(name, reading["xml"])
    if ok is None:
        return CANNOT_TELL, why
    return (HOLDS if ok else DOES_NOT_HOLD), f"{name}: {why}"


@validator_kind("libvirt_net")
async def libvirt_net_validator(spec, claim, rig, libvirt_nets):
    """`validate: {libvirt_net: <name>}` — a network of the rig, absent or up (module docstring)."""
    name = str(spec["libvirt_net"])
    reading = await libvirt_nets.read(name)
    log.info("libvirt network %s: %s", name, {k: v for k, v in reading.items() if k != "xml"})
    return await judge_network(rig, name, reading, claim.value)
