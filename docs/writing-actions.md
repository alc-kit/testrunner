# Writing a new action

An action is one step a plan can take: install something, break something, verify
something. This guide takes one from idea to proven. `examples/toy/` has a working
example of every part.

## 1. Write the contract first

Before you write any code, answer four questions. They become the decorator's arguments.

| question | argument | example |
|---|---|---|
| What must be true before it can run? | `requires` | `{"cluster": ["formed", "joined"]}` |
| What is true after it passes? | `produces` | `{"cluster": {"formed": "joined"}}` (a transition map: other values are kept) |
| Can it end some way other than pass or fail, in a way a plan may want to react to? | `outcomes` | `["refused", "degraded"]` |
| Does it only look? Must its output stay out of the logs? | `readonly`, `nolog` | `readonly=True` |

If the contract needs a state value that doesn't exist yet, add it to `states:` in
`testrunner.yml` first. A value nobody declared is refused when the actions are loaded.

## 2. Write it

Write it in Python (preferred, async where it waits on anything):
```python
# actions/rebalance.py
from testrunner import action, Outcome

@action(requires={"cluster": "joined"}, produces={"data": "balanced"}, outcomes=["refused"])
async def rebalance(proc, params, step):
    """move data until every node holds its share"""
    r = await proc.run(["ansible-playbook", "rebalance.yml", "-e", f"limit={params['nodes']}"],
                       log="rebalance.log", rules="rules/rebalance.yml", timeout=3600)
    if "pre-flight refused" in r.text:          # r.text: the output without colour codes
        return Outcome("refused", "pre-flight said no")
    return r.ok
```
Or wrap an existing script, with the same contract in `testrunner.yml`:
```yaml
actions:
  rebalance:
    run: ["scripts/rebalance.sh", "{nodes}"]
    rules: rules/rebalance.yml
    exit_codes: {4: refused}
    requires: {cluster: joined}
    produces: {data: balanced}
    outcomes: [refused]
```
Rules of thumb:
- **Let a fixture own repeated setup.** If two actions need the same client, inventory or
  scope check, write a `@fixture` (with `yield` for cleanup) and take it as a parameter.
  A rule written once in a fixture can't be forgotten in the next action.
- **Answer prompts with rules, not sleeps.** Keep `nolog: true` on any rule that types a secret.
- **Run long side jobs with `step.spawn()`**, for example a probe that must keep running
  while the action works. Use `wait=True, fatal=True` if its failure should fail the step.
- **Return outcomes the plan can react to.** Raise only for real failures.

## 3. Put it on a path

You can add it to a named path in `testrunner.yml`, where it runs every time:
```yaml
paths:
  lifecycle: [..., join, rebalance, verify]
```
Or add it to one run config only, as a detour, a branch or a condition:
```yaml
plan:
  path: lifecycle
  detours: [{after: join, steps: [rebalance]}]
  on: {rebalance: {refused: [collect-diagnostics, stop]}}
  expect: {rebalance: {state: {data: balanced}}}
```

## 4. Prove it

1. **`testrunner --plan`** for every run config that reaches the action. Static validation
   checks the contract against every walk. An error here means the contract and the path
   disagree, so fix one of them before you run anything.
2. **A negative control.** Write the input the action *must* refuse or fail on, and run it
   with `expect: {<step>: {outcome: failed}}`. A check that has never failed proves nothing.
3. **A real run** of the shortest range that reaches it (`testrunner join..rebalance`),
   then the full plan.

## 5. Observe what you can

If you can read the state the action changes from the real system, export an
`@observer` for it. The runner then checks `expect` against the system itself rather than
against what the action claimed, and a resumed run starts from the truth.
