"""Plan 6 slice 5: `pl loops run NAME` queues a run now; the dispatcher starts it on its next pass."""
import argparse
import json
import subprocess
import types
from datetime import datetime, timedelta

import pytest

from pl import accounts, dispatch, loops, util
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
