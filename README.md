# testrunner

A generic, asynchronous, state-machine test runner for **stateful systems**: things you
build up step by step (install, configure, cluster, break, repair, tear down), where
order matters, steps take minutes, prompts need answering and a failed run must be left
standing for inspection.

It borrows pytest's ideas — discovery by convention, decorators that export functions,
fixtures injected by parameter name with `yield` teardown, hooks — and adds what pytest
does not model: an explicit **state machine**, **paths with detours and outcome-driven
branches**, an **expected-state matrix**, **pty-driven processes** that answer prompts,
and an operator who can still type while things run.

testrunner keeps **no state of its own**. Everything persistent lives in the project
that uses it (the *subscriber*), written through the store API testrunner provides.

Requires Python ≥ 3.11 and, for YAML configs, PyYAML. TOML needs nothing extra.

## Use it in a project

```sh
git subtree add --prefix=testrunner https://github.com/alc-kit/testrunner <tag> --squash
testrunner/bin/testrunner --list
```
Update with `git subtree pull --prefix=testrunner … <tag> --squash`. Change testrunner
in this repository, not in a subscriber's copy.

Further reading:
- `docs/extending.md`: building a test framework on testrunner. Where each kind of code goes,
  designing the state model, and writing an extension.
- `docs/real-hardware.md`: driving real machines (BMC, SSH, destructive steps), with the working
  example `examples/bare-metal/`.
- `docs/writing-actions.md`: writing one action, step by step.

## Concepts

**States.** The subscriber declares variables that describe the system under test:
```yaml
states:
  service: {values: [absent, installed, running], initial: absent}
```

**Actions** are the exported functions, each with a contract:
```python
from testrunner import action, Outcome

@action(requires={"service": "installed"}, produces={"service": "running"},
        outcomes=["degraded"])
async def start(proc, params):
    r = await proc.run(["systemctl", "start", "thing"], log="start.log")
    if not r.ok:
        return False
    if "slow" in r.text:
        return Outcome("degraded", "started slowly")
```
Return `None`/`True` (passed), `False` (failed), an `Outcome`, or raise. A sync function
runs in a thread. `readonly=True` marks an action that only looks.

Shell commands export the same contract from YAML, so existing scripts work unchanged.
Every command testrunner starts sees `TR_STEP`, `TR_ACTION`, `TR_RUN_ID`, `TR_STATE_DIR`,
`TR_CONFIG_FILE`, `TR_PARAMS` (the step's parameters, JSON) and `TR_WITH` (only the
overrides: plan `with`, `--with` and the step's own `with`, JSON):
```yaml
actions:
  install:
    run: ["bash", "scripts/install.sh", "{target}"]   # {name} = a parameter
    rules: scripts/install-rules.yml                   # expect rules for its prompts
    exit_codes: {3: refused}                           # an exit code -> a named outcome
    requires: {service: absent}
    produces: {service: installed}
```

`produces` can also be a **transition map**: `produces: {rig: {up: installed}}` moves
`up` to `installed` and leaves any other value alone, so re-running an idempotent step
on a system that is further along does not move it backwards.

**Fixtures** — built in: `run`, `config`, `store`, `ui`, `registry` (per run) and `step`,
`params`, `proc`, `state` (per step). Your own:
```python
from testrunner import fixture

@fixture(scope="run")
async def api(config):
    client = await connect(config["params"]["url"])
    yield client
    await client.close()          # teardown, also after a failure
```

**Observers** read reality; where one exists, it wins over what an action claimed:
```python
@observer("service")
def service_state(api): return api.status()
```
They are asked before the plan is judged, when the run starts, and after every step, whatever
its outcome, except after a `readonly` action, which only looks.

**Hooks**: `run_start`, `step_start`, `step_end`, `run_end` — `@hook("step_end")`; a
hook receives only the arguments it names (`run`, `step`, `result`).

`run_config` runs earlier, as each run config LOADS and before anything is planned from
it: a plain (not async) function that may change `config` in place, given `how` (how the
config was chosen), `file` and `root`. A step's `when:` and the scenario snapshot then see
what it made of the config — e.g. a value the subscriber takes from the environment. Every
key it changes is logged (NOLOG values masked); an exception refuses the run.

## Plans

A run config names a path, plus anything that changes how it is walked:
```yaml
extends: default
params: {rows: 3}
plan:
  path: lifecycle                    # a named path, a range (up..verify) or a list
  with: {verbose: true}              # parameters for every step
  detours:                           # inject steps, then continue on the path
    - {after: start, steps: [{action: verify, id: early-verify}]}
  on:                                # react to an outcome
    seed: {degraded: [repair]}       #   run steps, then continue
    verify: {failed: stop}           #   or: continue, stop, {goto: <step>}
  expect:                            # the expected-state matrix
    wrong-rows: {outcome: failed}    #   a negative control
    start: {state: {service: running}}
```
By default the walk is linear: a step that ends as expected continues, anything else
stops the run and leaves the system as it is. Steps can also carry `when:` (a condition
on the config) and `max_visits:` (a bound for `goto` loops).

## State machine

A run is a walk over a state machine. Every part of it is declared, and `--plan --matrix`
prints the whole of it as a table, which also goes in the journal and both reports.

- **States**: the runner config's `states:` (e.g. `rig: [absent, up, installed, clustered]`).
  Observers read the real value, and it wins over the model.
- **Events**: a step's outcome: `passed`, `failed`, `errored`, `aborted`, `skipped`, or a named
  one the action declares (`outcomes=["degraded"]`).
- **Guards**: an action's `requires` (the states it may start from). Checked before it runs, and
  statically over every branch before the run starts.
- **Effects**: an action's `produces` (on `passed`) and `produces_on` (per outcome): the state it
  leaves. The observers then read what it really left.
- **Transitions**: what comes next, decided by how the step ENDED. Two forms:
  ```yaml
  on:
    verify: {failed: stop}             # by outcome only: continue | stop | {goto: id} | [steps]
    join:                              # rules on outcome AND the observed state; first match wins
      - when: {outcome: passed, state: {rig: clustered}}   # the marker that means "go on"
        then: continue
      - when: {outcome: passed, state: {rig: installed}}   # joined, but no cluster formed
        then: {goto: form}
      - when: {outcome: failed}
        then: [membership]                                 # run these, then go on
      # no rule matches: the run stops, failed
  ```
  - A step with no `on:` continues when it ends with its expected outcome (`expect:`, default
    `passed`), and stops the run otherwise.
  - The outcome-only form keeps that default for outcomes it does not list. A rule list does
    not: what it does not match stops the run.
  - A config re-routes an inherited plan with the walk edit
    `{transition: join, rules: [...]}`, which replaces that step's rules.
  - The static check follows every rule's branch, including a state only an observer can
    report.

```
$ testrunner --plan --matrix          # the join step of the example above
  step    | requires (state in)      | outcome (event) | state after    | next
  --------+--------------------------+-----------------+----------------+------------------------------------
  join    | rig: installed|clustered | passed*         | rig: clustered | outcome passed rig=clustered -> continue; outcome passed rig=installed -> goto form; otherwise -> stop (failed)
          |                          | failed          | (unchanged)    | outcome failed -> run membership, then go on
          |                          | errored         | (unchanged)    | otherwise -> stop (failed)
          |                          | aborted         | (unchanged)    | stop (aborted)
```

**Editing a walk.** A run config's `edit:` list changes the walk its plan compiles to, without
restating the plan: in order, each edit anchored on a step id. Edits **accumulate through
`extends:`** (the parent's first), so a child config adds a test to the walk it inherits:
```yaml
extends: qdevice
edit:
  - repeat: ping                 # the same step again (same action, with, when), new id
    after: join                  #   after: or before: a step id
    id: ping-after-join          #   optional; default ping#2
  - insert: [{action: membership, id: members-after-qdevice}]
    after: qdevice
  - skip: [cache-verify]         # one id or a list
  - replace: images              # one step out, these in, at its place
    by: [{action: images, with: {source: mirror}}]
```
A later edit may anchor on an id an earlier one made, and `on:`/`expect:` may name it. In a
config every name must be on the walk (an error otherwise); with steps given on the command line
an edit whose anchor is not in the run is dropped with a note. The edits are part of the step
source in `--plan`, the banner, the journal and the report:
`steps: plan of qdevice.yml (via x.yml); edits: +ping-after-join (again ping) after join, -cache-verify`.

**Retry, on evidence.** A step can be retried, as a walk edit or as `retry:` on the step in a
path. It is retried only when the evidence says a retry can help:
```yaml
edit:
  - retry: install               # a step id
    times: 2                     # at most 2 more attempts
    delay: 60                    # seconds before each
    if:                          # ALL must hold (outcome defaults to [failed, errored])
      outcome: [failed, errored]
      detail: "regex"            #   on the outcome's detail
      output: "Could not get lock|Temporary failure resolving"   # what the attempt printed
      state: {rig: up}           #   the OBSERVED state after the attempt
      probe: apt-reachable       #   a read-only action, run now; must pass
    unless:                      # ANY match: no retry (same keys)
      output: "No space left on device"
```
A step that ended as the plan expected, was skipped by `when`, or was aborted is never retried.
`requires` is checked again before each attempt, and the step's outcome is the last
attempt's. The journal records every attempt (`step_start`/`step_end` with `attempt: 2/3`) and
every decision with its evidence (`step_retry`, `step_no_retry`: `no retry after attempt 1/3
(failed): outcome failed; output did not match 'Could not get lock'`). The reports show one row
per attempt. A probe runs as its own step (`install~probe1`), so its evidence is in the journal
too. `on:`/`goto` stay for real branching.

**Redo.** `--from STEP` starts the compiled walk (plan, edits and all) at STEP; `--resume` starts
it at the step the last run stopped at (refused when the last run passed, or when that step is no
longer on the walk). `on:`/`expect:` naming a step left out are dropped with a note. One-run edits
mirror `edit:` and are notes, never errors, when a name is not on the walk:
`--skip STEP`, `--repeat STEP@after:ID`, `--insert ACTION@before:ID`, `--retry STEP=TIMES[:DELAY]`
(evidence conditions belong in a config's `edit:`).

**`all` vs the plan.** On the command line, `all` means the run config's plan: it compiles
exactly like a bare `testrunner`, `with`/`detours`/`on`/`expect` included. When the plan is
empty, `all` runs the runner config's `all.fallback` path instead (with the plan's other keys,
leniently: a detour whose anchor is not on that path is dropped with a note). `all` stands
alone on the command line, and is refused inside a plan or path, where it would mean the plan
itself: name the fallback path there. Every walk says where its steps came from, in `--plan`,
the run banner, the trace, the journal and `--report`:
```
steps: plan of sdn.yml (via sdn+lvmthin.yml)          # bare run; the plan is inherited
steps: all = plan of sdn.yml (via sdn+lvmthin.yml)
steps: all = path full: no plan in default.yml
steps: path full (command line)
steps: command line: up..join verify
```

**Before anything runs**, the observers are asked what the system IS (a run, not `--plan`,
which reads only the store), and the plan is simulated from there over every reachable branch: every
`requires` must hold, every reaction must name an outcome its action can end with, every
`expect.state` must be reachable. An impossible plan is refused before the first step.

## The scenario lock

The first state-changing run in a directory **selects the scenario**: its resolved run
config is snapshotted into the store. Every later or parallel runner in that directory
runs on that snapshot. Picking another config there is refused; editing the config file
gives a warning, and the snapshot still applies. The scenario ends with `--release`, or
automatically when a run reaches the state named in the runner config:
```yaml
scenario:
  release_when: {service: absent}
```
State-changing runs take an exclusive writer lock. Runs of read-only actions do not
take it, so they can run beside a state-changing run.

## Processes, prompts and colour

`proc.run()` runs a command on a pseudo-terminal, so it behaves as it would for a person.
That covers prompts, raw-mode gates and colour. Expect rules (`{expect: <regex>, send:
"YES\r", delay: 1.5}`) answer prompts, and are matched against the output **with colour
codes stripped**. The colour itself is forwarded to your terminal untouched, and log
files keep the raw bytes (`less -R`).

If no rule answers a prompt, it goes to the operator, or fails the step when nobody is
attached (`--non-interactive`, CI). While a run is going you can type `status` or
`abort`. Colour follows `--color auto|always|never`, `NO_COLOR` and `FORCE_COLOR`.

## Reports

A report is written only when you ask for one:
```sh
testrunner --report                       # run the plan, then write a markdown test report
testrunner --report --report-format both  # markdown + a self-contained HTML timing page
testrunner --report-of <run>|last         # (re)build the report of an earlier run
```
Reports go to `<state_dir>/reports/<started>-<run>.md|.html`; `latest.md` and
`latest.html` always point at the newest.

The **markdown** report has the result, a table of steps (outcome, start, duration, change
against the median of earlier passed runs of the same step, metrics), and a Mermaid
timeline. For every step that did not end as expected, it adds the detail and the last
lines of that step's log, with colour codes stripped and NOLOG values already masked.

The **HTML** page shows the same run as charts: a timeline coloured by outcome, and each
step's change against earlier runs. It needs nothing external, so it opens offline.

Both are built from the journal alone, so any past run can be reported afterwards. An
action or a `step_end` hook can add its own numbers with `step.metric("tasks", 779)`;
they are recorded in the journal and shown in both formats.

## Debugging a run: the trace

The terminal shows what a run **did**. The trace shows what testrunner **decided** and why:
which run config it chose and how, `when:` key by key (the value the config holds against
the one the step wants), `requires` against the current state, what each outcome
produced, what observers read, which way the walker went, every fixture set up, and
every command started — argv, cwd, the environment it got on top of the runner's own,
exit code, duration — with the output of commands whose output went nowhere else (the
silent probes whose answers decide what happens next).

Every run, `--plan`, `--select` and `--release` writes one, at DEBUG, whatever the terminal shows:
```
<state_dir>/logs/testrunner-<run id>.log     (testrunner-last.log points at the newest; 50 kept)
```
Its path is printed with the plan banner and again when a run fails. To see the same log
on the terminal (stderr) as it happens:
```
testrunner -v  ...          decisions and milestones (INFO)
testrunner -vv ...          everything (DEBUG)
testrunner --log-level WARNING ...     or TESTRUNNER_LOG_LEVEL=info in the environment
```
Each line names the step it belongs to — also lines from fixtures, background tasks and
your own code — and the file:line that wrote it:
```
2026-10-11T14:12:45.120Z INFO    [up] testrunner.runner (runner.py:368): up: requires {'rig': 'absent'} against state {'rig': 'absent'} -> holds
2026-10-11T14:12:47.004Z DEBUG   [up] testrunner.proc (proc.py:147): cmd#7 start: ansible -i inventory localhost -m debug ...
2026-10-11T14:12:48.311Z INFO    [up] testrunner.ext.ansible (ansible.py:181): hostvars['n1'].backend resolves to 'zfs'
```
To see **how each command was run** — to rerun one by hand, or to show it — add
`--show-commands` (or set `TESTRUNNER_SHOW_COMMANDS=1`). Before every command starts,
the terminal gets one line you can paste into a shell: its directory, the environment
it got on top of yours (`env -u` for a variable it had unset), and its arguments:
```
$ cd /srv/rig/automation && env -u RIG_SHAPE RIG_BACKEND=lvm-thin ansible-playbook site.yml -i /srv/rig/inventory --limit n1,n2
```
Commands whose output is not shown (the probes) are listed too, marked as such. NOLOG
values are masked, and a `nolog` action's commands show the program name only.

Log from your actions with the standard library — no testrunner API needed:
```python
import logging
log = logging.getLogger(__name__)
log.info("3 of 4 nodes answer; skipping the fourth: %s", reason)
```
NOLOG values are masked in the trace too, and a `nolog` action's commands are logged
by program name only (no arguments, environment or output). Logging never changes a
run: a broken log call is reported by Python's logging and the step goes on.

## NOLOG: values that must not be logged

Nothing is treated as secret unless it is **marked**. A marked value is replaced by
`********` in everything testrunner writes: step logs, the terminal, the journal, outcome
details and tracebacks. The action or command still gets the real value.
```yaml
params:
  api_token: {NOLOG: "abc123"}     # YAML or TOML: api_token = { NOLOG = "abc123" }
  db_password: !NOLOG hunter2      # YAML shorthand
```
- `run.nolog(value)` marks a value created during the run, such as a password an action generated.
- An expect rule with `nolog: true` keeps its answer out of the log.
- An action with `nolog=True` (shell: `nolog: true`) runs commands whose output is
  neither logged nor shown.

The scenario snapshot in the state directory keeps the real values, as the config file
does, so later runners can use them. It also records which values are NOLOG.

## The command line

```
testrunner                       run the run config's plan
testrunner all                   the same; an empty plan runs the all.fallback path
testrunner a..c d                run these steps (actions, paths, ranges), in order
testrunner --plan [steps]        validate and print the walk; run nothing
testrunner --from STEP | --resume    start the walk at STEP / where the last run stopped
testrunner --skip S --repeat S@after:ID --insert A@before:ID --retry S=N[:DELAY]   one-run edits
testrunner --list | --status | --scenario | --select -c NAME | --release | --report-of RUN
  --report  --report-format md|html|both
  -c/--config NAME   -w/--with key=value   --color MODE   --non-interactive   --root DIR
  -v | -vv | --log-level LEVEL     testrunner's own log on stderr (the trace file is always written)
  --show-commands                  every command before it runs, as a copy-pasteable shell line
```
Exit codes: 0 passed, 1 failed, 2 refused (config, plan or lock).

## Extensions

`testrunner.ext` holds the tooling a subscriber builds its actions on. A subscriber imports
what it needs:
- `ext.ansible`: playbooks and inventory queries, refusing an empty scope.
- `ext.vagrant`: `vagrant status` and `up`, and what exists.
- `ext.console`: serial console capture.
- `ext.rig`: a whole vagrant + ansible test rig.

An extension's settings live in the runner config under `ext: {<name>: {...}}`.

**`ext.rig`** is what every such rig is built from, whatever it tests:
- the run config and the environment it becomes (`rig.config`, a schema engine: the rig's
  `<configs>/schema.yml` declares its keys, types, allowed values, rules and environment names);
- the node catalogue (`rig.nodes`);
- the generated `ansible.cfg` and harness key;
- scope guards: only nodes that EXIST, and never an empty scope;
- the `rig` observer, the purge of stale per-node state, drive rules for the playbooks' prompts,
  the console capture and the sweep for leftover libvirt domains.

A rig describes itself in data:
```yaml
ext:
  rig:
    name: myrig
    prefix: MYRIG                 # its environment: MYRIG_*
    cluster_kind: node            # the catalogue `kind:` whose machines form the cluster
    cluster_noun: cluster         # "a <noun> node" in messages
    configs: configs              # run configs + schema.yml
    catalogue: lifecycle/nodes.yml
    stale_files: [secrets/recovery_key.txt]
```
It subclasses `testrunner.ext.rig.Rig` only for what it does differently. The three tools also
run as commands for a Vagrantfile or a shell:
```
python -m testrunner.ext.rig.config --root <rig> {select|env|show|plan|lock-write|lock-diff} ...
python -m testrunner.ext.rig.nodes  --root <rig> {validate|list|shell|get|role|scenarios|json} ...
python -m testrunner.ext.rig.drive_rules {check|render} ...
```
A rig usually keeps a three-line `bin/` wrapper for each. The package docstring
(`testrunner/ext/rig/__init__.py`) and each module's header have the details.

## The runner config

`testrunner.yml` (or `.toml`) at the subscriber root:
```yaml
modules: [actions/]            # python exports
pythonpath: [lib/]             # directories those modules import from
actions: {...}                 # shell exports
state_dir: state               # the subscriber's state (journal, state, scenario, logs)
states: {...}
paths: {lifecycle: [install, start, seed, verify]}
default_path: lifecycle        # what ranges slice (implied when there is one path)
all: {fallback: lifecycle}     # what `all` runs when the plan is empty (no path may be named `all`)
configs: {dir: configs, env: MY_CONFIG, link: my.yml, default: default}
params_section: params         # the run-config section that holds step parameters
scenario: {release_when: {...}}
input:
  prompt_idle: 20              # seconds of silence mid-line before a prompt is assumed...
  prompt_pattern: '[:?>\]#$]\s*$'   # ...on a line that ends like a prompt
```
`examples/toy/` is a complete subscriber that runs; read it next, then
`docs/writing-actions.md` for how to add an action.

## Licence

MIT — see `LICENSE`.
