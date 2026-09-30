"""Alerts: open, escalate after 1 h and 4 h, resolve when the check clears; ack stops the reminders."""
import argparse
import json
import stat
import time

import pytest

from pl import accounts, alerts, dispatch, memory, usage
from pl import config as C
from pl.trackers.github import RateLimited

SECRET = "TOPSECRET card words"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"acme": tmp_path / ".claude-acme", "acme2": tmp_path / ".claude-acme2"}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    return t


def _stored():
    p = C.STATE_DIR / "alerts.json"
    return p.read_text() if p.exists() else ""


# ---------- the lifecycle ----------

def test_open_escalate_resolve_with_a_fake_clock(clock):
    assert alerts.open("k1", "high", "Card abcd1234: spec died", "pl retry abcd1234") == "open"
    clock["now"] += 60
    assert alerts.open("k1", "high", "Card abcd1234: spec died", "pl retry abcd1234") is None   # a repeat never notifies
    a = alerts.get("k1")
    assert a["count"] == 2 and a["last_seen"] == clock["now"] and a["first_seen"] == clock["now"] - 60
    clock["now"] += 3600
    assert alerts.open("k1", "high", "t", "f") == "1 h"
    clock["now"] += 3600
    assert alerts.open("k1", "high", "t", "f") is None
    clock["now"] += 3 * 3600
    assert alerts.open("k1", "high", "t", "f") == "4 h"
    clock["now"] += 3600 * 20
    assert alerts.open("k1", "high", "t", "f") is None       # nothing after 4 h
    assert alerts.resolve("k1") is True
    a = alerts.get("k1")
    assert a["resolved_at"] == clock["now"] and a["duration"] == 60 + 3600 * 25
    assert [x["key"] for x in alerts.listing()] == [] and [x["key"] for x in alerts.listing(all=True)] == ["k1"]
    assert alerts.resolve("k1") is False                     # already resolved
    assert alerts.open("k1", "high", "t", "f") == "open"     # a new episode
    assert alerts.get("k1")["count"] == 1


def test_the_file_is_private_and_written_by_rename(clock):
    alerts.open("k1", "warn", "t", "f")
    p = C.STATE_DIR / "alerts.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not list(C.STATE_DIR.glob(".alerts*.tmp"))
    p.write_text("not json")                                 # a broken file reads as empty, never raises
    assert alerts.listing() == []


def test_sweep_resolves_what_cleared_and_notifies_only_escalations(clock):
    sent = []
    alerts.open("pr_waiting:a", "warn", "Card a: PR waits", "review it")
    alerts.open("pr_waiting:b", "warn", "Card b: PR waits", "review it")
    alerts.open("other:x", "warn", "t", "f")
    alerts.sweep("pr_waiting:", {"pr_waiting:a", "pr_waiting:b"}, lambda t, m: sent.append(t))
    assert sent == []                                        # no escalation yet
    clock["now"] += 3601
    alerts.sweep("pr_waiting:", {"pr_waiting:a"}, lambda t, m: sent.append(t))
    assert sent == ["Still open after 1 h: Card a: PR waits"]
    assert alerts.get("pr_waiting:b")["resolved_at"] and not alerts.get("other:x").get("resolved_at")
    alerts.sweep("pr_waiting:", {"pr_waiting:a"}, lambda t, m: sent.append(t))
    assert len(sent) == 1                                    # one notification per escalation


def test_ack_stops_escalation_until_it_resolves_and_opens_again(clock):
    alerts.open("k1", "high", "t", "f")
    assert alerts.ack("k1") is True and alerts.get("k1")["acked"]
    assert alerts.ack("nope") is False
    clock["now"] += 5 * 3600
    assert alerts.open("k1", "high", "t", "f") is None
    alerts.resolve("k1")
    assert alerts.open("k1", "high", "t", "f") == "open" and not alerts.get("k1").get("acked")
    clock["now"] += 3601
    assert alerts.open("k1", "high", "t", "f") == "1 h"


def test_pl_alerts_lists_open_ones_and_acks(clock, capsys):
    alerts.open("stage_failed:abcd1234:spec", "high", "Card abcd1234: the spec agent died 3 times", "pl retry abcd1234")
    alerts.open("memory_low", "high", "Low memory", "close apps")
    alerts.resolve("memory_low")
    alerts.cmd_alerts(argparse.Namespace(all=False, ack=None))
    out = capsys.readouterr().out
    assert "stage_failed:abcd1234:spec" in out and "pl retry abcd1234" in out and "memory_low" not in out
    alerts.cmd_alerts(argparse.Namespace(all=True, ack=None))
    assert "memory_low" in capsys.readouterr().out
    alerts.cmd_alerts(argparse.Namespace(all=False, ack="stage_failed:abcd1234:spec"))
    assert "acknowledged" in capsys.readouterr().out and alerts.get("stage_failed:abcd1234:spec")["acked"]
    with pytest.raises(SystemExit):
        alerts.cmd_alerts(argparse.Namespace(all=False, ack="nope"))


# ---------- the wired checks ----------

def _card(cid, col, **meta):
    return {"id": cid, "title": SECRET, "list_id": col, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


def _pass(monkeypatch, cs, status="none", screen=None, parked=(), healthy="acme", loops=None):
    """One dispatcher pass with the board, tmux, the registry and accounts faked. Returns the notifications."""
    notes = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: (status, w.get("session_id")))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: screen)
    monkeypatch.setattr(dispatch, "mark_exhausted", lambda prof, s: "2026-09-30T18:00:00+00:00")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set(parked))
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, all_cards, harness=None: healthy)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    for name in ("sweep_untracked", "mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "notify", lambda t, m: notes.append((t, m)))
    monkeypatch.setattr(usage, "scan", lambda save=True: {})
    monkeypatch.setattr(usage, "summary", lambda st, now=None: {"loops": loops or {}})
    dispatch.dispatch_once(1, False, pull=False)
    return notes


def _open(key):
    a = alerts.get(key)
    return bool(a) and not a.get("resolved_at")


def test_an_account_parked_opens_once_and_resolves_when_it_is_back(monkeypatch):
    w = {"stage": "spec", "session_id": "s", "pane": "%1", "window": "@1", "started_at": "2020-01-01T00:00:00+00:00"}
    c = _card("card0001aaaa", "Inbox", worker=w)
    notes = _pass(monkeypatch, [c], status="alive", screen="You've hit your limit", parked={"acme"}, healthy="acme2")
    assert _open("account_parked:acme") and [t for t, _ in notes] == ["Profile acme hit its usage limit"]
    c["metadata"].update(worker=dict(w), profile="acme")      # the same account hits the limit again
    assert _pass(monkeypatch, [c], status="alive", screen="You've hit your limit", parked={"acme"}, healthy="acme2") == []
    _pass(monkeypatch, [c], status="alive", parked=())
    assert not _open("account_parked:acme")


def test_all_accounts_out_opens_and_resolves(monkeypatch):
    c = _card("card0001aaaa", "Inbox")
    notes = _pass(monkeypatch, [c], parked={"acme", "acme2"}, healthy=None)
    assert _open("accounts_all_out") and [t for t, _ in notes] == ["Every Claude profile is out of credits"]
    assert _pass(monkeypatch, [c], parked={"acme", "acme2"}, healthy=None) == []   # a repeat: no second note
    _pass(monkeypatch, [c], parked=())
    assert not _open("accounts_all_out")


def test_a_stage_that_failed_3_times_opens_and_resolves_on_retry(monkeypatch):
    w = {"stage": "spec", "attempts": 3, "session_id": "s", "pane": "%1", "window": "@1"}
    c = _card("card0001aaaa", "Inbox", worker=w)
    notes = _pass(monkeypatch, [c], status="dead")
    assert _open("stage_failed:card0001aaaa:spec") and len(notes) == 1 and notes[0][0].startswith("Needs you")
    assert _pass(monkeypatch, [c], status="dead") == []
    c["metadata"].pop("worker")                               # pl retry clears the worker
    _pass(monkeypatch, [c])
    assert not _open("stage_failed:card0001aaaa:spec")


def test_a_dead_loop_opens_and_resolves(monkeypatch):
    C.SERVICES = {"merge-check": {"prompt": "/loop 30m /merge-check", "profile": "acme"}}
    notes = _pass(monkeypatch, [], loops={"merge-check": {"dead": True}})
    assert _open("loop_dead:merge-check") and len(notes) == 1
    _pass(monkeypatch, [], loops={"merge-check": {"dead": False}})
    assert not _open("loop_dead:merge-check")


def test_a_pr_waiting_over_24_hours_opens_and_resolves(monkeypatch):
    c = _card("card0001aaaa", "PR open")
    _pass(monkeypatch, [c])
    assert not _open("pr_waiting:card0001aaaa")               # a fresh PR
    c["updated_at"] = "2020-01-01T00:00:00+00:00"
    notes = _pass(monkeypatch, [c])
    assert _open("pr_waiting:card0001aaaa") and len(notes) == 1
    c["list_id"] = "Done"
    _pass(monkeypatch, [c])
    assert not _open("pr_waiting:card0001aaaa")


def test_a_github_rate_limit_opens_while_backing_off_and_resolves_when_clear(monkeypatch):
    notes = []
    monkeypatch.setattr(dispatch, "notify", lambda t, m: notes.append(t))
    a = argparse.Namespace(dry_run=True, once=True, interval=None, max_runs=None, max_prep=None, no_pull=True)

    def limited(*_a, **_k):
        raise RateLimited(time.time() + 600, "API rate limit exceeded")
    monkeypatch.setattr(dispatch, "dispatch_once", limited)
    dispatch.cmd_dispatch(a)
    dispatch.cmd_dispatch(a)
    assert _open("github_rate_limited") and len(notes) == 1
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *_a, **_k: ([], []))
    dispatch.cmd_dispatch(a)
    assert not _open("github_rate_limited")


def test_low_memory_opens_and_resolves(monkeypatch):
    notes = []
    monkeypatch.setattr(memory, "notify", lambda t, m: notes.append(t))
    monkeypatch.setattr(memory, "status", lambda: {"free": 1, "total": 32 * memory.GB, "low": True})
    st = {}
    memory.check_starts(st)
    memory.check_starts(st)
    assert _open("memory_low") and len(notes) == 1
    monkeypatch.setattr(memory, "status", lambda: {"free": 20 * memory.GB, "total": 32 * memory.GB, "low": False})
    memory.check_starts(st)
    assert not _open("memory_low")


def test_a_runaway_opens_and_resolves_once_the_tree_is_small(monkeypatch):
    notes, ps = [], {"n": 200}
    C.TMUX_SESSION = "pl-t"
    C.DISPATCH = {**C.DISPATCH, "kill_runaway": False}

    def run(argv):
        if argv[0] == "tmux":
            return "@1 100 run-topsecret-card-words\n"
        if argv[0] == "ps":
            rows = ["100 1 3000 -zsh", "101 100 4000 claude"] + [f"{1000 + i} 101 100 ruby x" for i in range(ps["n"])]
            return "\n".join(rows) + "\n"
    monkeypatch.setattr(memory, "reading", lambda: (20 * memory.GB, 32 * memory.GB))
    monkeypatch.setattr(memory, "_run", run)
    monkeypatch.setattr(memory, "notify", lambda t, m: notes.append(t))
    cs = [{"id": "card0001aaaa", "title": SECRET, "metadata": {"worker": {"window": "@1"}}}]
    st = {}
    memory.guard_runaways(st, cs)
    memory.guard_runaways(st, cs)
    assert _open("runaway:@1") and len(notes) == 1 and SECRET in notes[0]   # today's notification text stays
    ps["n"] = 5
    memory.guard_runaways(st, cs)
    assert not _open("runaway:@1")


def test_no_card_text_is_stored(monkeypatch):
    w = {"stage": "spec", "attempts": 3, "session_id": "s", "pane": "%1", "window": "@1"}
    old = {**_card("card0002bbbb", "PR open"), "updated_at": "2020-01-01T00:00:00+00:00"}
    _pass(monkeypatch, [_card("card0001aaaa", "Inbox", worker=w), old], status="dead")
    _pass(monkeypatch, [_card("card0003cccc", "Inbox")], parked={"acme", "acme2"}, healthy=None)
    text = _stored()
    assert "stage_failed:card0001aaaa:spec" in text and "pr_waiting:card0002bbbb" in text and "accounts_all_out" in text
    assert "TOPSECRET" not in text and "topsecret" not in text
    assert all(set(a) <= alerts.FIELDS for a in json.loads(text).values())


WEEKLY = """You've hit your weekly limit, resets Oct 4 at 9pm
   ❯ 1. Stop and wait for limit to reset
     2. Wait here, then continue automatically at Oct 4 at 9pm
     3. Ask your admin for more usage
   Enter to confirm · Esc to cancel"""


def test_a_spec_agent_stuck_on_a_weekly_limit_moves_to_the_other_account(monkeypatch):
    from pl import harnesses
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3 * 3600))   # past the restart window
    w = {"stage": "spec", "session_id": "s", "pane": "%1", "window": "@7", "profile": "acme", "started_at": old}
    c = _card("card0001aaaa", "Inbox", worker=dict(w))
    parked, asked, calls = [], [], []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [c])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: WEEKLY if harnesses.limit_hit(h, WEEKLY) else None)
    monkeypatch.setattr(dispatch, "mark_exhausted", lambda prof, s: parked.append(prof) or "2026-10-05T04:01:00+00:00")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, all_cards, harness=None: asked.append(harness) or "acme2")
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda cid, **k: calls.append(("update", cid, k)))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(("tmux", *a)) or "")
    for name in ("sweep_untracked", "mirror_to_product", "ensure_services", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(usage, "scan", lambda save=True: {})
    monkeypatch.setattr(usage, "summary", lambda st, now=None: {"loops": {}})
    dispatch.dispatch_once(1, False, pull=False)
    assert parked == ["acme"] and _open("account_parked:acme")
    assert asked[0] == "claude"                                   # a healthy account of the same harness
    assert ("tmux", "kill-window", "-t", "@7") in calls
    meta = next(k["metadata"] for op, *rest in calls if op == "update" for cid, k in [rest] if "profile" in k["metadata"])
    assert meta["worker"] is None and meta["profile"] == "acme2"
