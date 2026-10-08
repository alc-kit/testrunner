"""Plans: compilation, ranges, detours, branches, static validation — each rule also
proven against an input that must be refused."""
import pytest

from testrunner.config import ConfigError, load_run_config, load_runner_config
from testrunner.discovery import collect
from testrunner.planner import Compiler, Walker, initial_state, simulate

RUNNER = """
modules: [mod.py]
states:
  s: {values: [a, b, c], initial: a}
paths:
  main: [one, two, three]
  outer: [main, four]
default_path: main
"""
MOD = """
from testrunner import action, Outcome
@action(produces={"s": "b"})
def one(): pass
@action(requires={"s": "b"}, produces={"s": "c"}, outcomes=["odd"])
def two(): pass
@action(requires={"s": "c"})
def three(): pass
@action()
def four(): pass
@action(produces={"s": "a"})
def reset(): pass
"""


def build(sub, plan_yaml="", runner=RUNNER):
    sub.write("testrunner.yml", runner)
    sub.write("mod.py", MOD)
    if plan_yaml:
        sub.write("configs/default.yml", plan_yaml)
    rc = load_runner_config(sub.root / "testrunner.yml")
    reg = collect(rc)
    return rc, reg, load_run_config(rc)


def compiled(sub, plan_yaml, cli=None):
    rc, reg, run = build(sub, plan_yaml)
    prog = Compiler(rc, reg).compile(run.plan, cli)
    return rc, reg, run, prog


def check(rc, reg, run, prog):
    return simulate(rc, reg, prog, initial_state(rc, None), run.data)


def test_path_and_nested_paths_expand(sub):
    *_, prog = compiled(sub, "plan: {path: outer}")
    assert [s.id for s in prog.main] == ["one", "two", "three", "four"]


@pytest.mark.parametrize("rng,want", [("one..two", ["one", "two"]), ("..two", ["one", "two"]),
                                      ("two..", ["two", "three"]), ("main:two..three", ["two", "three"])])
def test_ranges(sub, rng, want):
    *_, prog = compiled(sub, "", cli=[rng])
    assert [s.id for s in prog.main] == want


@pytest.mark.parametrize("rng", ["three..one", "one..nine", "nine.."])
def test_bad_ranges_refused(sub, rng):
    with pytest.raises(ConfigError):
        compiled(sub, "", cli=[rng])


def test_repeated_action_gets_distinct_ids(sub):
    *_, prog = compiled(sub, "plan: {path: [one, two, three, reset, one, {action: two, id: second-two}]}")
    assert [s.id for s in prog.main] == ["one", "two", "three", "reset", "one#2", "second-two"]


def test_detour_spliced_after_and_before(sub):
    *_, prog = compiled(sub, """
        plan:
          path: main
          detours:
            - {after: one, steps: [four]}
            - {before: one, steps: [{action: four, id: first}]}
        """)
    assert [s.id for s in prog.main] == ["first", "one", "four", "two", "three"]


def test_detour_to_missing_anchor_refused(sub):
    with pytest.raises(ConfigError, match="no step 'nine'"):
        compiled(sub, "plan: {path: main, detours: [{after: nine, steps: [four]}]}")


def test_yaml_on_key_is_read_as_on(sub):
    *_, prog = compiled(sub, """
        plan:
          path: main
          on:
            two: {odd: stop}
        """)
    assert prog.reactions["two"]["odd"].kind == "stop"


def test_reaction_on_undeclared_outcome_refused(sub):
    with pytest.raises(ConfigError, match="never ends 'weird'"):
        compiled(sub, "plan: {path: main, 'on': {two: {weird: stop}}}")


def test_goto_must_target_the_main_path(sub):
    with pytest.raises(ConfigError, match="goto 'nine'"):
        compiled(sub, "plan: {path: main, 'on': {two: {odd: {goto: nine}}}}")


def test_static_validation_passes_a_good_plan(sub):
    v = check(*compiled(sub, "plan: {path: main}"))
    assert v.ok and v.walk == ["one", "two", "three"]


def test_static_validation_catches_a_broken_requires(sub):
    # negative control: `three` needs s=c, but `reset` leaves s=a
    v = check(*compiled(sub, "plan: {path: [one, two, reset, three]}"))
    assert not v.ok and "three: requires" in v.errors[0]


def test_static_validation_catches_a_bad_detour(sub):
    v = check(*compiled(sub, "plan: {path: main, detours: [{after: two, steps: [reset]}]}"))
    assert not v.ok


def test_static_validation_explores_branches(sub):
    # on `odd`, two produces nothing -> s stays b -> three's requires fails on that branch only
    v = check(*compiled(sub, "plan: {path: main, 'on': {two: {odd: continue}}}"))
    assert not v.ok and "three" in v.errors[0]
    # ...and a reaction that repairs the branch makes it valid
    v = check(*compiled(sub, "plan: {path: main, 'on': {two: {odd: [{action: two, id: retry-two}]}}}"))
    assert v.ok, v.errors


def test_expect_state_contradicting_produces_refused(sub):
    v = check(*compiled(sub, "plan: {path: main, expect: {two: {state: {s: a}}}}"))
    assert not v.ok and "expect.state" in v.errors[0]


def test_goto_loop_bounded_by_max_visits(sub):
    v = check(*compiled(sub, "plan: {path: main, 'on': {two: {odd: {goto: two}}}}"))
    assert any("max_visits" in w for w in v.warnings)


def test_when_skips_statically(sub):
    rc, reg, run, prog = compiled(sub, """
        params: {mode: x}
        plan:
          path: [one, {action: reset, when: {params.mode: y}}, two]
        """)
    v = check(rc, reg, run, prog)
    assert v.ok and v.walk[1].startswith("(reset: skipped")


def test_walker_transitions(sub):
    *_, prog = compiled(sub, """
        plan:
          path: main
          on:
            two: {odd: [four], failed: stop}
          expect:
            three: {outcome: failed}
        """)
    w = Walker(prog)
    cur = w.start()
    t = w.next(cur, "passed")
    assert w.current(t.cursor).id == "two" and t.verdict == "ok"
    t2 = w.next(t.cursor, "odd")
    assert w.current(t2.cursor).id == "four" and t2.verdict == "diverted"
    t3 = w.next(t2.cursor, "passed")        # reaction segment done -> back after `two`
    assert w.current(t3.cursor).id == "three"
    assert w.next(t3.cursor, "failed").verdict == "ok"           # expected failure
    assert w.next(t3.cursor, "passed").verdict == "failed"       # unexpected pass
    assert w.next(t.cursor, "failed").verdict == "failed"        # stop on unexpected


def test_unknown_state_value_in_action_refused(sub):
    sub.write("testrunner.yml", RUNNER)
    sub.write("mod.py", "from testrunner import action\n@action(produces={'s': 'z'})\ndef bad(): pass\n")
    with pytest.raises(ConfigError, match="'z' is not one of"):
        collect(load_runner_config(sub.root / "testrunner.yml"))


def test_range_without_default_path_refused(sub):
    sub.write("testrunner.yml", RUNNER.replace("default_path: main\n", ""))
    sub.write("mod.py", MOD)
    rc = load_runner_config(sub.root / "testrunner.yml")
    with pytest.raises(ConfigError, match="needs a path"):
        Compiler(rc, collect(rc)).compile(None, ["one..two"])
