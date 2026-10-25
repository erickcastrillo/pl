"""pl loops list|add|pause|resume|rm: scriptable loop management over [loops.<name>]."""
import json
import sys

import pytest

from pl import cli
from pl import config as C


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".pl-t"
    d.mkdir()
    monkeypatch.setenv("PL_CONFIG_DIR", str(d))
    (d / "config.toml").write_text("")
    C.load("t")
    yield d / "config.toml"
    for k, v in saved.items():
        setattr(C, k, v)


def pl(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pl", "loops", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def test_add_writes_the_table_and_list_shows_its_next_run(profile, monkeypatch, capsys):
    code = pl(monkeypatch, "add", "overdue-invoices", "--skill", "pl-billing-chase", "--every", "weekdays 09:00", "--yes")
    assert code == 0
    C.load(config_dir=str(profile.parent))
    table = profile.read_text()
    assert "[loops.overdue-invoices]" in table and 'skill = "pl-billing-chase"' in table and 'every = "weekdays 09:00"' in table
    capsys.readouterr()
    assert pl(monkeypatch, "list", "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["name"] == "overdue-invoices" and row["state"] == "on" and row["every"] == "weekdays 09:00"
    assert row["next_run"] and row["skill"] == "pl-billing-chase"


def test_add_with_a_bad_every_exits_2_and_lists_the_forms(profile, monkeypatch, capsys):
    assert pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "whenever", "--yes") == 2
    err = capsys.readouterr().err
    assert "30m, hourly, daily HH:MM, weekdays HH:MM, cron:<expr>" in err and len(err.strip().splitlines()) == 1
    assert "[loops.x]" not in profile.read_text()


def test_add_without_a_terminal_needs_yes(profile, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "30m") == 2
    assert "--yes" in capsys.readouterr().err
    assert "[loops.x]" not in profile.read_text()


def test_adding_the_same_loop_twice_changes_nothing(profile, monkeypatch, capsys):
    assert pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "30m", "--yes") == 0
    before = profile.read_text()
    capsys.readouterr()
    assert pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "30m", "--yes") == 0
    assert "loop x exists, nothing changed" in capsys.readouterr().out
    assert profile.read_text() == before


def test_pause_and_resume_flip_enabled(profile, monkeypatch, capsys):
    pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "30m", "--yes")
    assert pl(monkeypatch, "pause", "x") == 0
    assert "enabled = false" in profile.read_text()
    C.load(config_dir=str(profile.parent))
    assert "x" in C.LOOPS_OFF
    capsys.readouterr()
    pl(monkeypatch, "list", "--json")
    assert json.loads(capsys.readouterr().out)[0]["state"] == "paused"
    assert pl(monkeypatch, "resume", "x") == 0
    assert "enabled = true" in profile.read_text()


def test_rm_asks_first(profile, monkeypatch, capsys):
    pl(monkeypatch, "add", "x", "--prompt", "hi", "--every", "30m", "--yes")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert pl(monkeypatch, "rm", "x") == 0
    assert "[loops.x]" in profile.read_text()
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert pl(monkeypatch, "rm", "x") == 0
    assert "[loops.x]" not in profile.read_text()
