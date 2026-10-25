"""Plan 6 slice 4: a loop with `every` runs as a fresh headless session; each run is recorded."""
import json
import os
import shlex
import stat
import subprocess
import types
from datetime import datetime

import pytest

from pl import accounts, dispatch, harnesses
from pl import config as C

FAKE_CLI = '#!/bin/sh\necho "working"\necho "3 invoices overdue"\n'


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    C.PROFILES = {"cx": tmp_path / ".codex-cx"}
    C.ACCOUNTS = {"cx": {"harness": "codex", "config_dir": str(tmp_path / ".codex-cx")}}
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def tmux_calls(monkeypatch):
    calls, panes = [], {"text": ""}

    def tmux(*args, check=True):
        calls.append(args)
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=panes["text"], stderr=""))
    return calls, panes


def _script(calls):
    typed = [c[-1] for c in calls if c[0] == "send-keys" and "-l" in c]
    return shlex.split(typed[0])[1] if typed else None


def test_headless_script_runs_unattended_and_records_outcome(profile):
    cli = profile / "fakecodex"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    h = harnesses.Harness("codex", str(cli), "CODEX_HOME", ["x"], [str(cli), "exec", "{prompt}"], [])
    log, record = profile / "run.log", profile / "run.json"
    script = harnesses.headless_script(h, "cx", "/pl-billing-chase", "invoices", log, record)
    assert "--ask-for-approval never" in script and "--sandbox workspace-write" in script
    path = profile / "launch.sh"
    path.write_text(script)
    subprocess.run(["sh", str(path)], check=True)
    run = json.loads(record.read_text())
    assert run["exit"] == 0 and run["result"] == "3 invoices overdue" and run["tokens"] == "n/a"
    assert run["started_at"] <= run["ended_at"]
    assert "working" in log.read_text() and not path.exists()


def test_headless_script_creates_mode_0600_log(profile):
    cli = profile / "failing"
    cli.write_text("#!/bin/sh\nexit 3\n")
    cli.chmod(0o755)
    h = harnesses.Harness("codex", str(cli), None, ["x"], [str(cli), "exec", "{prompt}"], [])
    log, record = profile / "run.log", profile / "run.json"
    script = harnesses.headless_script(h, "cx", "p", "invoices", log, record)
    subprocess.run(["sh", "-c", script], check=True)
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600 and stat.S_IMODE(os.stat(record).st_mode) == 0o600
    assert json.loads(record.read_text())["exit"] == 3


def _loop(**kw):
    C.SERVICES = {"invoices": {"prompt": "/pl-billing-chase", "profile": "cx", "every": "daily 09:00", **kw}}


def test_scheduled_loop_starts_when_due_and_records_run_codex(profile, tmux_calls):
    calls, panes = tmux_calls
    _loop()
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    script = open(_script(calls)).read()
    assert "codex" in script and "--ask-for-approval" in script and "exec" in script
    assert ("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", "invoices") == calls[[c[0] for c in calls].index("new-window")][:6]
    s = st["services"]["invoices"]
    assert s["started_at"] and datetime.fromisoformat(s["next_at"]) > datetime.now()
    record = C.STATE_DIR / "loops" / "invoices.run.json"
    record.write_text(json.dumps({"started_at": "a", "ended_at": "b", "result": "ok", "tokens": "n/a", "exit": 0}))
    panes["text"] = "@7 %9 sh invoices\n"
    calls.clear()
    dispatch.ensure_services(st, [], {}, False, None)
    assert st["services"]["invoices"]["last_run"]["result"] == "ok" and st["services"]["invoices"]["runs"][-1]["exit"] == 0
    assert ("kill-window", "-t", "@7") == calls[0][:3] and not record.exists()
    assert _script(calls) is None   # not due again yet


def test_scheduled_loop_skips_run_outside_quiet_hours(profile, tmux_calls, monkeypatch):
    calls, _ = tmux_calls
    _loop(hours="08:00-18:00 weekdays")
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 10, 26, 20, 0))   # a Monday evening
    events = []
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: events.append(a))
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    assert _script(calls) is None and events == []
    assert st["services"]["invoices"]["next_at"] == datetime(2026, 10, 27, 9, 0).isoformat()


def test_missed_run_fires_once_on_wake(profile, tmux_calls, monkeypatch):
    calls, _ = tmux_calls
    _loop(hours="08:00-18:00")
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 10, 28, 10, 0))
    st = {"services": {"invoices": {"next_at": datetime(2026, 10, 24, 9, 0).isoformat()}}}   # four days asleep
    dispatch.ensure_services(st, [], {}, False, None)
    assert len([c for c in calls if c[0] == "new-window"]) == 1
    assert st["services"]["invoices"]["next_at"] == datetime(2026, 10, 29, 9, 0).isoformat()
