# Extending testrunner: building a test framework on it

testrunner is a small engine. A test framework is what you build on it: your actions, your
state model, your run configs, and the extensions they share. This page says where each kind of
code goes, how to design the part that matters most (the state model), and how to write an
extension other projects can reuse. `examples/toy/` is the smallest complete subscriber;
`examples/bare-metal/` drives a real server (see `docs/real-hardware.md`); `testrunner.ext.rig`
is a full extension, ported out of two production test harnesses.

## Three layers

| layer | what lives there | who owns it |
|---|---|---|
| **core** (`testrunner`) | the state machine, plans and walk edits, retry, transitions, the static check, prompts, the scenario lock, NOLOG, the trace, the journal, reports | this repository |
| **extensions** (`testrunner.ext.*`, or your own package) | tooling several frameworks share: `ansible`, `vagrant`, `console`, `rig` | whoever needs it twice |
| **subscriber** (your repository) | `testrunner.yml`, actions, fixtures, observers, hooks, run configs | you |

testrunner keeps no state of its own: the journal, the state and the scenario lock live in the
subscriber's `state_dir`. A subscriber pulls testrunner in as a git subtree and starts it with
`testrunner/bin/testrunner --root <its directory>`, usually from a short `run.sh` that also sets
`TESTRUNNER_PROG=./run.sh`, so the help and the hints name the command its operators type (README,
"Use it in a project").

## Where does my code go?

| you need to ... | write a ... | it is |
|---|---|---|
| DO something to the system (install, power on, break a disk) | **action** | `@action(...)` in a module listed in `modules:`, or a shell export under `actions:` |
| give steps something they NEED (a client, a connection, credentials) | **fixture** | `@fixture(scope="run"\|"step")`; set up on first use, torn down at the scope's end, also after a failure |
| READ what the system IS (power, version, membership) | **observer** | `@observer("<state var>")`; asked before the plan is judged, at run start, after every state-changing step. Or `observe: {order: [...]}`, derived from the validators |
| CONFIRM that a claimed value holds (a step said "installed": is it?) | **validator** | `validate:` per value in `states:` — `{run: ...}`, `{playbook: ...}`, `{python: <name>}` (an `@validator`), or as weak as `{outcome: true}`; an extension adds kinds with `@validator_kind` |
| act AROUND steps (refuse a run, adjust the config, record metrics) | **hook** | `@hook("run_config"\|"run_start"\|"step_start"\|"step_end"\|"run_end")` |
| run one thing in several orders, with other parameters | **run config** | `configs/<name>.yml`: `extends:`, `plan:`, `edit:`, `actions:`, `uses:`, parameters |
| share any of the above between frameworks | **extension** | a package; settings under `ext: {<name>: ...}` (below) |

An action's contract is declared, not coded: `requires` (the states it may start from),
`produces` (the state it leaves on `passed`), `produces_on` (per other outcome), `outcomes` (named
ones beyond passed/failed/errored/aborted/skipped), `readonly` (it only looks). The runner checks the
contract, and the static check simulates it over every branch of the plan before anything runs.
`docs/writing-actions.md` walks through writing one.

## The state model: design it first

The state model is the framework's spine. Everything else is judged against it.

1. **A state variable is something an observer can READ.** `power: [off, on]` (the BMC knows),
   `rig: [absent, up, installed, clustered]` (vagrant and the cluster know). A variable nobody can
   read is a guess. Leave out `initial` when only the system knows the value: the runner then asks
   the observer before it judges the plan.
2. **Values form a lifecycle,** and actions move along it. Use a transition map,
   `produces: {rig: {up: installed}}`, when re-running a step on a system that is further along must
   not move it backwards.
3. **Every claim is confirmed; nothing is assumed.** Declare every transition in `states:` (the
   actions' `produces` must agree), and give every value a validator. A claim is committed only
   when its validator holds. After a step that claims nothing, or whose claim did not hold, the
   value before and every reachable value are asked, and exactly one may hold; otherwise the
   variable is unknown and the step fails. The observers read again after every state-changing
   step: if they contradict the model, the step fails. Choose each validator's strength
   deliberately. `{outcome: true}` writes down "the step passed, so it holds"; a probe looks.
4. **Make the observer the judge where "done" is a state.** `expect: {power-on: {state: {power: on}}}`
   turns "the command exited 0" into "the chassis is on". Without it, a power-on the BMC accepted but
   never carried out would pass.
5. **Route on the state, not only on the outcome.** Transition rules (README, "State machine")
   decide the next step from the outcome AND the observed state. `--plan --matrix` prints the
   whole state/event matrix; review it like code.

## Composing runs without code

A new test scenario is usually a run config, not new code:

```yaml
extends: qdevice                     # inherit a whole plan
edit:
  - {repeat: ping, after: join}      # run a step again later
  - {insert: [membership], after: qdevice}
  - {insert: [pve-ping], between: [join, golden-build]}   # an action the config defines
  - extend: golden-build             # add an inner step to one step's walk, for this walk only
    steps: [{id: preflight, action: pve-ping, before: build}]
  - {skip: [alma]}
  - retry: install                   # retry, only when the evidence says it can help
    times: 2
    if: {output: "Could not get lock"}
  - transition: join                 # re-route on outcome + observed state
    rules:
      - {when: {outcome: passed, state: {rig: clustered}}, then: continue}
      - {when: {outcome: passed, state: {rig: installed}}, then: {goto: form}}
```
`pve-ping` above is not in the repository: a config may define walk actions (`actions:`) or
take another config's (`uses: [checks]`). The README's "Editing a walk" has the placement rules
(`after:`/`before:`/`between:`) and what `extend:` merges.

A negative control is an `expect` in the plan: `expect: {verify: {outcome: failed}}` passes the run
only if `verify` fails.

The README's "Plans", "Editing a walk" and "State machine" sections have the full syntax.
`--from STEP`, `--resume` and the one-run flags (`--skip`, `--repeat`, `--insert`, `--retry`) do the
same from the command line.

## Writing an extension

An extension is tooling two frameworks would otherwise each carry. `testrunner.ext.rig` was
ported out of two harnesses that held the same 900 lines twice. These rules come from that port.

1. **Settings in the runner config, under your extension's name:** `ext: {<name>: {...}}`.
   `RunnerConfig.ext[<name>]` hands you the mapping. Validate it yourself and refuse unknown
   keys: a typo must not silently become a default. `testrunner/ext/rig/settings.py` is the
   pattern: a dataclass, `settings_from(root, spec)`, `load_settings(root)`.
2. **Nothing of any subscriber in it:** no host names, node names, addresses, products or
   customers. What differs between subscribers is a setting. `ext.rig` turned the literal machine
   kind `pve` into `cluster_kind`, and derives everything from it, down to the names of the shell
   variables it exports. This repository is public; `tools/content-guard` refuses private
   addresses and internal host names on every commit.
3. **No module-level state about a subscriber.** Pass the root, or carry it in a loaded object:
   `ext.rig`'s schema carries `_root`. One process may serve several subscribers (testrunner's own
   tests do).
4. **A command line that reads the same settings.** A tool a Vagrantfile or a shell calls runs as
   `python -m testrunner.ext.<name>.<tool> --root <subscriber> ...` and loads the same `ext:`
   settings as the library. One source of truth, no second config file. A subscriber keeps a
   three-line `bin/` wrapper, so its scripts need not change.
5. **Import lazily if your modules are also commands.** If the package's `__init__` imports a
   module that is then run with `python -m`, Python warns on stderr. That warning landed inside a
   Vagrantfile's JSON (measured). `testrunner/ext/rig/__init__.py` resolves its names on first use
   instead.
6. **Fail with a reason:** `raise OutcomeError(FAILED, "<what, and what to do>")`.
   - Refuse an empty scope: an ansible `--limit ''` matches nothing and exits 0, so a broken
     inventory would pass every step.
   - Log every decision at INFO with its evidence; that is what the trace is for.
7. **Test it on invented data, and pair every rule with the input it must refuse.**
   - Mutation controls: break one rule at a time, and the suite must fail each time.
   - Count a control as caught only when the tests ran and named failures. A control that ran no
     tests once reported "caught".
8. **When you port existing tooling, prove equivalence.** Run the old tooling's own test suites,
   unchanged, against the new code through the wrappers. `ext.rig` passed the harnesses' five
   suites (161 tests) that way before either harness switched over.

Shape of a small extension:

```
src/testrunner/ext/fleet/
  __init__.py      # docstring: what it is, its settings, its commands; lazy names
  settings.py      # FleetSettings, settings_from(), load_settings()
  core.py          # the library: classes and functions your actions use
  inventory.py     # a tool, also `python -m testrunner.ext.fleet.inventory --root R`
tests/test_ext_fleet.py
```

A subscriber uses it like this:

```python
from testrunner import fixture
from testrunner.ext.fleet import Fleet, load_settings

@fixture(scope="run")
def fleet(run):
    return Fleet(load_settings(run.root))
```

An extension that only one framework needs does not belong here: keep it in that framework's
repository, on its `pythonpath:`, until a second one needs it.

## Checklist for a new framework

- [ ] `testrunner.yml`: `modules`, `states` (each one readable by an observer), `paths`,
      `configs`, `state_dir`, `scenario.release_when` (on an OBSERVED state).
- [ ] Every transition declared under `states:` and claimed by its action (they must agree).
- [ ] A validator for every value a transition touches, each proven both ways: it holds in the
      state it checks, and does not hold one state earlier.
- [ ] An observer for every changing state variable (or `observe: {order}`, or `model_only: true`).
- [ ] Actions with declared contracts; read-only ones marked `readonly=True`.
- [ ] `expect` wherever "done" is a state, not an exit code.
- [ ] Destructive actions behind a prompt that a drive rule answers with the target's name.
- [ ] Credentials marked NOLOG in the run config.
- [ ] `--plan --matrix` reviewed; a negative control (an `expect` of failure) for each guard.
- [ ] A smoke test of the whole walk against stub tools, plus its own negative control.
