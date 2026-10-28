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
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ...api import CANNOT_TELL, DOES_NOT_HOLD, HOLDS, validator_kind
from ...proc import Proc

log = logging.getLogger(__name__)

NET_VALUES = ("absent", "up")
NOT_FOUND = "no network with matching name"
FLAGS = ("Persistent", "Active", "Autostart")


async def read_network(proc: Proc, name: str) -> dict:
    """One reading of a network: {defined: False}, or {defined: True, <flag>: bool, xml: str}, or
    {error: why} when virsh did not answer (cannot tell)."""
    info = await proc.run(["virsh", "net-info", name], env={"LC_ALL": "C"}, echo=False)
    if info.returncode != 0:
        if NOT_FOUND in info.text:
            return {"defined": False}
        return {"error": f"virsh net-info {name} failed (exit {info.returncode}): {info.text.strip()[-200:]}"}
    reading: dict[str, Any] = {"defined": True}
    for flag in FLAGS:
        m = re.search(rf"^{flag}:\s+(\S+)", info.text, re.M)
        if not m or m.group(1) not in ("yes", "no"):
            return {"error": f"virsh net-info {name} says nothing readable about {flag!r}"}
        reading[flag] = m.group(1) == "yes"
    xml = await proc.run(["virsh", "net-dumpxml", name], env={"LC_ALL": "C"}, echo=False)
    if xml.returncode != 0:
        return {"error": f"virsh net-dumpxml {name} failed (exit {xml.returncode})"}
    reading["xml"] = xml.text
    return reading


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
async def libvirt_net_validator(spec, claim, rig):
    """`validate: {libvirt_net: <name>}` — a network of the rig, absent or up (module docstring)."""
    name = str(spec["libvirt_net"])
    reading = await read_network(Proc(rig.root, rig.logs), name)
    log.info("libvirt network %s: %s", name, {k: v for k, v in reading.items() if k != "xml"})
    return await judge_network(rig, name, reading, claim.value)
