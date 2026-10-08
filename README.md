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

**Hooks**: `run_start`, `step_start`, `step_end`, `run_end` — `@hook("step_end")`; a
hook receives only the arguments it names (`run`, `step`, `result`).

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

**Before anything runs**, the plan is simulated over every reachable branch: every
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
testrunner a..c d                run these steps (actions, paths, ranges), in order
testrunner --plan [steps]        validate and print the walk; run nothing
testrunner --list | --status | --scenario | --select -c NAME | --release
  -c/--config NAME   -w/--with key=value   --color MODE   --non-interactive   --root DIR
```
Exit codes: 0 passed, 1 failed, 2 refused (config, plan or lock).

## The runner config

`testrunner.yml` (or `.toml`) at the subscriber root:
```yaml
modules: [actions/]            # python exports
actions: {...}                 # shell exports
state_dir: state               # the subscriber's state (journal, state, scenario, logs)
states: {...}
paths: {lifecycle: [install, start, seed, verify]}
default_path: lifecycle        # what ranges slice (implied when there is one path)
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
