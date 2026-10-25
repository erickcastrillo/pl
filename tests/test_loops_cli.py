"""Plan 6 slice 5: `pl loops run NAME` queues a run now; the dispatcher starts it on its next pass."""
import argparse
import json
import subprocess
import sys
import tomllib
import types
from datetime import datetime, timedelta

import pytest

from pl import accounts, cli, dispatch, loops, util
from pl import config as C


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    C.ATTN = tmp_path / "attn"
    C.STATE_FILE = C.ATTN / "state.json"
    C.PROFILES = {"cx": tmp_path / ".codex-cx"}
    C.ACCOUNTS = {"cx": {"harness": "codex", "config_dir": str(tmp_path / ".codex-cx")}}
    C.SERVICES = {"invoices": {"prompt": "/pl-billing-chase", "profile": "cx", "every": "daily 09:00"},
                  "triage": {"prompt": "/loop 30m /triage", "profile": "cx"}}
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _args(name, **kw):
    return argparse.Namespace(loops_cmd="run", name=name, wait=False, json=False, **kw)


def test_run_marks_the_loop_due_and_the_next_pass_starts_it(monkeypatch, capsys):
    loops.cmd_loops(_args("invoices"))
    assert "queued; the dispatcher starts it on its next pass" in capsys.readouterr().out
    st = util.load_state()
    assert st["services"]["invoices"]["run_now"] is True

    calls = []
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(a) or {"new-window": "@7", "list-panes": "%9"}.get(a[0], ""))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 10, 26, 20, 0))
    st["services"]["invoices"]["next_at"] = (datetime(2026, 10, 26, 20, 0) + timedelta(days=1)).isoformat()   # not due
    dispatch.ensure_services(st, [], {}, False, None)
    assert [c for c in calls if c[0] == "new-window"]
    assert "run_now" not in st["services"]["invoices"]


def test_run_refuses_a_loop_without_a_schedule_or_an_unknown_one():
    with pytest.raises(SystemExit, match="triage"):
        loops.cmd_loops(_args("triage"))
    with pytest.raises(SystemExit, match="nope"):
        loops.cmd_loops(_args("nope"))


def test_wait_prints_the_record_once_the_run_ends(monkeypatch, capsys):
    record = {"started_at": "a", "ended_at": "b", "result": "3 overdue", "tokens": 12, "exit": 0}

    def sleep(_):
        st = util.load_state()
        st["services"]["invoices"]["last_run"] = record
        util.save_state(st)

    monkeypatch.setattr(loops.time, "sleep", sleep)
    a = _args("invoices")
    a.wait, a.json = True, True
    loops.cmd_loops(a)
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == record


def test_wait_gives_up_when_the_dispatcher_never_picks_the_run_up(monkeypatch):
    monkeypatch.setattr(loops.time, "sleep", lambda _: None)
    monkeypatch.setattr(loops, "WAIT_POLLS", 3)
    a = _args("invoices")
    a.wait = True
    with pytest.raises(SystemExit, match="dispatcher has not picked it up"):
        loops.cmd_loops(a)


@pytest.fixture
def table(profile, monkeypatch):
    d = profile / ".pl-t"
    d.mkdir()
    monkeypatch.setenv("PL_CONFIG_DIR", str(d))
    (d / "config.toml").write_text('[loops.triage]\nprompt = "/loop 30m /triage the inbox"\n')
    C.load("t")
    return d / "config.toml"


def pl(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pl", "loops", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def _loop(table, name):
    return tomllib.loads(table.read_text())["loops"][name]


def test_convert_sets_every_and_strips_the_loop_prefix(table, monkeypatch):
    assert pl(monkeypatch, "edit", "triage", "--convert") == 0
    assert _loop(table, "triage") == {"prompt": "/triage the inbox", "every": "30m"}
    table.write_text('[loops.a]\nprompt = "/loop 1h /x"\n[loops.b]\nprompt = "/loop 2h /y"\n')
    C.load(config_dir=str(table.parent))
    assert pl(monkeypatch, "edit", "a", "--convert") == 0 and pl(monkeypatch, "edit", "b", "--convert") == 0
    assert (_loop(table, "a")["every"], _loop(table, "a")["prompt"]) == ("hourly", "/x")
    assert (_loop(table, "b")["every"], _loop(table, "b")["prompt"]) == ("120m", "/y")


def test_convert_of_a_day_interval_exits_2(table, monkeypatch, capsys):
    table.write_text('[loops.daily]\nprompt = "/loop 1d /x"\n')
    C.load(config_dir=str(table.parent))
    before = table.read_text()
    assert pl(monkeypatch, "edit", "daily", "--convert") == 2
    assert "--every" in capsys.readouterr().err
    assert table.read_text() == before


def test_edit_of_the_builtin_review_loop_writes_its_table(table, monkeypatch):
    (table.parent / "ha").mkdir()
    table.write_text('[tracker]\ntype = "github-project"\nowner = "o"\nrepo = "o/r"\n[accounts.a]\nconfig_dir = "~/.pl-t/ha"\n'
                     '[code_host.labels]\nreview = "r"\nready = "ok"\n[loops.triage]\nprompt = "p"\n')
    C.load(config_dir=str(table.parent))
    builtin = C.SERVICES["auto-review"]
    assert builtin["builtin"] and "loops" in tomllib.loads(table.read_text())
    assert "auto-review" not in tomllib.loads(table.read_text())["loops"]
    assert pl(monkeypatch, "edit", "auto-review", "--every", "hourly", "--hours", "08:00-18:00 weekdays") == 0
    assert _loop(table, "auto-review") == {"prompt": builtin["prompt"], "account": "a", "every": "hourly", "hours": "08:00-18:00 weekdays"}
    assert "triage" in tomllib.loads(table.read_text())["loops"]


def test_edit_changes_fields_and_refuses_a_bad_every(table, monkeypatch, capsys):
    assert pl(monkeypatch, "edit", "triage", "--skill", "pl-x", "--writes", "--account", "cx") == 0
    assert (_loop(table, "triage")["skill"], _loop(table, "triage")["writes"]) == ("pl-x", True)
    assert "prompt" not in _loop(table, "triage")
    assert pl(monkeypatch, "edit", "triage", "--prompt", "again") == 0
    assert (_loop(table, "triage")["prompt"], "skill" in _loop(table, "triage")) == ("again", False)
    assert pl(monkeypatch, "edit", "triage", "--no-writes") == 0 and _loop(table, "triage")["writes"] is False
    assert pl(monkeypatch, "edit", "triage", "--every", "whenever") == 2
    assert pl(monkeypatch, "edit", "nope", "--every", "30m") == 2


NAMES = ("auto-review", "daily-standup", "inbox-triage", "overdue-invoices", "dependency-updates", "stale-cards")


def test_every_template_fills_every_field(table, monkeypatch):
    assert tuple(loops.TEMPLATES) == NAMES
    for name in NAMES:
        assert pl(monkeypatch, "add", f"t-{name}", "--template", name, "--yes") == 0
        entry = _loop(table, f"t-{name}")
        assert entry["prompt"] and entry["every"] and entry["hours"] and isinstance(entry["writes"], bool)
    assert pl(monkeypatch, "add", "x", "--template", "nope", "--yes") == 2


def test_flags_override_a_template(table, monkeypatch):
    assert pl(monkeypatch, "add", "mine", "--template", "stale-cards", "--every", "daily 07:30", "--prompt", "mine", "--writes", "--yes") == 0
    entry = _loop(table, "mine")
    assert (entry["every"], entry["prompt"], entry["writes"]) == ("daily 07:30", "mine", True)
    assert entry["hours"] == loops.TEMPLATES["stale-cards"]["hours"]


def test_templates_name_no_vendor():
    vendors = ("gmail", "outlook", "google", "microsoft", "stripe", "quickbooks", "xero", "paypal", "slack", "github", "gitlab", "jira", "linear", "notion", "sendgrid")
    for name, template in loops.TEMPLATES.items():
        assert not [v for v in vendors if v in str(template).lower()], name
