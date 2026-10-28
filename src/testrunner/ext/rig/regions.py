"""The generic regions of a rig (WP testrunner-state-validation §10 B5a, D9; user 2026-10-28: "VM's can
live there as that is a more generalized element, and partly the support probe can have some
generalized parts living in testrunner as well"). Whatever runs ON the machines — Proxmox VE, say —
is a system under test, and its probes live with it (alc-kit/proxmox-testrunner-ext), not here.

Two validators and the one reading they share (a `look` fixture: one reading per act of judging):

    vms      the rig's cluster machines, judged against the EXPECTED nodes — the declared data
             `expected` when the runner config declares it, else the scenario's non-late cluster
             nodes from the catalogue:
               absent   no domain of a cluster machine exists
               running  every expected machine runs AND answers TCP/22 (never ping: a PVE node's
                        firewall drops ICMP from the management network, measured 2026-10-28)
               halted   every expected machine exists, and none runs
               partial  anything else (some exist, some run, or one runs but does not answer)
    support  the scenario's other machines that the Vagrantfile defines (a cache, a Tang server,
             a quorum host — whatever the catalogue lists that is not a cluster node):
               up       every one runs and answers TCP/22
               absent   none exists
             anything between holds neither value (a derived observer answers NONE_HOLDS).

Declare them as `validate: {python: vms}` / `{python: support}` on a variable whose values are
exactly those names, and `observe: {order: [...]}` to read the variable from them.

Not here (rig-specific, so the rig adds it): leftover networks and volumes after a destroy that
failed half-way (§8.6 case 4) — testrunner does not know a rig's network names.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...api import fixture, validator
from ...proc import Proc
from ..machines import NOT_CREATED

log = logging.getLogger(__name__)

TCP_TIMEOUT = 3.0
VMS_VALUES = ("absent", "running", "halted", "partial")
SUPPORT_VALUES = ("absent", "up")


async def answers(ip: str, port: int = 22, timeout: float = TCP_TIMEOUT) -> bool:
    """Does `ip:port` accept a TCP connection? (Not ping: see the module docstring.)"""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def read_machines(rig: Any, expected: list[str] | None) -> dict:
    """One reading of the rig's machines: what vagrant (the rig's provider) says of each, which
    domains of the rig are left, and which running machines answer TCP/22."""
    rig.ensure_bootstrap()                  # the Vagrantfile refuses to load without the harness key
    proc = Proc(rig.root, rig.logs)
    states = await rig.machines(proc).status()
    cat = rig.catalogue
    cluster = list(expected) if expected is not None else cat.cluster_nodes()
    support = [n for n in cat.members() if n not in cat.cluster() and n in states]
    machines = {n: states.get(n, NOT_CREATED) for n in {*cat.catalogue_cluster(), *cluster, *support}}
    running = [n for n, st in machines.items() if st == "running"]
    up = dict(zip(running, await asyncio.gather(*(answers(str(cat.machines[n]["ip"])) for n in running
                                                   if n in cat.machines))))
    reading = {"expected": cluster, "support": support, "states": machines, "answers": up,
               "domains": await rig.domains_left(proc),
               "cluster_machines": cat.catalogue_cluster()}
    log.info("rig machines: expected %s %s; support %s %s; domains left %s", cluster,
             {n: machines[n] for n in cluster}, support, {n: machines[n] for n in support}, reading["domains"])
    return reading


def judge_vms(reading: dict, value: str) -> tuple[bool, str]:
    exp = reading["expected"]
    st = reading["states"]
    ans = reading["answers"]
    seen = ", ".join(f"{n}={st[n]}{'' if st[n] != 'running' else (' answers' if ans.get(n) else ' NOT answering')}"
                     for n in exp) or "no expected machine"
    if value == "absent":
        cluster = set(reading["cluster_machines"])
        left = [d for d in reading["domains"] if any(d == c or d.endswith(f"_{c}") for c in cluster)]
        exist = [n for n in reading["cluster_machines"] if st.get(n, NOT_CREATED) != NOT_CREATED]
        return (not left and not exist), (f"cluster domains left: {left or exist}" if left or exist
                                          else "no cluster machine exists")
    if not exp:
        return False, "no expected machine"
    run = [n for n in exp if st[n] == "running" and ans.get(n)]
    created = [n for n in exp if st[n] != NOT_CREATED]
    if value == "running":
        return len(run) == len(exp), seen
    if value == "halted":
        return len(created) == len(exp) and not any(st[n] == "running" for n in exp), seen
    if value == "partial":
        absent_ok, _ = judge_vms(reading, "absent")
        whole = len(run) == len(exp) or (len(created) == len(exp) and not any(st[n] == "running" for n in exp))
        return not absent_ok and not whole, seen
    return False, f"{value!r} is not a vms value ({', '.join(VMS_VALUES)})"


def judge_support(reading: dict, value: str) -> tuple[bool, str]:
    sup = reading["support"]
    st = reading["states"]
    ans = reading["answers"]
    seen = ", ".join(f"{n}={st[n]}" for n in sup) or "the scenario has no support machine"
    if value == "up":
        return all(st[n] == "running" and ans.get(n) for n in sup), seen
    if value == "absent":
        return all(st[n] == NOT_CREATED for n in sup), seen
    return False, f"{value!r} is not a support value ({', '.join(SUPPORT_VALUES)})"


@fixture(scope="look")
async def machines_reading(rig, data):
    """The rig's machines, read ONCE per look (vagrant, the domains left, TCP/22)."""
    return await read_machines(rig, data.get("expected") if isinstance(data, dict) else None)


@validator("vms")
def vms(claim, machines_reading):
    """vms=<value>, judged on this look's reading (module docstring)."""
    return judge_vms(machines_reading, claim.value)


@validator("support")
def support(claim, machines_reading):
    """support=<value>, judged on this look's reading (module docstring)."""
    return judge_support(machines_reading, claim.value)
