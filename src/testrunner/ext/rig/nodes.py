"""rig-nodes: the rig's node catalogue, read in ONE place.

Part of testrunner.ext.rig; a rig calls it as `python -m testrunner.ext.rig.nodes --root
<rig> ...` (its bin/rig-nodes is a wrapper), or imports it. Nothing here is specific to a
rig: the rig's facts live in its catalogue (ext.rig.catalogue, default lifecycle/nodes.yml),
and what its cluster nodes are is the setting ext.rig.cluster_kind (written <cluster> below).
This is the only thing that interprets the catalogue, so a Vagrantfile, the stages, the
config schema and the tests cannot each read it a little differently.

THE CATALOGUE says what COULD exist and what each box is for. It never says what exists
right now: that is `vagrant status` and the cluster's own membership. Nothing here may be
used to scope a stage.

  rig-nodes [--file F] validate                    check the catalogue; exit 1 naming every problem
  rig-nodes [--file F] list [--scenario S] [--kind <cluster>|support] [--include-late|--only-late]
                            [--subset "a b"] [--off FEATURE]...
                                                    machine names, in creation order; --off drops
                                                    the machines that are `optional:` on FEATURE
  rig-nodes [--file F] shell [--scenario S]        the catalogue as shell assignments, for ONE
                                                    `eval` at load instead of a call per value
  rig-nodes [--file F] get <machine> <field>       one field of one machine
  rig-nodes [--file F] role <role>                 the machine(s) a role names
  rig-nodes [--file F] scenarios                   the scenario names; the default first
  rig-nodes [--file F] json [--scenario S | --all] the machine table of a scenario (or of every
                                                    scenario), and the roles, as JSON

THE FORMAT (top-level keys of the catalogue; every other key is left alone):

  machines:            ordered mapping — the order is the order `vagrant up` creates them
    <name>:
      kind:      <cluster> | support
      ip:        IPv4 on the harness network (unique)
      ip6:       IPv6 on the harness network (optional, unique)
      mem, cpu:  MB, vCPUs
      disks:     data-disk count; size: GiB per data disk (0 iff disks is 0)
      late:      true — never created by `up` (the addnode stage creates it)
      optional:  <feature> — exists only while that rig feature is on (e.g. apt_cache)
      overrides: {mem: <rig.resources key>, ...} — a run config that sets that key
                 (non-zero) replaces the field; the value here is the default
      ...        any other key is carried through untouched (e.g. a rig's own flags)
  roles:               <role>: <machine> | [<machine>, ...]: cluster nodes only
  scenarios:           <name>: [<machine>, ...] — optional; absent = one scenario, all machines
  default_scenario:    <name> — required when scenarios is given

Optionally (ext.rig.inventory: {group, conditional, v6_var}), during a move to the
catalogue: an inventory section in it (<group>, <conditional>) still carries each node's
ansible_host (and <v6_var>); validate requires them to AGREE with machines, so the
duplication cannot drift before it is removed.
"""
import ipaddress
import json
import os
import sys

import yaml

FIELDS = ("kind", "ip", "ip6", "mem", "cpu", "disks", "size", "late", "optional", "overrides")
OVERRIDABLE = ("mem", "cpu", "size")


def die(msg, code=2):
    print("rig-nodes: " + msg, file=sys.stderr)
    sys.exit(code)


def load(path):
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as e:
        die("cannot read %s: %s" % (path, e))
    except yaml.YAMLError as e:
        die("%s is not valid YAML: %s" % (path, e))
    if not isinstance(data, dict):
        die("%s: the top level must be a mapping" % path)
    return data


def problems(data, st):
    """Every way the catalogue is wrong, as a list of strings. Empty = valid."""
    bad = []
    machines = data.get("machines")
    if not isinstance(machines, dict) or not machines:
        return ["machines: missing or empty — the catalogue lists every box the rig can create"]

    def int_field(name, spec, key, minimum):
        v = spec.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
            bad.append("machines.%s.%s must be an integer >= %d (got %r)" % (name, key, minimum, v))
            return None
        return v

    seen_ip, seen_ip6 = {}, {}
    for name, spec in machines.items():
        if not isinstance(spec, dict):
            bad.append("machines.%s must be a mapping" % name)
            continue
        if spec.get("kind") not in st.kinds:
            bad.append("machines.%s.kind must be one of %s (got %r)" % (name, ", ".join(st.kinds), spec.get("kind")))
        for key, version, seen in (("ip", 4, seen_ip), ("ip6", 6, seen_ip6)):
            if key not in spec:
                if key == "ip":
                    bad.append("machines.%s.ip is missing" % name)
                continue
            try:
                addr = ipaddress.ip_address(str(spec[key]))
                if addr.version != version:
                    raise ValueError
            except ValueError:
                bad.append("machines.%s.%s is not an IPv%d address (got %r)" % (name, key, version, spec[key]))
                continue
            if str(addr) in seen:
                bad.append("machines.%s.%s %s is also %s's" % (name, key, addr, seen[str(addr)]))
            seen[str(addr)] = name
        int_field(name, spec, "mem", 1)
        int_field(name, spec, "cpu", 1)
        disks = int_field(name, spec, "disks", 0)
        size = int_field(name, spec, "size", 0)
        if disks is not None and size is not None and (disks == 0) != (size == 0):
            bad.append("machines.%s: size must be 0 exactly when disks is 0 (disks=%d size=%d)" % (name, disks, size))
        if "late" in spec and not isinstance(spec["late"], bool):
            bad.append("machines.%s.late must be true or false" % name)
        if spec.get("late") and spec.get("kind") != st.cluster_kind:
            bad.append("machines.%s is late but not a %s node — only the addnode stage creates late machines"
                       % (name, st.cluster_noun))
        if "optional" in spec and not (isinstance(spec["optional"], str) and spec["optional"]):
            bad.append("machines.%s.optional must name a rig feature" % name)
        ov = spec.get("overrides", {})
        if not isinstance(ov, dict) or any(k not in OVERRIDABLE or not isinstance(v, str) for k, v in ov.items()):
            bad.append("machines.%s.overrides must map %s to a rig.resources key" % (name, "/".join(OVERRIDABLE)))

    pve = [n for n, s in machines.items() if isinstance(s, dict) and s.get("kind") == st.cluster_kind]

    roles = data.get("roles") or {}
    if not isinstance(roles, dict):
        bad.append("roles must be a mapping")
        roles = {}
    for role, val in roles.items():
        if not (isinstance(role, str) and role.isidentifier()):
            bad.append("roles: %r is not a valid role name (letters, digits, _)" % role)
            continue
        names = val if isinstance(val, list) else [val]
        if not names or not all(isinstance(n, str) for n in names):
            bad.append("roles.%s must name a machine or a list of machines" % role)
            continue
        for n in names:
            if n not in machines:
                bad.append("roles.%s names %s, which is not a machine" % (role, n))
            elif n not in pve:
                bad.append("roles.%s names %s, which is not a %s node" % (role, n, st.cluster_noun))

    scenarios = data.get("scenarios")
    if scenarios is not None:
        if not isinstance(scenarios, dict) or not scenarios:
            bad.append("scenarios must be a non-empty mapping")
        else:
            for sc, members in scenarios.items():
                if not isinstance(members, list) or not members:
                    bad.append("scenarios.%s must be a non-empty list of machines" % sc)
                    continue
                for n in members:
                    if n not in machines:
                        bad.append("scenarios.%s names %s, which is not a machine" % (sc, n))
                if len(set(members)) != len(members):
                    bad.append("scenarios.%s lists a machine twice" % sc)
            if data.get("default_scenario") not in scenarios:
                bad.append("default_scenario must name one of the scenarios (got %r)" % data.get("default_scenario"))
    elif "default_scenario" in data:
        bad.append("default_scenario is set but there are no scenarios")

    # The transition (optional): the inventory's copies must agree with the catalogue.
    inv = st.inventory
    if not inv:
        return bad
    nodes = data.get(inv["group"]) or {}
    if set(nodes) != set(pve):
        bad.append("%s (%s) and the machines of kind %s (%s) differ"
                   % (inv["group"], ", ".join(sorted(nodes)) or "none", st.cluster_kind, ", ".join(sorted(pve)) or "none"))
    hosts = dict(nodes)
    for group in (data.get(inv.get("conditional") or "") or {}).values():
        hosts.update(group or {})
    for name, hv in hosts.items():
        spec = machines.get(name)
        if not isinstance(spec, dict) or not isinstance(hv, dict):
            if name not in machines:
                bad.append("inventory host %s is not a machine" % name)
            continue
        if "ansible_host" in hv and str(hv["ansible_host"]) != str(spec.get("ip")):
            bad.append("%s: inventory ansible_host %s disagrees with machines ip %s"
                       % (name, hv["ansible_host"], spec.get("ip")))
        v6 = inv.get("v6_var")
        if v6 and v6 in hv and str(hv[v6]) != str(spec.get("ip6")):
            bad.append("%s: inventory %s %s disagrees with machines ip6 %s" % (name, v6, hv[v6], spec.get("ip6")))
    return bad


def checked(data, st):
    bad = problems(data, st)
    if bad:
        die("the catalogue is invalid (run `rig-nodes validate`): " + bad[0])
    return data


def scenario_members(data, scenario):
    machines = data["machines"]
    scenarios = data.get("scenarios")
    if scenarios is None:
        if scenario:
            die("no scenarios are defined, so --scenario %s means nothing" % scenario)
        return list(machines)
    name = scenario or data["default_scenario"]
    if name not in scenarios:
        die("unknown scenario %r (scenarios: %s)" % (name, ", ".join(scenarios)))
    members = set(scenarios[name])
    return [n for n in machines if n in members]          # creation order, always


def cmd_list(data, args, st):
    scenario, kind, late, only_late, subset, off = None, None, False, False, None, set()
    while args:
        a = args.pop(0)
        if a == "--scenario" and args:
            scenario = args.pop(0)
        elif a == "--kind" and args:
            kind = args.pop(0)
            if kind not in st.kinds:
                die("--kind must be one of %s" % ", ".join(st.kinds))
        elif a == "--include-late":
            late = True
        elif a == "--only-late":
            late = only_late = True
        elif a == "--off" and args:
            off.add(args.pop(0))
        elif a == "--subset" and args:
            subset = args.pop(0).split()
        else:
            die("list: unexpected argument %r" % a)
    machines = data["machines"]
    names = scenario_members(data, scenario)
    if subset is not None:
        # A subset narrows the cluster nodes `up` creates; it never names a support box
        # (those come with the rig) or a late node (addnode creates that one).
        allowed = [n for n in names if machines[n]["kind"] == st.cluster_kind and not machines[n].get("late")]
        for n in subset:
            if n not in allowed:
                die("--subset names %s, which this scenario cannot narrow to (its nodes: %s)"
                    % (n, " ".join(allowed)))
        names = [n for n in names if machines[n]["kind"] != st.cluster_kind or n in subset]
    if not late:
        names = [n for n in names if not machines[n].get("late")]
    if only_late:
        names = [n for n in names if machines[n].get("late")]
    if off:
        names = [n for n in names if machines[n].get("optional") not in off]
    if kind:
        names = [n for n in names if machines[n]["kind"] == kind]
    print(" ".join(names))


def main(argv=None):
    from .settings import load_settings
    from ...config import ConfigError
    argv = list(sys.argv[1:] if argv is None else argv)
    root = None
    if argv[:1] == ["--root"]:
        if len(argv) < 2:
            die("--root needs a directory")
        root, argv = argv[1], argv[2:]
    try:
        st = load_settings(root)
    except ConfigError as e:
        die(str(e))
    path = str(st.catalogue_file)
    if argv[:1] == ["--file"]:
        if len(argv) < 2:
            die("--file needs a path")
        path, argv = argv[1], argv[2:]
    if not argv:
        print(__doc__.split("\n\n")[3], file=sys.stderr)
        sys.exit(2)
    cmd, args = argv[0], argv[1:]
    data = load(path)
    if cmd == "validate":
        bad = problems(data, st)
        for b in bad:
            print("rig-nodes: " + b, file=sys.stderr)
        if bad:
            sys.exit(1)
        root_s = str(st.root)
        shown = os.path.relpath(path, root_s) if os.path.abspath(path).startswith(root_s + os.sep) else path
        print("rig-nodes: %s is valid (%d machines)" % (shown, len(data["machines"])))
        return
    checked(data, st)
    machines = data["machines"]
    if cmd == "list":
        cmd_list(data, list(args), st)
    elif cmd == "get":
        if len(args) != 2:
            die("get needs <machine> <field>")
        name, field = args
        if name not in machines:
            die("no machine %r" % name)
        if field not in machines[name]:
            die("machine %s has no field %r" % (name, field))
        v = machines[name][field]
        print(json.dumps(v) if isinstance(v, (dict, list, bool)) else v)
    elif cmd == "role":
        if len(args) != 1:
            die("role needs <role>")
        roles = data.get("roles") or {}
        if args[0] not in roles:
            die("no role %r (roles: %s)" % (args[0], ", ".join(roles) or "none"))
        v = roles[args[0]]
        print(" ".join(v if isinstance(v, list) else [v]))
    elif cmd == "scenarios":
        sc = data.get("scenarios")
        if sc is None:
            print("")
        else:
            d = data["default_scenario"]
            print(" ".join([d] + [s for s in sc if s != d]))
    elif cmd == "shell":
        # One eval for everything a harness needs at load. Values are shell-quoted; names
        # are fixed (RIG_*; <K> = the cluster kind, upper-cased), and a role becomes
        # RIG_ROLE_<role> (role names are identifiers; validate refuses anything else).
        import shlex
        scenario = None
        if args[:1] == ["--scenario"] and len(args) == 2:
            scenario = args[1]
        elif args:
            die("shell: unexpected argument(s) %s" % " ".join(args))
        names = scenario_members(data, scenario)
        k = st.cluster_kind.upper()
        pve = [n for n in names if machines[n]["kind"] == st.cluster_kind]
        out = {
            "RIG_%s_NODES" % k: [n for n in pve if not machines[n].get("late")],
            "RIG_LATE_NODES": [n for n in pve if machines[n].get("late")],
            "RIG_ALL_%s_NODES" % k: pve,
            # every cluster machine of the catalogue, whatever the scenario: what a VM
            # that EXISTS is checked against to tell a node from a support box
            "RIG_CATALOGUE_%s" % k: [n for n in machines if machines[n]["kind"] == st.cluster_kind],
            "RIG_SCENARIOS": ([data["default_scenario"]] + [x for x in data["scenarios"] if x != data["default_scenario"]])
                             if data.get("scenarios") else [],
        }
        for role, val in (data.get("roles") or {}).items():
            out["RIG_ROLE_" + role] = val if isinstance(val, list) else [val]
        # RIG_FEATURE_<feature> = the machine(s) that exist only while that rig feature
        # is on (`optional:`), so a box is found by what it is for, not by its name.
        for n in names:
            f = machines[n].get("optional")
            if f:
                out.setdefault("RIG_FEATURE_" + "".join(c if c.isalnum() else "_" for c in f), []).append(n)
        for k, v in out.items():
            print("%s=%s" % (k, shlex.quote(" ".join(v))))
    elif cmd == "json":
        scenario, every = None, False
        if args[:1] == ["--scenario"] and len(args) == 2:
            scenario = args[1]
        elif args == ["--all"]:
            every = True                    # every machine, whatever the scenario (the inventory)
        elif args:
            die("json: unexpected argument(s) %s" % " ".join(args))
        names = list(machines) if every else scenario_members(data, scenario)
        print(json.dumps({"machines": [dict(name=n, **machines[n]) for n in names],
                          "roles": data.get("roles") or {}}, indent=2))
    else:
        die("unknown command %r" % cmd)


if __name__ == "__main__":
    main()
