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

Requires Python ≥ 3.11 (CI tests 3.11–3.15) and, for YAML configs, PyYAML. TOML needs nothing
extra.

## Use it in a project

```sh
git subtree add --prefix=testrunner https://github.com/alc-kit/testrunner <tag> --squash
testrunner/bin/testrunner --list
```
Update with `git subtree pull --prefix=testrunner … <tag> --squash`. Change testrunner
in this repository, not in a subscriber's copy.

A project usually starts it from a small wrapper, and says what the wrapper is called, so
the help (`./run.sh --help`) and every "run this next" hint name the command its operators
type, not `testrunner`. `$0` is exactly what they typed:
```sh
#!/usr/bin/env bash
export TESTRUNNER_PROG="$0"
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/testrunner/bin/testrunner" --root "$HERE" "$@"
```

Further reading:
- `docs/extending.md`: building a test framework on testrunner. Where each kind of code goes,
  designing the state model, and writing an extension.
- `docs/real-hardware.md`: driving real machines (BMC, SSH, destructive steps), with the working
  example `examples/bare-metal/`.
- `docs/writing-actions.md`: writing one action, step by step.

## Concepts

**States.** The subscriber declares the whole state machine in the runner config: each
variable's values, what each value means (`criterion`), how a claimed value is confirmed
(`validate`), and every transition, meaning which action may change the variable, on which
outcome, from which value to which:
```yaml
states:
  service:
    initial: absent
    validate: {outcome: true}          # every value's validator, unless a value names its own
    values:
      absent:    {criterion: "no service file"}
      installed: {criterion: "the service file says installed"}
      running:   {criterion: "the service file says running"}
    transitions:
      - {from: absent, to: installed, by: install, on: passed}
      - {from: "*",    to: running,   by: start,   on: passed}
```
The actions declare the same changes in their `produces` (below). **The two must agree, line
for line**: a claim the runner config does not declare, a declared transition no action
claims, or the same change on a different outcome makes the plan **undecided**. It is refused
before anything runs, with every mismatch listed. The list of transitions is complete: an
action/outcome pair that is not in it changes nothing. Undefined behaviour is a failure, never
a default. See [State machine](#state-machine) for the rest of the declaration (validators,
observers, `model_only`).

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
on a system that is further along does not move it backwards. The runner config declares it
as `{from: up, to: installed, by: <action>, on: passed}`. A plain `produces: {rig: absent}`
changes the variable from any value, and is declared `from: "*"`.

**Fixtures** — built in: `run`, `config`, `store`, `ui`, `registry` (per run) and `step`,
`params`, `proc`, `state` (per step). Your own are per run, per step, or **per look**:
`@fixture(scope="look")` is set up once per look, meaning one act of judging a state variable.
That is either a derived observer asking its values in order, or the check after a step (the
claimed value, the value before, the values the step could have moved it to). Every validator in
that look gets the same value, so a probe reads the system once and judges every value from that
one reading. Asked for outside a look (by an action, say), it is refused. Your own:
```python
from testrunner import fixture

@fixture(scope="run")
async def api(config):
    client = await connect(config["params"]["url"])
    yield client
    await client.close()          # teardown, also after a failure
```

**Observers** read reality:
```python
@observer("service")
def service_state(api): return api.status()
```
They are asked before the plan is judged, when the run starts, and after every step, whatever
its outcome, except after a `readonly` action, which only looks. At the start, what they read
replaces the stored state. After a step, an observer that contradicts the model FAILS the step
(see [State machine](#state-machine)). An observer answers one of three things:
- **a value**: it read that value, and the value is stored as reality;
- **`CANNOT_TELL`**: it did not read a value. The variable keeps the value it had, an unknown
  stays unknown, and the output says which observer could not tell;
- **`NONE_HOLDS`**: it looked, and the system is in none of the variable's values. The variable
  becomes **unknown**: before the plan, the steps that require it are refused; after a step,
  that step fails. It is journalled as `state_unknown` with `observed: "no value holds"`.

(`from testrunner import CANNOT_TELL, NONE_HOLDS`.) A derived observer (`observe: {order}`)
answers `NONE_HOLDS` when every value does not hold, and `CANNOT_TELL` when one value cannot tell.

**Validators** confirm a claimed value: `validate:` in the runner config, as a command, an
`@validator` function, an extension's kind (`playbook:`), or as weak as "the step passed"
(`{outcome: true}`). A claim is committed only when its validator holds:
```python
from testrunner import validator

@validator()                      # validate: {python: service-is}
def service_is(claim, api):
    return api.status() == claim.value, f"the API says {api.status()}"
```

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

- **States**: the runner config's `states:`, each variable with:
  - `values`: a list of names, or a mapping `{<value>: {criterion, validate}}`;
  - `initial`: the value before anything ran (leave it out when only an observer knows);
  - `criterion`: what a value MEANS, in a sentence. It is shown by `--list`, and later in the
    plan and reports;
  - `validate`: how a claimed value is confirmed: once for the variable, or per value. Every
    value a transition touches must have one, or the plan is refused;
  - `transitions`: every change, as `{from, to, by, on}`, all four stated. `from: "*"` means
    from any value. An inner step of a walk action is `by: <walk>/<step id>`;
  - **or** `machine`: the same machine as Mermaid `stateDiagram-v2` lines with UML labels, which
    render on GitHub unchanged when pasted into a ```` ```mermaid ```` fence. A variable uses one
    form; the YAML `transitions` stay valid during the migration window.

    ```yaml
    machine: |
      [*]       --> absent
      absent    --> up        : up [passed]
      up        --> installed : install [passed]
      %% the cluster was already formed
      installed --> founded   : form [skipped]
    ```

    The accepted subset is exactly this:
    - `[*] --> <value>`, the initial value, at most once (it replaces `initial`);
    - `<value> --> <value> : <action> [<outcome>]`;
    - `%%` comments on their own line (Mermaid does not accept one after a statement);
    - an optional first line, `stateDiagram-v2`.

    Anything else is refused with its line and the reason. That includes: another arrow, a
    composite state, a note, a label without `[outcome]`, `[*]` as a target, and any UML
    `/ effect` other than an update of declared data (below). There is no `*`: write a line for
    every value.

    **Every line written out.** For every value, every outcome of an action that has a
    transition from it needs its line: `passed`, `failed`, `errored`, `aborted`, `skipped`, and
    any outcome the action names. A missing one refuses the plan, and the message lists each as
    `v --> ? : action [outcome]`. An outcome that changes nothing is a **self-loop**
    (`up --> up : install [failed]`): a claim of no change, checked like any other. On a failure,
    it is the expected end result. A self-loop is declared once, in the machine; the action's
    `produces` claims only changes. The YAML `transitions:` form keeps the old closed world (an
    unlisted pair claims nothing) during the migration window. `--list` and the reports also show each variable as a **state × event
    table** (`-> <to> [<outcome>]` per cell, `·` where the action claims nothing from that value);
  - how it is read back. Exactly one of these applies to a variable a transition changes:
    - an `@observer`;
    - `observe: {order: [...]}`, an observer derived from the validators;
    - `model_only: true`: nothing re-reads it, and its value is what the steps claimed.

    A variable that no transition changes is a constant (its `initial`).

  Observers read the real value at run start, and it wins over the stored model.
- **Data** (extended state): `data: {expected: {initial: [n1, n2, n3]}}` declares a SET of names.
  `initial` may also be a template, rendered at run start with the run's resolvers (e.g. an
  extension's list of the scenario's nodes). It is changed **only** by a UML effect on a
  `machine:` line:

  ```
  clustered --> clustered : addnode [passed] / expected += {param.node}
  clustered --> clustered : rmnode [passed]  / expected -= {param.node}
  ```

  The effect applies only when that line's claim is **confirmed** by its validator; it is
  journalled as `data_changed` (the value, what the set was, what it is now, and which line did
  it). It is a set union or difference, so a member added twice is a journalled no-change. A
  failure line takes no effect. The value is stored beside the state, journalled at `run_start`
  and `run_end`, and shown by `--list`. Validators and probes see the current value: the run
  fixture `data`, and the template `{data.expected}`. That lets "clustered" be judged against
  the nodes expected **now**, not a fixed list.
- **Agreement**: the transitions declared in the runner config and the actions' `produces` /
  `produces_on` must match, line for line, outcomes included. If they don't, the plan is
  UNDECIDED and refused before anything runs (`--list`, `--plan` and every run). An inner step
  that calls an action without effects of its own carries that action's claims, which are
  checked once, as that action's.
- **Events**: a step's outcome: `passed`, `failed`, `errored`, `aborted`, `skipped`, or a named
  one the action declares (`outcomes=["degraded"]`).
- **Guards**: an action's `requires` (the states it may start from). Checked before it runs, and
  statically over every branch before the run starts.
- **Effects**: an action's `produces` (on `passed`) and `produces_on` (per outcome): the state it
  CLAIMS to leave. A claim is never taken on trust (next bullets).
- **Validators** answer one question for one value: does it hold now? There are three answers:
  *holds*, *does not hold*, *cannot tell*. How strong a validator is, is the subscriber's to
  decide and to describe. Every record says which validator confirmed what.

  | kind | holds | does not hold | cannot tell |
  |---|---|---|---|
  | `{outcome: true}` | the claiming step ended on the outcome that claims the change | it did not | — |
  | `{outcome: [id, ...]}` | these inner steps of the claiming walk passed (e.g. build and fetch) | one did not | — |
  | `{const: true}` | always: the claim needs no check | — | — |
  | `{run: [argv] or "shell", env, cwd, timeout}` | exit 0 | exit 1 | any other exit, a timeout, a prompt |
  | `{python: <name>}` (an `@validator`) | `True` | `False` | `None`, or it raises |
  | `{playbook: <path>, limit, extra, args, timeout}` (`ext.ansible`) | exit 0 | exit 2 (a task failed) | exit 4 (unreachable), any other; or `changed` under `--check` |
  | `{adhoc: {pattern, module, args}, limit}` (`ext.ansible`) | exit 0 | exit 2 | exit 4, any other; or a host `CHANGED` or `SKIPPED` |

  - Their values are templates like a walk step's (`{param.node}`, `{run.root}`, `{env.X}`).
  - `playbook:` and `adhoc:` always run with `--check`: a validator only looks. Under `--check`,
    ansible SKIPS `command`/`shell` tasks. Give a read-only command task `check_mode: false`, or
    the validator proves nothing. An ad-hoc command that was skipped answers *cannot tell*.
  - `run:` and `python:` validators cannot be forced to only look. Whoever writes one promises it.
  - An extension adds a kind with `@validator_kind("<name>", keys=(...))`. Its function returns
    `(answer, why)`.
- **Claims are confirmed.** After a step, for every variable its action declares a transition
  for, from the value the variable has now:
  - **The action ended `failed`, `errored` or `aborted`.** The transition declared for that
    outcome is the failure's **expected end result**. It may be an error state
    (`installed --> form_failed : form [failed]`), so a plan can expect a failure and deviate to
    repair the rig before going on. Its validator is asked:
    - **found**: the variable moves there (`state_changed`, "the failure's declared end result").
      The step stays `failed`, the plan's rules route it, and the observer reads after it as
      after any step;
    - **not found** (does not hold, or cannot tell): **the test fails here and the run stops,
      whatever the rules say.** The variable keeps its value from before the step, the last
      confirmed state (`state_kept`, with the verdicts). What the observers can attest is
      journalled as `observed_after_failure` and never applied;
    - **no transition declared for that outcome**: the value from before is kept, the observers'
      reading is recorded the same way, and the rules route as before. If the rig no longer
      matches the kept value, the next step's observer sees the difference and fails that step.
    - **Several end results declared for one value and failure outcome are its FAILURE MODES**
      (`absent --> quorum : make [failed]` and `absent --> split : make [failed]`; on the
      action, `produces_on={"failed": {"sys": {"absent": ["quorum", "split"]}}}`). Every mode's
      validator is asked from one reading:
      - exactly one holds: that is the mode, and the variable moves there;
      - none holds, several hold, or one cannot tell: the failure cannot be classified, and the
        test fails as above.

      The plan's rules are the decision matrix (`when: {outcome: failed, state: {sys: quorum}},
      then: [fix-quorum]`). **Every mode must be routed by a rule of that step**, `then: stop`
      included, or the plan is refused before anything runs. A list of end results is only for
      `failed`, `errored` and `aborted`; any other outcome has one.

    The scenario is never released on what nobody looked at: after a failure the release look
    reads reality into a copy (a kept value stays), and a kept value that no observer can read
    keeps the scenario.
  - **The outcome claims a change**: the target value's validator is asked. If it *holds*, the
    change is committed and journalled as `state_changed`, with the validator that confirmed it.
    Otherwise the step **fails**, and the variable is checked as below.
  - **The outcome claims nothing, or the claim did not hold** (a passed or skipped step that
    claims nothing; a refused claim): nothing is assumed. The value before, and every value the
    action's transitions could lead to, are asked:
    - exactly one holds: that is the value (`state_checked` when it is unchanged; `state_changed`
      when it moved);
    - none holds, several hold, or one cannot tell: the variable becomes **unknown**, and the
      step fails. Its error tells the whole story: the claim, every validator's answer and why,
      what is blocked (steps that require the variable do not run; the scenario is not
      released), which logs to read, and what to do next. It is journalled as `state_unknown`.
  - A dry run asks no validator: the model carries the walk.
- **Observers and validators** are both proof, and when both answer they must agree. After a
  step, an observer that reads a different value from the model fails the step:
  - if this step's validator LOOKED (anything but `outcome:`/`const:`), two looks disagree, and
    the variable is **unknown**;
  - otherwise the observer is the only one that looked, so its reading stands: the step's claim
    did not hold, or the step changed the variable without declaring it;
  - inside a walk, an observed change that the enclosing walk declares (from the value the
    variable had, to the value now seen) is that walk's claim, seen early. It is recorded, not
    failed;
  - a value the model does not know (unknown) is learned from the observer, but only when the
    observer READ a value. An observer that cannot tell which value it is returns `CANNOT_TELL`,
    and the variable stays unknown. It must never answer a guess, such as a lower bound: a guess
    would be stored as reality;
  - an observer that finds no value holding (`NONE_HOLDS`) makes the variable unknown, and fails
    the step.
- **Observers derived from validators**: `observe: {order: [clustered, installed, up, absent]}`
  asks each value's validator in that order, and the first that holds is the value. An earlier
  value that *cannot tell* stops the search: the observer cannot look, and the run warns and keeps
  the model's value. Only validators that look can be asked, so an `outcome:`/`const:` validator in
  the order is refused. The validators run outside any step, with the run's fixtures; a
  `{param.x}` comes from the run config.
- **Transitions**: what comes next, decided by how the step ENDED. Two forms:
  ```yaml
  on:
    verify: {failed: stop}             # by outcome only: continue | stop | {goto: id} | [steps]
    join:                              # rules on outcome AND the observed state; first match wins
      - when: {outcome: passed, state: {rig: clustered}}   # the marker that means "go on"
        then: continue
      - when: {outcome: failed, state: {rig: installed}}   # its claim did not hold: no cluster formed
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
  step    | requires (state in)      | outcome (event) | state after                               | next
  --------+--------------------------+-----------------+-------------------------------------------+------------------------------------
  join    | rig: installed|clustered | passed*         | rig: clustered                            | outcome passed rig=clustered -> continue; otherwise -> stop (failed)
          |                          | failed          | (kept: no end result declared)            | outcome failed rig=installed -> goto form; outcome failed -> run membership, then go on
          |                          | errored         | (kept: no end result declared)            | otherwise -> stop (failed)
          |                          | aborted         | (kept: no end result declared)            | stop (aborted)
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
  - insert: [pve-ping]           # an action this config defines (below)
    between: [join, golden-build]
  - skip: [cache-verify]         # one id or a list
  - replace: images              # one step out, these in, at its place
    by: [{action: images, with: {source: mirror}}]
  - extend: golden-build         # one step's walk action, for this walk only (below)
    steps:
      - {id: preflight, action: pve-ping, before: build}
```
A later edit may anchor on an id an earlier one made, and `on:`/`expect:` may name it. In a
config every name must be on the walk (an error otherwise); with steps given on the command line
an edit whose anchor is not in the run is dropped with a note. The edits are part of the step
source in `--plan`, the banner, the journal and the report:
`steps: plan of qdevice.yml (via x.yml); edits: +ping-after-join (again ping) after join, -cache-verify`.

**Placement.** `repeat:` and `insert:` take exactly one of:

| key | the new steps land | refused when |
|---|---|---|
| `after: <id>` | right after `<id>` | `<id>` is not on the walk |
| `before: <id>` | right before `<id>` | `<id>` is not on the walk |
| `between: [<a>, <b>]` | right before `<b>` | either id is not on the walk, or `<a>` does not come earlier than `<b>` |

`between:` does not need `<a>` and `<b>` to be neighbours: on `join, images, golden-build` the
step lands after `images`, right before `golden-build`. `<a>` is a check, not an anchor: it
states what you expect the walk to look like, so an inherited walk that has since been
reordered refuses the edit instead of silently placing the step somewhere else. The command
line's `--repeat`/`--insert` take `@after:` and `@before:` only.

**Extending one step.** `extend: <id>` with `steps:` changes the WALK action behind that step,
for this walk only; the repository's walk and every other step that uses it are untouched. The
inner steps follow the walk-action rules (below), by id:
- an inner step whose `id` the walk already has is MERGED into it, as a walk action's
  `extends:` merges: maps merged, lists and scalars replaced, a different kind key (`action:`,
  `playbook:`, ...) replaces the old one,
- a new `id` is appended at the end, or placed by one of `after:`, `before:` or
  `between: [<a>, <b>]`, which name inner ids of that walk, with the same rules as above,
- `after:`/`before:`/`between:` on an existing id is refused (move a step with `skip` +
  `insert`, not by merging).

The step keeps its id (`--from golden-build`, `on:` and `expect:` still name it); its action
becomes `<action>+<id>` (`golden-build+golden-build`) and its inner steps are named
`golden-build/preflight` as usual. `--plan` says `extended golden-build (1 inner step(s))`.
Extending a Python or shell action is refused: change its parameters with `replace` and
`with:`.

**Actions in a run config.** A run config may define walk actions of its own (`actions:`, the
same schema as `actions/*.yml`, below) and take another config's (`uses:`), so an edit can
insert a step the repository does not have:
```yaml
extends: p3-serial
uses: [checks]                   # checks.yml's actions: ONLY (not its plan, rig or edits)
actions:
  pve-ping:
    doc: every PVE node answers, before the golden image is built
    steps:
      - {id: ping, action: ping}
edit:
  - {insert: [pve-ping], between: [join, golden-build]}
```
- `actions:` are inherited through `extends:`; `uses:` configs are read in order. When two
  define the same name, the later one wins and the run says so (`action 'pve-ping': child.yml
  (own) replaces parent.yml`): the parent's first, then each `uses:` config, then the config's
  own.
- A config action may `extends:` a repository walk. It may not take a name the repository
  already uses, for a Python action or a walk alike (one name, one action): give it a new
  name.
- A config named in `uses:` or `extends:` is looked up beside the config, then in the configs
  directory; one that does not exist, or a loop, is refused.
- The actions are part of the config, so a run that joins a scenario from its snapshot has them
  too.

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
      state: {rig: up}           #   the OBSERVED state after the attempt; a failure's classified
                                 #   mode is that state: {rig: [quorum]} retries listed modes only
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
The run then starts from that same look. It does not look again, as long as the state that
look started from is still the state the run starts from; if the store changed in between, it
looks again. `run_start` in the journal records which one happened (`observed_before_plan`).

## Walk actions

An action can also be a **walk**: a list of inner steps, written in YAML, that the planner
compiles exactly like a plan. Every inner step gets what a plan step has — a guard
(`requires`), an effect (`produces`/`produces_on`), an expected outcome (`expect`), a
transition (`on:`), `retry`, `when`, `max_visits` — so a step that failed can be retried,
ignored or made to fail the walk, decided by the outcome and the state the runner actually
reached. Use a walk for COMPOSITION (run this playbook, then that one, on these hosts); keep
Python for questions and procedures that need logic (a probe that answers `member` /
`not-member`, a sweep of leftover machines).

A run config can define walks too (`actions:`, `uses:`; "Actions in a run config" above).

```yaml
# testrunner.yml: action_files: [actions/*.yml]      (or walks in its own `actions:`)
actions:
  install:
    doc: install the product on the nodes
    requires: {rig: [up, installed]}
    produces: {rig: {up: installed}}
    params: {limit: "{live}", log: install.log}       # defaults; a caller's `with:` wins
    steps:
      - id: install
        playbook: playbooks/install.yml               # a kind ext.ansible registers
        limit: "{param.limit}"
        extra: {encrypt: "true"}
        rules: {file: drive/install.json, CLUSTER: "{inventory:{rig:founder}/cluster_name}"}
        log: "{param.log}"

  reprovision:
    extends: install                                  # its steps, merged by id
    params: {victim: "{role:reprovision_victim}"}
    steps:
      - {id: install, limit: ["{param.victim}"], extra: {force: "true"}, log: reprovision.log}
      - {id: unlock, action: reboot, with: {limit: ["{param.victim}"]}}

  foreign-tang:
    outcomes: [refused]
    steps:
      - {id: arrange, run: ["scripts/bind-foreign.sh"]}
      - id: provoke
        playbook: playbooks/encrypt.yml
        outcomes:                                     # classify what the command did
          refused: {exit: nonzero, output: ["does not declare", "{param.url}"]}
        expect: refused                               # anything else fails the walk ...
      - {id: restore, run: ["scripts/restore.sh"], always: true}   # ... after this ran
      - {id: verify, action: verify, on: {failed: [collect-logs, fail]}}
```

**Inner steps** are exactly one of: `action:` (any action, with `with:`; its contract is that
action's unless the step declares its own), `run:` (a command: argv or a string, `env`, `cwd`,
`rules`, `timeout`, `log`), `refuse:` (fail with a message — with `when:`, a stage refused on a rig it
does not belong to), or a kind an extension registers — `playbook:` (`ext.ansible`),
`vagrant: up|destroy` (`ext.vagrant`) — or your own `@step_kind`.

**Outcomes from evidence.** A command step passes on exit 0 and fails otherwise, unless an
`outcomes:` entry holds first: `exit` (a number, a list, `zero`, `nonzero`), `output` (a
regular expression or a list of them, all must match; a `{reference}` in it matches
literally), `timed_out`.

**Reactions** (`on: {<outcome>: ...}`, or transition rules, as in a plan): `continue`,
`fail` (the walk ends with that outcome), `ignore` (recorded, the walk goes on), `skip` (the
walk ends skipped), `{end: <outcome>}` (one the walk declares in `outcomes:`), `{goto: <id>}`,
or a step list, which may end in one of those words (`[collect-logs, fail]`). `always: true`
runs a step whenever the walk leaves past it, however it ended (not after an operator abort,
which leaves the system as it is). An `ignore` is safe by construction: the next step's
`requires` still guards it, so ignoring a failure a later step depends on is refused before
the run starts.

**Templates** are `{name}` references in values, nothing more: `{param.x}`, `{env.X}`,
`{run.root}`, `{run.id}`, `{step.id}`, `{state.var}`, and RESOLVERS — `ext.rig` registers
`{live}`, `{role:<name>}`, `{roles:<name>}`, `{rig:<attribute>}`,
`{inventory:<host>/<expression>}`, `{peer_of:<node>}`; register your own with `@resolver`. A
value that IS one reference keeps its type (a list stays a list). An unknown name is refused
when the walks load.

**The contract is derived**: `requires` from the first inner step, `readonly` when every
inner step is; the static check composes the walk's effect from its inner steps and checks
inside it. Inner steps are named `<walk>/<step>` in the trace, the journal, the reports and
`--plan --matrix`; an `edit:` (or `--skip`) may retry or skip one, and `--from` / `--resume`
start inside a walk (`--resume` after `addnode/join` failed does not rebuild the node).

## Dry run

`testrunner --dry-run [steps]` walks the plan against the real config and the observed state,
judging every `when`, `requires` and transition as a run would, without changing anything:

- what only LOOKS runs: the observers (at the start), read-only walk kinds (`require_hosts`,
  `require_nodes`) and `refuse`, so a dry run shows the refusals a run would hit;
- the commands the rest would run are printed, built by the same code that runs them:
  `playbook:` with its argv, the answers its rules would type and its log; `vagrant:`,
  `machines:`, `run:` and shell actions; a kind of your own describes itself with
  `@step_kind(describe=fn)`;
- a Python action is NOT run: it is marked "not run" and assumed to end with its expected
  outcome, unless `@action(dry=fn)` describes what it would do;
- a resolver that needs something absent (no rig: `{live}`) is shown unrendered, and said so;
- every step is assumed to end with its expected outcome; `--dry-outcome STEP=OUTCOME`
  (repeatable; an inner step too, `walk/step`) explores another branch. A retry is
  described, not simulated.

```
$ testrunner --dry-run addnode
==== [1] addnode (dry) ====
==== [1.3] addnode/install (dry) ====
     (dry) would run: ansible-playbook playbooks/install-proxmox.yml -i .../inventory --limit pve-val-4 -e ...   (in .../ansible)
     (dry)   answers 'anything else to abort' -> 'YES\r'
     (dry)   log addnode-install-pve-val-4.log
```

A dry run takes no lock, claims no scenario, writes no state and no journal, and does not
re-read reality after a step (nothing changed). Its trace is `testrunner-dry-<id>.log`, never
`testrunner-last.log`, so `--resume` never resumes from it. `run_start` hooks run (a subscriber's
refusal of the run shows); `run.dry` tells them, and your fixtures, not to change anything. Step
hooks and `run_end` hooks do not run. Exit 0 when the walk goes through as assumed, 1 when a
guard, a refusal or a transition stops it.

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

**An interrupted run is never a pass.** On Ctrl-C (SIGINT), or when the run's task is
cancelled:
- every step still open, a walk and its inner step alike, ends `aborted` in the journal,
  innermost first;
- the run ends failed, "interrupted during <step>", and exits 130;
- `release_when` is not judged: the stopped run left the system wherever it was, so the
  scenario stays until a run reaches the state, or `--release`.

The operator's own `abort` (typed while the run waits) ends the step `aborted` too, and the
run fails.

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

Both formats also carry the **state**:
- **State changes**: each variable at the start and at the end, then every change a step
  claimed, with the validator (or observer) that confirmed it and its reason. Values checked and
  kept after a step that claimed nothing are listed too, and a value that became **unknown** is
  marked, with what each validator answered.
- **State machine**: the machine as declared when the run started. That is, per variable: how it
  is read back, each value with its criterion and validator, and every transition.
- **State/event matrix**: per step and outcome, the state it needs, the state it leaves, and
  what comes next.

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
testrunner [steps] -- ARGS...    everything after the first `--` is for the steps' tool
```
Exit codes: 0 passed, 1 failed, 2 refused (config, plan or lock), 130 interrupted (Ctrl-C:
journalled as failed, see "The scenario lock").

`TESTRUNNER_PROG` names the command in the help and in the hints testrunner prints (a
project's wrapper sets it, above). Unset, it is `testrunner`.

### Arguments for the steps: `--`

```
testrunner up..verify -- -vvv --limit node-1 -e debug=true
```
testrunner does not read anything after the first `--`. It hands those arguments to the steps.
An action gets them as `proc.forwarded` (or `run.forwarded`), and a shell action's command gets
them as `$TR_FORWARDED` (JSON). The banner, `--plan`, the journal and the report show them, so a
report of a narrowed run says that it was narrowed. `--status`, `--list` and the other modes
that run no step refuse them.

`ext.ansible` gives them to every playbook a step runs. They come last, so a forwarded `-e` beats
the step's own. Ad-hoc calls and inventory probes never get them. A forwarded `--limit`
**narrows** the step's own limit and never replaces it:
- ansible keeps only the last `--limit` it is given, so appending one would undo a step's own
  targeting (a victim, the live nodes).
- ansible itself works out the intersection (`ansible <step limit> --list-hosts --limit <yours>`).
  If no hosts are left, the step refuses (`errored`).
- A playbook that a step runs without a limit (one that targets its own `hosts:`, such as a key
  server or the founder) runs un-narrowed.

These are refused, because they make ansible exit 0 having done nothing or change what is
tested: `-C/--check`, `--syntax-check`, `--list-hosts`, `--list-tasks`, `--list-tags` and
`-i/--inventory`. The refusal happens when the step builds its controller, before the step does
anything. Prefixes (`--lim`) and bundles (`-vl node-1`) are read the way ansible reads them.

## Extensions

`testrunner.ext` holds the tooling a subscriber builds its actions on. A subscriber imports
what it needs:
- `ext.ansible`: playbooks and inventory queries, refusing an empty scope; it takes the
  run's arguments after `--` (above).
- `ext.vagrant`: `vagrant status` and `up`, and what exists.
- `ext.console`: serial console capture.
- `ext.rig`: a whole vagrant + ansible test rig.

An extension's settings live in the runner config under `ext: {<name>: {...}}`. Enabling one
there also loads the inner-step kinds and resolvers it registers for walk actions:
`ext.ansible` -> `playbook:`, `require_hosts: <group>` (refuse an empty group), `ext.vagrant` -> `vagrant:`, `ext.rig` -> `{live}` `{role:x}`
`{roles:x}` `{rig:x}` `{inventory:h/x}` `{peer_of:n}` (`ext: {ansible: {}, vagrant: {}}`).

**`ext.rig`** is what every such rig is built from, whatever it tests:
- the run config and the environment it becomes (`rig.config`, a schema engine: the rig's
  `<configs>/schema.yml` declares its keys, types, allowed values, rules and environment names);
- the node catalogue (`rig.nodes`);
- the generated `ansible.cfg` and harness key;
- scope guards: only nodes that EXIST, and never an empty scope;
- the `rig` observer, the purge of stale per-node state, drive rules for the playbooks' prompts,
  the console capture and the sweep for leftover libvirt domains;
- the generic **regions**, as validators read once per look (`ext/rig/regions.py`):
  - `{python: vms}`: `absent`, `running`, `halted` or `partial`. It is judged against the
    declared data `expected` when there is any, else the catalogue's cluster nodes. `running`
    means every expected machine runs **and** answers TCP/22; it never pings.
  - `{python: support}`: the same four values as `vms` (`absent`, `up`, `halted`, `partial`),
    for the scenario's other machines.

  What runs *on* the machines is a system under test, and its probes live with it, e.g. Proxmox
  VE's in [proxmox-testrunner-ext](https://github.com/alc-kit/proxmox-testrunner-ext).

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

## The Vagrant layer

`ext.rig` ships the DEFAULT Vagrantfile for a vagrant + libvirt rig, as a Ruby library
(`src/testrunner/ext/rig/vagrant/rig.rb`) that a rig's own `Vagrantfile` loads. Vagrant only
ever evaluates the rig's `Vagrantfile`, so the rig chooses how much of it to use:

```ruby
# (a) the default, as is
require_relative "testrunner/src/testrunner/ext/rig/vagrant/rig"
RigVagrant.configure(root: __dir__, network: "my-rig-net")

# (b) the default, with hooks for what this rig does differently
RigVagrant.configure(root: __dir__) do |rig|
  rig.networks { |node, name, spec| ... }                  # replaces the default network
  rig.machines { |nodes| ... }                             # filter or change the machine table
  rig.autostart { |name, spec| ... }                       # default: not for catalogue `late:`
  rig.domain { |lv, name, spec| ... }                      # after the default libvirt settings
  rig.provision(:after_names) { |node, name, spec| ... }   # or :after_key
end
```
(c) A rig may instead write a `Vagrantfile` of its own and never require the library. (d) A rig
may use no vagrant at all: its `Rig` subclass returns its own machine provider from
`machines()` (`ext/machines.py`: `status`, `up`, `destroy`), and ext.rig's guards, its observer
and the `machines:` walk kind use that instead.

The default builds every machine of the node catalogue (`bin/rig-nodes json`) with its sizes
(`overrides: {field: key}` → `<PREFIX>_<KEY>`), libvirt on the system daemon, the NIC model /
q35 / virtio `ctrl_vlan` and an emulated IPMI BMC on `kind: pve` machines, a provisioner for
predictable NIC names and the serial console, an apt cache (a squid box from
`provision/squid.conf`, every other box pointed at it; `<CACHE_PREFIX>_APT_*`) and root login
with `secrets/harness_key.pub`. `<PREFIX>` is testrunner.yml's `ext.rig.prefix`. A missing
`squid.conf` warns and disables the cache: every vagrant command evaluates the Vagrantfile, a
teardown too, so nothing a teardown does not need may fail it.

`ext/rig/vagrant/record.rb` loads a Vagrantfile with a stub `Vagrant` that records every
configuration call as JSON, and `check.py` compares the machines with the catalogue — a
device-free test of any rig's Vagrantfile.

## The runner config

`testrunner.yml` (or `.toml`) at the subscriber root:
```yaml
modules: [actions/]            # python exports
pythonpath: [lib/]             # directories those modules import from
actions: {...}                 # shell exports, and walk actions (an entry with steps:)
action_files: [actions/*.yml]  # more walk actions, one `actions:` mapping per file
state_dir: state               # the subscriber's state (journal, state, scenario, logs)
states: {...}                  # the state machine: values, criteria, validators, transitions
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
