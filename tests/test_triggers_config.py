"""Tests for trigger configuration, validation, and CLI commands."""
import json
import stat
import sys
import tomllib

import pytest

from pl import cli
from pl import config as C
from pl import triggers


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PL_TMUX_SESSION", raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _setup_profile(fake_home, text=""):
    d = fake_home / ".pl-test"
    d.mkdir(parents=True, exist_ok=True)
    cfg = d / "config.toml"
    cfg.write_text(text)
    (d / "state").mkdir(parents=True, exist_ok=True)
    C.load("test")
    return d


def test_add_writes_the_table_and_list_json_shows_it(fake_home, monkeypatch, capsys):
    prof = _setup_profile(fake_home, '[connectors.mail]\nrole = "email"\nteams = ["sales"]\n')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pl",
            "--profile",
            "test",
            "triggers",
            "add",
            "sales-mail",
            "--connector",
            "mail",
            "--query",
            "label:sales is:unread",
            "--every",
            "5m",
            "--pipeline",
            "sales",
            "--yes",
        ],
    )
    cli.main()

    doc = tomllib.loads((prof / "config.toml").read_text())
    assert "triggers" in doc and "sales-mail" in doc["triggers"]
    t = doc["triggers"]["sales-mail"]
    assert t["connector"] == "mail"
    assert t["query"] == "label:sales is:unread"
    assert t["every"] == "5m"
    assert t["pipeline"] == "sales"
    assert t["cap"] == 20

    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "test", "triggers", "list", "--json"])
    cli.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    item = next(x for x in data if x["name"] == "sales-mail") if isinstance(data, list) else data["sales-mail"]
    assert item["connector"] == "mail"
    assert item["query"] == "label:sales is:unread"
    assert item["every"] == "5m"
    assert item["pipeline"] == "sales"
    assert item["cap"] == 20
    assert item["paused"] is False


def test_a_connector_not_shared_with_the_pipeline_exits_2(fake_home, monkeypatch, capsys):
    _setup_profile(fake_home, '[connectors.mail]\nrole = "email"\nteams = ["support"]\n')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pl",
            "--profile",
            "test",
            "triggers",
            "add",
            "sales-mail",
            "--connector",
            "mail",
            "--query",
            "label:sales is:unread",
            "--every",
            "5m",
            "--pipeline",
            "sales",
            "--yes",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "triggers.sales-mail.connector" in err


def test_bad_every_lists_the_accepted_forms(fake_home, monkeypatch, capsys):
    _setup_profile(fake_home, '[connectors.mail]\nrole = "email"\nteams = ["sales"]\n')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pl",
            "--profile",
            "test",
            "triggers",
            "add",
            "sales-mail",
            "--connector",
            "mail",
            "--query",
            "label:sales is:unread",
            "--every",
            "not-a-time",
            "--pipeline",
            "sales",
            "--yes",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "triggers.sales-mail.every" in err
    assert "accepted forms" in err.lower()


def test_add_twice_is_unchanged_exit_0(fake_home, monkeypatch):
    prof = _setup_profile(fake_home, '[connectors.mail]\nrole = "email"\nteams = ["sales"]\n')
    cmd = [
        "pl",
        "--profile",
        "test",
        "triggers",
        "add",
        "sales-mail",
        "--connector",
        "mail",
        "--query",
        "label:sales is:unread",
        "--every",
        "5m",
        "--pipeline",
        "sales",
        "--yes",
    ]
    monkeypatch.setattr(sys, "argv", list(cmd))
    cli.main()
    first_text = (prof / "config.toml").read_text()

    monkeypatch.setattr(sys, "argv", list(cmd))
    cli.main()
    second_text = (prof / "config.toml").read_text()
    assert first_text == second_text


def test_triggers_validation_and_parse_every():
    assert triggers.parse_every("30s") == 30.0
    assert triggers.parse_every("5m") == 300.0
    assert triggers.parse_every("1h") == 3600.0
    assert triggers.parse_every("2d") == 172800.0
    assert triggers.parse_every("invalid") is None

    doc = {
        "connectors": {"mail": {"role": "email", "teams": ["sales"]}},
        "triggers": {
            "t1": {"connector": "mail", "query": "is:unread", "every": "5m", "pipeline": "sales"},
        },
    }
    assert triggers.validate(doc) == []

    bad_doc = {"triggers": {"t2": {"connector": "unknown", "every": "bad", "query": 123}}}
    errs = triggers.validate(bad_doc)
    assert any("connector" in e for e in errs)
    assert any("every" in e for e in errs)
    assert any("query" in e for e in errs)


def test_pause_and_resume_updates_triggers_state(fake_home, monkeypatch, capsys):
    prof = _setup_profile(fake_home, '[connectors.mail]\nrole = "email"\nteams = ["sales"]\n')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pl",
            "--profile",
            "test",
            "triggers",
            "add",
            "sales-mail",
            "--connector",
            "mail",
            "--query",
            "label:sales is:unread",
            "--every",
            "5m",
            "--pipeline",
            "sales",
            "--yes",
        ],
    )
    cli.main()

    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "test", "triggers", "pause", "sales-mail"])
    cli.main()
    state_file = prof / "state" / "triggers.json"
    assert state_file.is_file()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    st = json.loads(state_file.read_text())
    assert st["sales-mail"]["paused"] is True

    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "test", "triggers", "list", "--json"])
    cli.main()
    data = json.loads(capsys.readouterr().out)
    item = next(x for x in data if x["name"] == "sales-mail") if isinstance(data, list) else data["sales-mail"]
    assert item["paused"] is True

    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "test", "triggers", "resume", "sales-mail"])
    cli.main()
    st = json.loads(state_file.read_text())
    assert st["sales-mail"]["paused"] is False
