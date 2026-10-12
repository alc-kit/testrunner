"""testrunner.ext.rig: a vagrant + ansible test rig, for testrunner actions.

What every such rig is built from, whatever it tests: the run config and the environment it
becomes (rig_config: the schema engine), the node catalogue (rig_nodes), the generated
ansible.cfg and harness key, scope guards (only nodes that EXIST, never an empty scope), the
rig observer, stale per-node state, drive rules for the playbooks' prompts, the serial
console capture and the leftover-domain sweep.

A rig describes itself in its runner config, not in code:

    ext:
      rig:
        name: myrig                  # messages, the harness key's comment
        prefix: MYRIG                # its environment variables: MYRIG_*
        cluster_kind: node           # the catalogue `kind:` whose machines form the cluster
        cluster_noun: cluster        # "a <noun> node" in messages
        configs: configs             # run configs; the schema is configs/schema.yml
        catalogue: lifecycle/nodes.yml
        stale_files: [secrets/recovery_key.txt]   # under host_vars/<node>/, purged on re-create
        inventory: {group: cluster_nodes, conditional: conditional_hosts, v6_var: host_v6}  # optional

and subclasses `Rig` only for what it does differently (where its automation lives, the
networks it needs, ...). The three tools also run from the command line, for a Vagrantfile
or a shell: `python -m testrunner.ext.rig.{config,nodes,drive_rules} --root <rig> ...`
(drive_rules takes no --root).

Conventions it relies on: the rig state variable is `rig`, with `absent` (no cluster node
exists) and `up`; the inventory lives in inventory/, secrets in secrets/, logs in logs/.
"""
__all__ = ["Rig", "Catalogue", "RigSettings", "banner", "die", "load_settings", "render_rules", "settings_from"]
_WHERE = {"Rig": "core", "Catalogue": "core", "banner": "core", "die": "core", "render_rules": "core",
          "RigSettings": "settings", "load_settings": "settings", "settings_from": "settings"}


def __getattr__(name):
    # imported on first use, never by the package itself: `python -m testrunner.ext.rig.nodes`
    # must not find its module already imported (runpy then warns on stderr, and a caller that
    # reads the tool's output with stderr mixed in, as the Vagrantfile does, gets the warning)
    if name in _WHERE:
        import importlib
        return getattr(importlib.import_module(f".{_WHERE[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
