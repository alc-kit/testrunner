"""One server through its BMC and SSH. Replace the commands with your platform's
(Redfish, a vendor CLI, a rescue system): the shape stays the same."""
from testrunner import Outcome, action, fixture, observer
from testrunner.api import FAILED, OutcomeError
from testrunner.proc import Proc


@fixture(scope="run")
def shell(run, store):
    """A command runner for the observers: they look at the machine outside any step, where
    the per-step `proc` does not exist. Masks the run's NOLOG values like `proc` does."""
    return Proc(run.root, store.path("logs", "observe.log").parent, secrets=run.secrets)


@fixture(scope="run")
def bmc(config):
    """ipmitool against the BMC; the password travels in the environment (-E), never in argv.
    It is NOLOG in the config, so it is masked wherever it would be shown."""
    p = config["params"]
    return {"argv": ["ipmitool", "-I", "lanplus", "-H", p["bmc"], "-U", p["bmc_user"], "-E"],
            "env": {"IPMI_PASSWORD": p["bmc_password"]}}


@observer("power")
async def power(bmc, shell):
    r = await shell.run([*bmc["argv"], "chassis", "power", "status"], env=bmc["env"], echo=False)
    if r.returncode != 0:
        raise OutcomeError(FAILED, f"the BMC did not answer (exit {r.returncode})")
    return "on" if "is on" in r.text else "off"


@observer("os")
async def os_state(config, shell):
    host = config["params"]["host"]
    r = await shell.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{host}",
                         "cat /etc/os-release"], echo=False)
    return "installed" if r.returncode == 0 and "ID=" in r.text else "absent"


@action(readonly=True)
async def survey(bmc, proc):
    """read-only: what the BMC reports (sensors, event log); changes nothing"""
    r = await proc.run([*bmc["argv"], "sdr", "list"], env=bmc["env"], log="survey.log")
    return r.ok


@action(produces={"power": "on"})
async def power_on(bmc, proc):
    """power the chassis on (the observer, not this step, says whether it is)"""
    r = await proc.run([*bmc["argv"], "chassis", "power", "on"], env=bmc["env"])
    return r.ok


@action(requires={"power": "on"}, produces={"os": "installed"}, outcomes=["refused"])
async def reinstall(params, proc):
    """DESTRUCTIVE: wipe and reinstall the OS. The script asks the operator to type the node's
    name; the drive rule answers with the name from the config, so a run against the wrong
    machine stops at the prompt instead of wiping it."""
    node = params["node"]
    r = await proc.run(["bash", "scripts/reinstall.sh", params["host"], node], log="reinstall.log",
                       rules=[{"expect": rf"type the node name \({node}\) to wipe it:", "send": f"{node}\r"}],
                       timeout=3600)
    if r.unanswered is not None:
        return Outcome("refused", f"an unexpected prompt: {r.unanswered!r}")
    return r.ok


@action(requires={"power": "on", "os": "installed"}, readonly=True)
async def verify(params, proc):
    """read-only: the reinstalled system answers, and is the one we expect"""
    r = await proc.run(["ssh", "-o", "BatchMode=yes", f"root@{params['host']}", "hostname"], log="verify.log")
    if r.text.strip() != params["node"]:
        raise OutcomeError(FAILED, f"{params['host']} says it is {r.text.strip()!r}, not {params['node']!r}")
