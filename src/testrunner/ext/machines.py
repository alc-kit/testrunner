"""The machine-provider seam: what ext.rig needs from whatever creates a rig's machines.

    status()                     {machine: state}; a machine that does not exist is NOT_CREATED
                                 (or absent from the mapping)
    up(machines, **kw)           create / start these machines (never an empty list: refused)
    destroy(machines=None, **kw) remove these machines; None = every machine

ext.vagrant's `Vagrant` is the default implementation, and a rig built on the Vagrant layer
(ext/rig/vagrant/rig.rb) uses it unchanged. A rig that uses another framework — or none —
returns its own provider from its Rig's `machines()` (WP testrunner §6c.6, mode d); ext.rig's
guards, its observer and the `machines:` walk kind then use it, and vagrant is never run.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

NOT_CREATED = "not_created"


@runtime_checkable
class MachineProvider(Protocol):
    async def status(self) -> dict[str, str]: ...

    async def up(self, machines: list[str], **kw: Any) -> Any: ...

    async def destroy(self, machines: list[str] | None = None, **kw: Any) -> Any: ...
