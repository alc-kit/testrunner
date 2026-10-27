"""A project's wrapper (`./run.sh`) names itself in TESTRUNNER_PROG, so the help and every
"run this next" hint say the command the operator types, not `testrunner` (user, 2026-10-27:
"for the untrained eye that could be misleading"). Each with its control: unset, it is testrunner."""
import json

from conftest import main


def help_text(capsys, monkeypatch, prog=None):
    if prog is None:
        monkeypatch.delenv("TESTRUNNER_PROG", raising=False)
    else:
        monkeypatch.setenv("TESTRUNNER_PROG", prog)
    try:
        main(["--help"])
    except SystemExit:
        pass
    return capsys.readouterr().out


def test_the_help_names_the_wrapper(capsys, monkeypatch):
    out = help_text(capsys, monkeypatch, "./run.sh")
    assert out.startswith("usage: ./run.sh ")
    assert "./run.sh runs testrunner (TESTRUNNER_PROG=./run.sh)." in out
    assert "  ./run.sh --plan [steps]         compile + validate" in out      # the column stays put
    assert "  ./run.sh                        run the run config's plan" in out
    assert "\n  testrunner " not in out


def test_control_unset_it_is_testrunner(capsys, monkeypatch):
    out = help_text(capsys, monkeypatch)
    assert out.startswith("usage: testrunner ")
    assert "runs testrunner (TESTRUNNER_PROG" not in out
    assert "  testrunner --plan [steps]       compile + validate" in out


def test_an_unknown_variables_hint_names_the_wrapper(sub, capsys, monkeypatch):
    from test_validate import make_sub
    s = make_sub(sub)
    s.write("unsure", "")
    monkeypatch.setenv("TESTRUNNER_PROG", "./run.sh")
    main(["--root", str(s.root), "--color", "never", "--non-interactive", "make"])
    capsys.readouterr()
    ev = [json.loads(ln) for ln in (s.root / "state" / "journal.jsonl").read_text().splitlines()]
    why = next(e["why"] for e in ev if e["event"] == "state_unknown")
    assert "`./run.sh --plan` reads it again" in why and "`testrunner --plan`" not in why
