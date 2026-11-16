"""The 2026-11-11 incident: a pass re-checks, restarts and escalates, and never moves, merges or edits a card."""
import json
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from pl import accounts, agent_screen, alerts, board, commands, dispatch, events, steward
from pl import config as C

NOW = 1_000_000.0
HOUR = 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _card(cid, col, pane=None, **meta):
    m = {"pipeline_mode": "auto", **meta}
    if pane:
        m["worker"] = {"stage": "run", "pane": pane}
    return {"id": cid, "title": f"Title {cid}", "list_id": col, "description": "body", "updated_at": iso(NOW - 9 * HOUR), "metadata": m}


def _profile(tmp_path, name):
    d = tmp_path / f".pl-{name}"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text("")
    C.load(name)


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    _profile(tmp_path, "t")
    monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    monkeypatch.setattr(alerts, "_offer", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 11, 16, 8, 0))
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", None))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "@5 auto-review" if a[0] == "list-windows" else "")
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("the diff is over max_lines", "", ""))
    monkeypatch.setattr(steward.events, "_read", lambda: [])
    forbidden = []
    for mod, names in ((commands, ("merge", "done", "drop", "approve", "reject", "move", "update")), (board, ("update", "set_section"))):
        for n in names:
            if hasattr(mod, n):
                monkeypatch.setattr(mod, n, lambda *a, _n=f"{mod.__name__}.{n}", **k: forbidden.append(_n))
    monkeypatch.setattr(accounts, "parking", lambda name: {"until": iso(NOW - 3 * HOUR), "next_check": iso(NOW + HOUR)})
    monkeypatch.setattr(accounts, "checkable", lambda name: True)
    monkeypatch.setattr(accounts, "_check_lock", lambda name: __import__("contextlib").nullcontext(True))
    monkeypatch.setattr(accounts, "_check", lambda name: "ok")
    monkeypatch.setattr(C, "PROFILES", {"acct": "/x"})
    C.SERVICES = {"auto-review": {"prompt": "/loop 30m /pl-review", "profile": "p"}}
    C.AUTO_MERGE = {"enabled": True, "every": "15m"}
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(steward.notifications, "send", lambda ev, title, msg: sent.append(title) or True)
    yield {"forbidden": forbidden, "sent": sent}
    for k, v in saved.items():
        setattr(C, k, v)


CARDS = [_card("o/r#1", "PR open"), _card("o/r#2", "PR open"),
         _card("o/r#3", "In progress", "%3", question="which one?"), _card("o/r#4", "In progress", "%4")]


def _state():
    return {"services": {"auto-review": {"ran_at": iso(NOW - 4 * HOUR)}, "auto-merge": {"ran_at": iso(NOW - 3 * HOUR)}}}


def _pass(st):
    return steward.run_pass(CARDS, st, reg={}, now=NOW)


def test_incident_pass_rechecks_restarts_and_escalates(world):
    for cid in ("o/r#1", "o/r#2"):
        alerts.open(f"pr_person:{cid}", "warn", "t", "f", cards=[cid])
    st = _state()
    rows = _pass(st)
    assert {"account check: ok", "restart"} <= {r["action"] for r in rows}
    assert st["services"]["auto-merge"]["run_now"] is True
    lines = sorted(it["line"] for it in st["steward"]["items"].values())
    assert lines == ["PR needs a person", "PR needs a person", "question on the agent screen", "size limit (max_lines)"]
    assert len([t for t in world["sent"] if t.startswith("needs you")]) == 4


def test_incident_second_pass_sends_nothing_new(world):
    for cid in ("o/r#1", "o/r#2"):
        alerts.open(f"pr_person:{cid}", "warn", "t", "f", cards=[cid])
    st = _state()
    _pass(st)
    world["sent"].clear()
    _pass(st)
    assert world["sent"] == []


def test_steward_never_moves_merges_or_edits_sections(world):
    _pass(_state())
    assert world["forbidden"] == []


def test_two_profiles_keep_their_own_state(tmp_path, monkeypatch):
    C.SERVICES, C.AUTO_MERGE = {}, {}
    seen = {}
    for name, cid in (("pa", "o/a#1"), ("pb", "o/b#2")):
        _profile(tmp_path, name)
        monkeypatch.setattr(steward.board, "cards", lambda cid=cid: [_card(cid, "In progress", "%1", question="q")])
        steward.cmd_steward(Namespace(once=True, dry_run=False, json=False))
        seen[name] = (C.STATE_FILE, C.STATE_DIR / "alerts.json")
    for name, other in (("pa", "o/b#2"), ("pb", "o/a#1")):
        text = "".join(p.read_text() for p in seen[name] if p.exists())
        assert other not in text and json.loads(seen[name][0].read_text())["steward"]["items"]
