"""Python exports for the toy service."""
from pathlib import Path

from testrunner import Outcome, action, fixture, hook, observer


@fixture(scope="run")
def sandbox(store, config):
    """Where the pretend service lives — inside the subscriber's state directory."""
    return store.path(config["params"].get("sandbox_dir", "sandbox"))


@observer("service")
def service_state(sandbox: Path):
    f = sandbox / "service"
    return f.read_text().strip() if f.exists() else "absent"


@action(requires={"service": "installed"}, produces={"service": "running"})
async def start(sandbox: Path, proc):
    """start the service"""
    r = await proc.run(["bash", "-c", f"printf running > '{sandbox}/service'; printf '\\033[32mstarted\\033[0m\\n'"],
                       log="start.log")
    return r.ok


@action(requires={"service": "running"}, produces={"data": "seeded"}, outcomes=["degraded"])
async def seed(sandbox: Path, params):
    """put data into the service"""
    (sandbox / "data").write_text("x" * int(params.get("rows", 3)))
    if params.get("degrade"):
        return Outcome("degraded", "seeded, but slowly")


@action(requires={"service": "running"})
def verify(sandbox: Path, params):
    """assert the data is there (sync: runs in a thread)"""
    have = len((sandbox / "data").read_text()) if (sandbox / "data").exists() else 0
    want = int(params.get("rows", 3))
    assert have == want, f"{have} rows, want {want}"


@action(readonly=True)
def peek(sandbox: Path, config):
    """read-only: print what the service holds (may run beside a state-changing run)"""
    print(f"service: {service_state(sandbox)}, rows: {config['params'].get('rows')}")


@action(produces={"data": "seeded"})
async def repair(sandbox: Path):
    """the detour taken when seeding degrades"""
    (sandbox / "repaired").write_text("yes")


@hook("run_end")
def remember(run, result):
    run.store.kv("toy").put("last", {"passed": result.passed, "steps": len(result.steps)})
