"""The rig extension's settings: the `ext: {rig: {...}}` part of the subscriber's runner
config. The library (testrunner.ext.rig) and the three tools' command lines read the same
part, so a Vagrantfile calling `bin/rig-nodes` and a stage calling the library cannot
disagree about the rig."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import ConfigError, find_runner_config, load_runner_config

KEYS = {"prefix", "cluster_kind", "cluster_noun", "configs", "catalogue", "stale_files", "inventory", "name"}
INVENTORY_KEYS = {"group", "conditional", "v6_var"}


@dataclass
class RigSettings:
    root: Path
    name: str = "rig"                  # for messages and the harness key's comment
    prefix: str = "RIG"                # the rig's environment variables: <PREFIX>_*
    cluster_kind: str = "node"         # the catalogue `kind:` whose machines form the cluster
    cluster_noun: str = "cluster"      # what a cluster node is called in messages ("a <noun> node")
    configs: str = "configs"           # run configs; the schema is <configs>/schema.yml
    catalogue: str = "lifecycle/nodes.yml"
    stale_files: tuple[str, ...] = ()  # per-node files under host_vars/<node>/, stale once it is re-created
    # optional, during a move to the catalogue: an inventory section inside it that must AGREE
    # with machines (group: <key>, conditional: <key>, v6_var: <host var holding the IPv6>)
    inventory: dict[str, str] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def configs_dir(self) -> Path:
        return self.root / self.configs

    @property
    def schema_file(self) -> Path:
        return self.configs_dir / "schema.yml"

    @property
    def catalogue_file(self) -> Path:
        return self.root / self.catalogue

    @property
    def kinds(self) -> tuple[str, str]:
        return (self.cluster_kind, "support")


def settings_from(root: Path | str, spec: dict | None, where: str = "ext.rig") -> RigSettings:
    spec = dict(spec or {})
    unknown = sorted(set(spec) - KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(KEYS))})")
    inv = spec.get("inventory")
    if inv is not None and (not isinstance(inv, dict) or set(inv) - INVENTORY_KEYS or "group" not in inv):
        raise ConfigError(f"{where}.inventory: want {{group: <key>, conditional: <key>, v6_var: <var>}}")
    kind = spec.get("cluster_kind", "node")
    if not (isinstance(kind, str) and kind.isidentifier()) or kind == "support":
        raise ConfigError(f"{where}.cluster_kind: want an identifier other than `support` (got {kind!r})")
    stale = spec.get("stale_files") or []
    if not isinstance(stale, list) or not all(isinstance(s, str) for s in stale):
        raise ConfigError(f"{where}.stale_files: want a list of paths under host_vars/<node>/")
    return RigSettings(root=Path(root).resolve(), name=str(spec.get("name", "rig")),
                       prefix=str(spec.get("prefix", "RIG")), cluster_kind=kind,
                       cluster_noun=str(spec.get("cluster_noun", "cluster")),
                       configs=str(spec.get("configs", "configs")),
                       catalogue=str(spec.get("catalogue", "lifecycle/nodes.yml")),
                       stale_files=tuple(stale), inventory=inv, raw=spec)


def load_settings(root: Path | str | None = None) -> RigSettings:
    """The settings of the rig whose runner config is at (or above) `root` (default: cwd)."""
    rc = load_runner_config(find_runner_config(Path(root) if root else Path.cwd()))
    return settings_from(rc.root, rc.ext.get("rig"), f"{rc.file}: ext.rig")
