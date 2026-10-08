"""Vagrant from an action: what exists, bring machines up, tear them down.

    vg = Vagrant(proc, cwd=root)
    states = await vg.status()          # {"web-1": "running", "web-2": "not_created", ...}
    await vg.up(["web-1", "web-2"], provider="libvirt", parallel=False)

`status()` parses `vagrant status --machine-readable`, so a machine name with spaces in
its human description, or a localised state text, cannot confuse it. A machine whose
state is `not_created` does not exist; anything else does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..api import FAILED, OutcomeError
from ..proc import Proc, Result

NOT_CREATED = "not_created"


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
        return parse_machine_readable(r.text)

    async def existing(self, among: Iterable[str] | None = None) -> list[str]:
        """Machines that exist (any state but not_created), in `among`'s order if given."""
        states = await self.status()
        names = list(among) if among is not None else list(states)
        return [n for n in names if states.get(n, NOT_CREATED) != NOT_CREATED]

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
        r = await self._run(*args, *machines, log=log)
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"vagrant up {' '.join(machines)}: exit {r.returncode}")
        return r

    async def destroy(self, machines: list[str] | None = None, *, log: str | None = None) -> Result:
        r = await self._run("destroy", "-f", *(machines or []), log=log)
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"vagrant destroy: exit {r.returncode}")
        return r

    async def run(self, *args: str, log: str | None = None, echo: bool = True) -> Result:
        return await self._run(*args, log=log, echo=echo)
