#!/usr/bin/env python3
"""Compare what a Vagrantfile DEFINES (a record.rb log) with the node catalogue.

Shared, byte for byte, by proxmox-gate3 and proxmox-lab-opnsense.

  check.py <recording.json> <rig-nodes json> <resources.json> <apt_cache 0|1> [console log|off]

resources.json maps a rig.resources key to the value this run sets (0 = unset). For
every catalogue machine (an `optional: apt_cache` one only while the cache is on),
the Vagrantfile must define it, IN CATALOGUE ORDER, with autostart = not late, the
memory / cpus / data-disk count / disk size the catalogue gives after overrides,
and its ip (and ip6) somewhere in its own configuration — and define nothing else.
Every `kind: pve` machine, and only those, carries an emulated IPMI BMC (the fleet's
servers have one; without it the node's openipmi.service fails on every boot).
With console `log` (the default), every `kind: pve` machine, and only those, gets the
serial-console provisioner armed (GRUB/kernel/initramfs on ttyS0, which run.sh
captures); with `off`, none is armed.
Prints one line per mismatch; exit 1 if any.
"""
import json
import sys

rec = json.load(open(sys.argv[1]))
machines = json.load(open(sys.argv[2]))["machines"]
resources = json.load(open(sys.argv[3]))
cache_on = sys.argv[4] == "1"
console_log = (sys.argv[5] if len(sys.argv) > 5 else "log") == "log"

want = []
for m in machines:
    if m.get("optional") == "apt_cache" and not cache_on:
        continue
    spec = {k: m[k] for k in ("mem", "cpu", "disks", "size")}
    for field, key in (m.get("overrides") or {}).items():
        if int(resources.get(key, 0)) > 0:
            spec[field] = int(resources[key])
    want.append((m["name"], m, spec))

defines = [(e[2][0], e[3].get("autostart", "true")) for e in rec if e[0] == "config.vm" and e[1] == "define"]
bad = []
if [d[0] for d in defines] != [w[0] for w in want]:
    bad.append("defined %s, the catalogue says %s" % ([d[0] for d in defines], [w[0] for w in want]))
auto = dict(defines)
for name, m, spec in want:
    if name not in auto:
        continue
    mine = [e for e in rec if e[0].startswith("config.vm.define[%s]" % name)]
    def one(method):
        vals = [e[2][0] for e in mine if e[1] == method and e[0].endswith("provider[libvirt]")]
        return int(vals[-1]) if vals else None
    disks = [e for e in mine if e[1] == "storage" and e[0].endswith("provider[libvirt]") and e[3].get("size")]
    got = {"autostart": auto[name], "mem": one("memory="), "cpu": one("cpus="), "disks": len(disks),
           "size": int(disks[0][3]["size"].rstrip("G")) if disks else 0}
    exp = {"autostart": "false" if m.get("late") else "true", **spec}
    for k in exp:
        if str(got[k]) != str(exp[k]):
            bad.append("%s.%s: the Vagrantfile gives %s, the catalogue %s" % (name, k, got[k], exp[k]))
    if disks and any(e[3]["size"] != disks[0][3]["size"] for e in disks):
        bad.append("%s: its data disks differ in size" % name)
    qargs = [e[3].get("value", "") for e in mine if e[1] == "qemuargs" and e[0].endswith("provider[libvirt]")]
    has_bmc = "ipmi-bmc-sim,id=bmc0" in qargs and any(a.startswith("isa-ipmi-kcs,bmc=bmc0") for a in qargs)
    if has_bmc != (m.get("kind") == "pve"):
        bad.append("%s (kind %s): %s an IPMI BMC" % (name, m.get("kind"), "has" if has_bmc else "lacks"))
    inlines = [e[3].get("inline", "") for e in mine if e[1] == "provision" and e[2][:1] == ["shell"]]
    armed = any("console=ttyS0" in t and "console=1" in t for t in inlines)
    if armed != (console_log and m.get("kind") == "pve"):
        bad.append("%s (kind %s, console %s): serial console %s" % (
            name, m.get("kind"), "log" if console_log else "off", "armed" if armed else "NOT armed"))
    text = json.dumps(mine)
    for k in ("ip", "ip6"):
        if k in m and m[k] not in text:
            bad.append("%s.%s %s appears nowhere in its configuration" % (name, k, m[k]))
print("\n".join(bad))
sys.exit(1 if bad else 0)
