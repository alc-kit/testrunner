import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "tools" / "content-guard"


def guard(tmp_path, text):
    f = tmp_path / "f.txt"
    f.write_text(text)
    return subprocess.run([sys.executable, str(GUARD), str(f)], capture_output=True, text=True)


def test_clean_text_passes(tmp_path):
    assert guard(tmp_path, "address 203.0.113.7 is documentation space\n").returncode == 0


def test_private_address_refused(tmp_path):
    assert guard(tmp_path, "host " + ".".join(["192", "168", "4", "20"]) + "\n").returncode == 1


def test_internal_host_name_refused(tmp_path):
    assert guard(tmp_path, "ssh build-01.corp\n").returncode == 1
