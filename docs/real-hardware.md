# Driving real hardware with testrunner

Nothing in testrunner's core assumes a virtual machine. A run is a walk over a state machine:
- the steps act on a system;
- observers read what the system IS;
- the plan says what must happen next.

The "system" can be one server, a rack, a switch or a whole cluster. The VM rigs this
repository grew up with (`testrunner.ext.vagrant`, `ext.console`, `ext.rig`) are one kind of
system. This page covers the other kind, and `examples/bare-metal/` is a working subscriber
for it. `tests/test_bare_metal_example.py` runs that subscriber end to end against a stub BMC
and a stub server, so this page cannot drift from the code.

## What changes on real hardware

| | a VM rig | real hardware |
|---|---|---|
| create / destroy | cheap, repeatable: `vagrant up`, `destroy` | expensive or destructive: reinstall, wipe, re-cable |
| where the state is | vagrant and the cluster, both close at hand | in the machine: BMC power, firmware, disks, the OS, the cluster |
| a failed step | throw the VM away | the machine is now in some state; find out which |
| the console | `virsh console` (`ext.console`) | the BMC's serial-over-LAN (IPMI SOL, Redfish) |
| credentials | throwaway | real: BMC passwords, vault keys |
| "is it there?" | `vagrant status` | power state, reachability, what answers on the address |

So on hardware the observers carry more weight, destructive steps need gates, and secrets need
care. The engine already has a mechanism for each of these.

## The example, file by file

`examples/bare-metal/` drives one server through its BMC (`ipmitool`) and SSH.

**`testrunner.yml`**: the state model.
```yaml
states:
  power: {values: ["off", "on"]}         # no `initial`: only the BMC knows
  os:    {values: [absent, installed]}   # no `initial`: only the machine knows
paths:
  lifecycle: [survey, power-on, reinstall, verify]
scenario:
  release_when: {power: "off"}           # one checkout drives one machine
```
Neither variable has an `initial` value. The runner reads both from the machine before it
judges the plan, and the static check then starts from what is really there.

**`actions/metal.py`**: one observer per state, and four actions.
- `power` asks the BMC (`ipmitool ... chassis power status`).
- `os` asks the machine (`ssh ... cat /etc/os-release`).

Observers run outside any step. A run-scoped fixture (`shell`) gives them a command runner that
masks NOLOG values like the per-step `proc` does.

| action | contract | why |
|---|---|---|
| `survey` | `readonly=True` | reads the BMC's sensors; may run beside a writer, changes nothing |
| `power-on` | `produces: {power: on}` | the claim; the observer has the last word |
| `reinstall` | `requires: {power: on}`, `produces: {os: installed}`, outcome `refused` | destructive, behind a typed confirmation (below) |
| `verify` | `requires: {power: on, os: installed}`, `readonly=True` | the machine that answers is the one we meant |

**`configs/default.yml`**: the machine, and how the walk is judged.
```yaml
params:
  node: server-a
  host: 192.0.2.20
  bmc: 192.0.2.120
  bmc_user: admin
  bmc_password: {NOLOG: change-me}
plan:
  path: [lifecycle]
  expect:
    power-on: {state: {power: "on"}}     # the OBSERVER judges power-on, not ipmitool's exit code
edit:
  - retry: power-on
    times: 2
    delay: 10
    if: {state: {power: "off"}}          # retry only while the BMC still reports off
```

## Patterns that keep real hardware safe

**1. The observer is the judge.**
- Without the `expect`, a power-on the BMC accepted but never carried out would *pass*, and
  `reinstall` would fail later for an unclear reason.
- With it, `power-on` fails right there: `expected state not reached: power='on' (is 'off')`.
- Mark every step whose real result is a state this way.

**2. Retry on evidence, never blindly.**
- The retry above fires only when the observer still reads `power: off`.
- Attempts and decisions go into the journal with their evidence: `retry 2/3: outcome failed;
  observed state is {'power': 'off'}`.
- A BMC that never powers the chassis on fails after the retries, and `reinstall` never starts.
  Both cases are tested.
- Never put a retry on a destructive step unless its evidence (`output:`, `probe:`) proves the
  earlier attempt changed nothing.

**3. Destructive steps sit behind a gate that names the target.**
- `scripts/reinstall.sh` asks: "type the node name (server-a) to wipe it:".
- The action answers with a drive rule built from the config's `node`:
  `{"expect": "type the node name \\(server-a\\) to wipe it:", "send": "server-a\r"}`.
- Pointed at a machine whose installer asks for another name, the rule does not match. The
  step stops at the prompt (`--non-interactive`: an unanswered prompt fails its step), and
  nothing is wiped.
- A drive rule's answer ends in `\r`, because raw-mode prompts take CR as Enter.

**4. Verify the identity, not just reachability.** `verify` checks that the machine at the
address says it is `server-a`. A swapped cable or a reused address is caught (tested), instead
of an unrelated machine being certified.

**5. Credentials are NOLOG.**
- `bmc_password: {NOLOG: ...}` is masked as `********` in the terminal, the trace, the journal and
  `--show-commands` (tested).
- Pass it in the environment (`ipmitool -E` reads `IPMI_PASSWORD`), never in an argument list.
- One file keeps the real value on purpose: the scenario snapshot, `state/scenario.json`. Later
  runs of the same scenario need it, as they need the config file. Guard the state directory like
  the config, and never commit either.

**6. Look before you act.** `testrunner --plan` reads the machine through the observers, then
simulates the whole walk, every branch included, against the actions' contracts. A step that
would run on a machine in the wrong state is refused before anything touches it.
`--plan --matrix` prints the state/event matrix for review.

**7. One checkout, one target.** The scenario lock commits a checkout to its run config at the
first state-changing run. Another config is refused until the scenario is released, which
happens on an *observed* state (`release_when`) or by `--release`. Read-only runs (`survey`)
may run beside a writer.

**8. When a step fails, the machine is in some state: find out which, then go on.**
- After a failed step the observers read again, and the store holds what they saw.
- Fix the cause, then `testrunner --resume`, which starts at the step that stopped.
- Or use `--from STEP` to start later in the walk.
- If an observer cannot look (a BMC down), the run warns and keeps the model's value. A scenario
  is never released on a value nobody observed.

## Consoles on hardware

`ext.console` captures libvirt consoles (`virsh console`). On hardware the console is the BMC's
serial-over-LAN. The same approach works, but it is not shipped as an extension yet:
- run the capture under `script` as a background task the run owns (`run.spawn(...)`, from a
  `run_start` hook);
- one log per machine;
- stop it in `run_end`.

```python
import asyncio
import os
from testrunner import hook

@hook("run_start")                          # a hook is given `run` (and `step`/`result`), no other fixture
async def console_capture(run):
    p = run.run_config.data["params"]
    log = run.store.path("logs", "console", f"{p['node']}.log")
    cmd = f"ipmitool -I lanplus -H {p['bmc']} -U {p['bmc_user']} -E sol activate"
    proc = await asyncio.create_subprocess_exec(
        "script", "-q", "-f", "-c", cmd, str(log),
        env={**os.environ, "IPMI_PASSWORD": p["bmc_password"]},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL)
    run.spawn(proc.wait(), name="sol")      # cancelled with the run
```
This is a pattern, not tested code: check your BMC's SOL behaviour (one session at a time, the
`sol deactivate` it may need first).

## Using `ext.rig` with hardware

Some parts of `ext.rig` serve any fleet, and some assume VMs:

| part | on hardware |
|---|---|
| `rig.config`: the run-config schema engine, environment, rules, lock/diff | as is |
| `rig.nodes`: the node catalogue (machines, addresses, roles, scenarios) | as is. `kind:` names your node kind; `ip` is the management address |
| `Rig.ansible()`, the generated `ansible.cfg`, the harness key, the NOLOG secrets | as is, against your real inventory |
| `Rig.live_nodes` / `observe_rig` (asks `vagrant status`) | override: ask the BMCs or ping the hosts |
| `Rig.console_start` (libvirt) | override, or use the SOL pattern above |
| `Rig.domains_left` (libvirt domains) | not applicable |

A hardware rig therefore subclasses `Rig` and overrides the three methods that ask "what exists"
(`live_nodes`, `observe_rig`, `console_start`). Everything else, from scope guards that refuse an
empty node list to stale-state purges and drive rules, applies unchanged. If several hardware
frameworks need the same overrides, that is the moment to write an extension for them
(`docs/extending.md`).

## Before the first run against a real machine

- [ ] Every state variable has an observer, and no `initial` the machine could contradict.
- [ ] `expect` on every step whose result is a state.
- [ ] Every destructive step: a prompt naming the target, answered by a drive rule from the config.
- [ ] Credentials NOLOG; the state directory guarded like the config.
- [ ] `testrunner --plan --matrix` reviewed against the real machine.
- [ ] A dry run of the whole walk against stub tools (as `tests/test_bare_metal_example.py` does),
      with its negative controls.
- [ ] A read-only survey run first.
