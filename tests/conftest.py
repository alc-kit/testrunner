import io
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from testrunner.cli import main  # noqa: E402


class Sub:
    """A throwaway subscriber in tmp_path."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        return p

    def run(self, *args: str, capsys=None) -> tuple[int, str]:
        code = main(["--root", str(self.root), "--color", "never", "--non-interactive", *args])
        out = capsys.readouterr().out if capsys else ""
        return code, out


@pytest.fixture
def sub(tmp_path):
    return Sub(tmp_path)


@pytest.fixture
def toy(tmp_path):
    dst = tmp_path / "toy"
    shutil.copytree(REPO / "examples" / "toy", dst, ignore=shutil.ignore_patterns("state"))
    return Sub(dst)


@pytest.fixture
def out():
    return io.StringIO()
