"""Template resolvers a rig's walk actions use (enabled by `ext: {rig: ...}`).

    limit: "{live}"                         the rig's cluster nodes that exist (never empty)
    victim: "{role:reprovision_victim}"     the catalogue's one node in a role
    nodes: "{roles:convert_victims}"        every node in a role (a list)
    founder: "{rig:founder}"                an attribute of the subscriber's `rig` fixture
                                            (a method is called, with the step's params if it takes them)
    cluster: "{inventory:{param.founder}/cluster_name}"   a host variable, as ansible resolves it
    peer: "{peer_of:{param.node}}"          a live node that is not the one named
    mode: "{config:GUEST_EGRESS|none}"      a rig variable (PREFIX_NAME) as this step resolves it;
                                            unset without a `|default` is refused, by name

and one inner-step kind:

    - {require_nodes: ["{param.victim}"]}   refuse unless every named node EXISTS in the rig
                                            (a stage hard-wired to a node; ansible would skip a
                                            missing one with exit 0)

They take the subscriber's `rig`, `vg` and `ans` fixtures by name, as actions do. Every
value is logged (the runner logs each resolution), so a run that targeted the wrong node says
which resolver answered what.
"""
from __future__ import annotations

import inspect
import logging

from ...api import ERRORED, FAILED, OutcomeError, resolver, step_kind

log = logging.getLogger(__name__)


@resolver("live")
async def live(rig, vg):
    return await rig.live_limit(vg)


@resolver("role")
def role(arg, rig):
    if not arg:
        raise OutcomeError(ERRORED, "{role:<name>}: name the role")
    return rig.catalogue.role1(arg)


@resolver("roles")
def roles(arg, rig):
    if not arg:
        raise OutcomeError(ERRORED, "{roles:<name>}: name the role")
    out = rig.catalogue.role(arg)
    if not out:
        raise OutcomeError(FAILED, f"the catalogue has no node in role {arg!r}")
    return out


@resolver("rig")
async def rig_attr(arg, rig, params):
    if not arg or arg.startswith("_") or not hasattr(rig, arg):
        raise OutcomeError(ERRORED, f"{{rig:{arg}}}: the rig has no public attribute {arg!r}")
    value = getattr(rig, arg)
    if callable(value):
        takes = inspect.signature(value).parameters
        value = value(params) if "params" in takes else value()
        if inspect.isawaitable(value):
            value = await value
    log.debug("{rig:%s} -> %r", arg, value)
    return value


@resolver("inventory")
async def inventory(arg, ans):
    host, sep, expr = arg.partition("/")
    if not sep or not host or not expr:
        raise OutcomeError(ERRORED, f"{{inventory:{arg}}}: want {{inventory:<host>/<expression>}}")
    value = await ans.hostvar(host, expr)
    if value == "":
        raise OutcomeError(FAILED, f"the inventory resolves {expr!r} for {host} to nothing")
    return value


@resolver("peer_of")
async def peer_of(arg, rig, vg):
    peers = [n for n in await rig.live_nodes(vg) if n != arg]
    log.debug("{peer_of:%s}: live peers %s", arg, peers)
    if not peers:
        raise OutcomeError(FAILED, f"no live node besides {arg}")
    return peers[0]


@resolver("config")
def config(arg, rig, params):
    name, sep, default = arg.partition("|")
    if not name:
        raise OutcomeError(ERRORED, "{config:<NAME>[|default]}: name the variable")
    value = rig.e(name, params)
    if value is None:
        if sep:
            log.debug("{config:%s}: %s_%s unset -> the default %r", arg, rig.PREFIX, name, default)
            return default
        raise OutcomeError(FAILED, f"the rig variable {rig.PREFIX}_{name} is unset, and this step needs it "
                                   f"(set it in the run config, or give a default: {{config:{name}|<value>}})")
    return value


@step_kind("require_nodes", readonly=True)
async def require_nodes(spec, rig, vg, params, step):
    nodes = spec["require_nodes"]
    nodes = [nodes] if isinstance(nodes, str) else [str(n) for n in nodes or []]
    if not nodes:
        raise OutcomeError(ERRORED, "require_nodes: name at least one node")
    stage = step.parent or step.id
    log.info("%s needs %s to exist", stage, nodes)
    await rig.require_nodes(vg, stage, *nodes, subset=rig.e("NODES", params))
