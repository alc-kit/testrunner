"""Vagrant from an action: what exists, bring machines up, tear them down.

    vg = Vagrant(proc, cwd=root)
    states = await vg.status()          # {"web-1": "running", "web-2": "not_created", ...}
    await vg.up(["web-1", "web-2"], provider="libvirt", parallel=False)

`status()` parses `vagrant status --machine-readable`, so a machine name with spaces in
its human description, or a localised state text, cannot confuse it. A machine whose
state is `not_created` does not exist; anything else does.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from ..api import FAILED, OutcomeError, step_kind
from ..proc import Proc, Result

logger = logging.getLogger(__name__)   # not `log`: methods take a log= file name

from .machines import NOT_CREATED  # noqa: E402,F401  (one definition, the provider seam)


def parse_machine_readable(text: str) -> dict[str, str]:
    """`timestamp,target,type,data...` lines -> {machine: state}."""
    out: dict[str, str] = {}
    for line in text.replace("\r", "").splitlines():
        parts = line.split(",")
        if len(parts) >= 4 and parts[2] == "state" and parts[1]:
            out[parts[1]] = parts[3]
    return out


class Vagrant:
    def __init__(self, proc: Proc, *, cwd: Path, env: dict[str, str] | None = None):
        self.proc, self.cwd, self.env = proc, Path(cwd), dict(env or {})

    async def _run(self, *args: str, log: str | None = None, echo: bool = True,
                   timeout: float | None = None) -> Result:
        return await self.proc.run(["vagrant", *args], cwd=self.cwd, env=self.env, log=log,
                                   echo=echo, timeout=timeout)

    async def status(self) -> dict[str, str]:
        r = await self._run("status", "--machine-readable", echo=False)
        if r.returncode != 0:
            raise OutcomeError(FAILED, "`vagrant status` failed — cannot tell which machines exist")
        states = parse_machine_readable(r.text)
        logger.info("vagrant status: %s", states or "NO machines (is this the Vagrantfile's directory?)")
        return states

    async def existing(self, among: Iterable[str] | None = None) -> list[str]:
        """Machines that exist (any state but not_created), in `among`'s order if given."""
        states = await self.status()
        names = list(among) if among is not None else list(states)
        out = [n for n in names if states.get(n, NOT_CREATED) != NOT_CREATED]
        unknown = [n for n in names if n not in states]
        if unknown:
            logger.warning("vagrant does not know %s (counted as not existing)", unknown)
        logger.debug("existing among %s: %s", names, out)
        return out

    async def up(self, machines: list[str], *, provider: str | None = None, parallel: bool = True,
                 log: str | None = None) -> Result:
        if not machines:
            # `vagrant up` with no names brings up EVERY machine — never what was meant
            raise OutcomeError(FAILED, "vagrant up: no machine named")
        args = ["up"]
        if not parallel:
            args.append("--no-parallel")
        if provider:
            args.append(f"--provider={provider}")
        logger.info("vagrant up %s (provider %s, %s)", machines, provider or "default",
                    "parallel" if parallel else "one at a time")
        r = await self._run(*args, *machines, log=log)
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"vagrant up {' '.join(machines)}: exit {r.returncode}")
        return r

    async def destroy(self, machines: list[str] | None = None, *, log: str | None = None) -> Result:
        logger.info("vagrant destroy %s", machines or "ALL machines")
        r = await self._run("destroy", "-f", *(machines or []), log=log)
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"vagrant destroy: exit {r.returncode}")
        return r

    async def run(self, *args: str, log: str | None = None, echo: bool = True) -> Result:
        return await self._run(*args, log=log, echo=echo)


def vagrant_describe(spec):
    """--dry-run: the vagrant command vagrant_step would run."""
    verb, machines = spec["vagrant"], spec.get("machines")
    names = [machines] if isinstance(machines, str) and machines != "all" else machines
    if verb == "up":
        flags = ([] if spec.get("parallel", True) else ["--no-parallel"]) + (
            [f"--provider={spec['provider']}"] if spec.get("provider") else [])
        return [" ".join(["vagrant", "up", *flags, *(names or ["<no machine named: refused>"])])]
    if verb == "destroy":
        return [" ".join(["vagrant", "destroy", "-f", *([] if names == "all" else (names or ["<name the machines>"]))])]
    return [f"vagrant {verb}: not up or destroy (refused when run)"]


@step_kind("vagrant", keys=("machines", "provider", "parallel", "log"), describe=vagrant_describe)
async def vagrant_step(spec, vg):
    """A walk's `{vagrant: up|destroy, machines: [...], provider, parallel, log}` through the
    subscriber's `vg` fixture. `up` refuses an empty machine list (vagrant would bring up EVERY
    machine); `destroy` with no machines destroys all — say so explicitly with `machines: all`."""
    verb, machines = spec["vagrant"], spec.get("machines")
    if isinstance(machines, str) and machines != "all":
        machines = [machines]
    logger.info("vagrant step: %s %s", verb, machines)
    if verb == "up":
        return await vg.up(list(machines or []), provider=spec.get("provider"),
                           parallel=bool(spec.get("parallel", True)), log=spec.get("log"))
    if verb == "destroy":
        if not machines:
            raise OutcomeError(FAILED, "vagrant destroy: name the machines, or machines: all")
        return await vg.destroy(None if machines == "all" else list(machines), log=spec.get("log"))
    raise OutcomeError(FAILED, f"vagrant step: {verb!r} is not up or destroy")
