import os
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "tools" / "content-guard"


def guard(tmp_path, text, deny=""):
    f = tmp_path / "f.txt"
    f.write_text(text)
    env = {**os.environ, "CONTENT_DENYLIST": deny}
    return subprocess.run([sys.executable, str(GUARD), str(f)], capture_output=True, text=True, env=env)


def test_clean_text_passes(tmp_path):
    assert guard(tmp_path, "address 203.0.113.7 is documentation space\n").returncode == 0


def test_private_address_refused(tmp_path):
    assert guard(tmp_path, "host " + ".".join(["192", "168", "4", "20"]) + "\n").returncode == 1


def test_denylisted_name_refused_and_not_echoed(tmp_path):
    r = guard(tmp_path, "we test against acme-prod-03 here\n", deny="acme-prod")
    assert r.returncode == 1 and "acme-prod" not in r.stdout + r.stderr
