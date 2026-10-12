"""examples/bare-metal, end to end against a stub BMC (ipmitool) and a stub server (ssh): the
example docs/real-hardware.md walks through must actually run, retry on evidence, answer its
destructive confirmation, keep the BMC password out of every log, and catch the wrong machine."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

IPMITOOL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ["STUB_STATE"]); st.mkdir(exist_ok=True)
if os.environ.get("IPMI_PASSWORD") != "change-me":      # -E: the password comes from the environment
    print("Error: Unable to establish IPMI v2 / RMCP+ session", file=sys.stderr); sys.exit(1)
a = " ".join(sys.argv[1:])
power = (st / "power").read_text() if (st / "power").exists() else "off"
if a.endswith("chassis power status"):
    print(f"Chassis Power is {power}")
elif a.endswith("chassis power on"):
    lag = int((st / "lag").read_text()) if (st / "lag").exists() else 0
    if lag:                                              # the BMC accepts, the chassis does not follow (yet)
        (st / "lag").write_text(str(lag - 1))
    else:
        (st / "power").write_text("on")
    print("Chassis Power Control: Up/On")
elif a.endswith("sdr list"):
    print("CPU Temp         | 41 degrees C      | ok")
else:
    sys.exit(2)
'''
SSH = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ["STUB_STATE"]); st.mkdir(exist_ok=True)
cmd = sys.argv[-1]
if (st / "power").read_text() != "on" if (st / "power").exists() else True:
    print("ssh: connect to host: No route to host", file=sys.stderr); sys.exit(255)
if cmd == "cat /etc/os-release":
    if not (st / "installed").exists():
        print("ssh: Connection refused", file=sys.stderr); sys.exit(255)
    print('ID=debian')
elif cmd == "hostname":
    print(os.environ.get("STUB_HOSTNAME", "server-a"))
elif cmd.startswith("touch /tmp/reinstalled"):
    (st / "installed").write_text("1")
'''


@pytest.fixture
def metal(tmp_path):
    root = tmp_path / "bare-metal"
    shutil.copytree(REPO / "examples" / "bare-metal", root, ignore=shutil.ignore_patterns("state"))
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    for name, body in (("ipmitool", IPMITOOL), ("ssh", SSH)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    # the retry's delay is 10 s in the example; a test config keeps the evidence, not the wait
    (root / "configs" / "fast.yml").write_text(
        "extends: default\nedit:\n  - retry: power-on\n    times: 2\n    delay: 0\n    if: {state: {power: \"off\"}}\n")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_STATE": str(tmp_path / "machine"),
           "PYTHONPATH": str(REPO / "src")}
    env.pop("METAL_CONFIG", None)
    return root, env, tmp_path / "machine"


def run(metal, *args, **extra):
    root, env, _ = metal
    r = subprocess.run([sys.executable, "-m", "testrunner", "--root", str(root), "--color", "never",
                        "--non-interactive", "-c", "fast", *args], capture_output=True, text=True,
                       env={**env, **extra}, timeout=120)
    return r


def journal(root):
    return [json.loads(ln) for ln in (root / "state" / "journal.jsonl").read_text().splitlines()]


def test_the_example_runs_end_to_end(metal):
    root, _, machine = metal
    r = run(metal)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    ends = [(e["step"], e["outcome"]) for e in journal(root) if e["event"] == "step_end"]
    assert ends == [("survey", "passed"), ("power-on", "passed"), ("reinstall", "passed"), ("verify", "passed")]
    assert (machine / "installed").exists()                 # the confirmation was answered: it ran
    assert journal(root)[-1]["state"] == {"power": "on", "os": "installed"}


def test_the_observer_judges_power_on_and_the_retry_follows_the_evidence(metal):
    root, _, machine = metal
    machine.mkdir()
    (machine / "lag").write_text("1")                        # the chassis follows on the second try
    r = run(metal)
    assert r.returncode == 0, r.stdout[-3000:]
    ev = journal(root)
    assert [e.get("attempt") for e in ev if e["event"] == "step_end" and e["step"] == "power-on"] == ["1/3", "2/3"]
    retry = next(e for e in ev if e["event"] == "step_retry")
    assert "observed state is {'power': 'off'}" in retry["why"]


def test_control_a_bmc_that_never_powers_on_fails_after_the_retries(metal):
    root, _, machine = metal
    machine.mkdir()
    (machine / "lag").write_text("9")
    r = run(metal)
    assert r.returncode == 1
    ev = journal(root)
    assert [e["outcome"] for e in ev if e["event"] == "step_end" and e["step"] == "power-on"] == ["failed"] * 3
    assert not any(e["event"] == "step_end" and e["step"] == "reinstall" for e in ev)   # never wiped


def test_the_wrong_machine_is_caught(metal):
    r = run(metal, STUB_HOSTNAME="server-b")
    assert r.returncode == 1 and "says it is 'server-b', not 'server-a'" in r.stdout


def test_the_bmc_password_is_never_logged(metal):
    root, _, _ = metal
    r = run(metal, "--show-commands")
    assert r.returncode == 0
    files = {p.relative_to(root / "state").as_posix(): p.read_text(errors="replace")
             for p in (root / "state").rglob("*") if p.is_file()}
    assert "change-me" not in r.stdout + r.stderr
    # in no log, the trace or the journal; only in the scenario snapshot, which keeps the real
    # values as the config file does (later runners need them): guard it like the config
    assert [f for f, text in files.items() if "change-me" in text] == ["scenario.json"]
    assert any("IPMI_PASSWORD=********" in text for text in files.values())   # control: there, masked


def test_plan_reads_the_machine_first(metal):
    r = run(metal, "--plan")
    assert r.returncode == 0 and "survey" in r.stdout and "verify" in r.stdout
