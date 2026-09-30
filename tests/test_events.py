"""WP8: the event log records what happens; metrics count it exactly per window."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from pl import commands, dispatch, events
from pl import config as C

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _write(*evs):
    """Put raw events into the log; each is (kind, when, extra dict)."""
    path = C.STATE_DIR / "events.jsonl"
    with open(path, "a") as f:
        for kind, when, extra in evs:
            f.write(json.dumps({"ts": when.isoformat(), "profile": "t", "kind": kind, "card": "c1", **extra}) + "\n")
    return path


def _lines():
    return [json.loads(x) for x in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]


def test_emit_appends_a_line_with_profile_and_creates_the_file_0600():
    events.emit("moved", "c1", **{"from": "Inbox", "to": "Spec ready"})
    events.emit("approved", "c1")
    path = C.STATE_DIR / "events.jsonl"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    first, second = _lines()
    assert first["kind"] == "moved" and first["card"] == "c1" and first["profile"] == "t"
    assert first["from"] == "Inbox" and first["to"] == "Spec ready"
    assert datetime.fromisoformat(first["ts"]).utcoffset() is not None
    assert second["kind"] == "approved"


def test_legacy_mode_profile_is_null(fake_home):
    C.load()
    events.emit("approved", "c1")
    assert _lines()[0]["profile"] is None


def test_error_message_is_cut_to_200_chars():
    events.emit("error", None, message="x" * 500)
    assert len(_lines()[0]["message"]) == 200


def _pass(monkeypatch, col_of):
    """One dispatcher pass over one auto card whose column is col_of['now']; nothing real runs."""
    card = {"id": "card0001", "title": "SECRET TITLE", "description": "SECRET BODY", "list_id": "L",
            "metadata": {"pipeline_mode": "auto"}}
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [card])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: col_of["now"])
    monkeypatch.setattr(dispatch, "stage_for", lambda c, col: None)
    monkeypatch.setattr(dispatch, "mirror_to_product", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: None)   # no real tmux
    dispatch.dispatch_once(1, False, pull=False)


def test_a_column_change_emits_one_moved_and_a_quiet_pass_emits_none(monkeypatch):
    col = {"now": "Inbox"}
    _pass(monkeypatch, col)
    assert not (C.STATE_DIR / "events.jsonl").exists()      # first sighting records, does not emit
    col["now"] = "Spec ready"
    _pass(monkeypatch, col)
    _pass(monkeypatch, col)
    moved = [e for e in _lines() if e["kind"] == "moved"]
    assert len(moved) == 1
    assert (moved[0]["card"], moved[0]["from"], moved[0]["to"]) == ("card0001", "Inbox", "Spec ready")
    assert json.loads(C.STATE_FILE.read_text())["last_col"] == {"card0001": "Spec ready"}
    assert "SECRET" not in (C.STATE_DIR / "events.jsonl").read_text()


def test_existing_state_keys_survive(monkeypatch):
    C.STATE_FILE.write_text(json.dumps({"notified": {"k": 1}, "services": {"s": 2}}))
    _pass(monkeypatch, {"now": "Inbox"})
    st = json.loads(C.STATE_FILE.read_text())
    assert st["notified"] == {"k": 1} and st["services"] == {"s": 2}


def test_a_pass_that_hits_a_tracker_error_emits_error(monkeypatch):
    def boom(*a, **k):
        raise SystemExit("board said no " + "y" * 400)
    monkeypatch.setattr(dispatch, "dispatch_once", boom)
    a = type("A", (), {"dry_run": True, "max_runs": 1, "max_prep": 1, "no_pull": True, "once": True, "interval": 0})()
    dispatch.cmd_dispatch(a)
    e = _lines()[0]
    assert e["kind"] == "error" and e["message"].startswith("board said no") and len(e["message"]) == 200


def test_metrics_count_only_inside_the_window_edge_excluded():
    h = timedelta(hours=1)
    _write(("moved", NOW - h, {"from": "Inbox", "to": "Spec ready"}),                    # exactly at the edge: out
           ("moved", NOW - h + timedelta(seconds=1), {"from": "Inbox", "to": "Spec ready"}),
           ("moved", NOW - timedelta(minutes=5), {"from": "Spec ready", "to": "Plan for review"}),
           ("moved", NOW - timedelta(minutes=5), {"from": "Plan for review", "to": "Approved"}),
           ("approved", NOW - timedelta(minutes=1), {}),
           ("approved", NOW - h - timedelta(seconds=1), {}),
           ("error", NOW - timedelta(days=3), {"message": "old"}),
           ("error", NOW - timedelta(days=2), {"message": "latest"}))
    m = events.metrics(3600, now=NOW)
    assert (m["specs_written"], m["plans_written"], m["approvals"]) == (1, 1, 1)
    assert m["errors_last"]["message"] == "latest"


def test_metrics_compare_instants_not_strings():
    # 13:30+02:00 is 11:30 UTC: 30 minutes before NOW, inside a 1 hour window although it sorts after "12:00" as a string
    inside = datetime(2026, 9, 28, 13, 30, tzinfo=timezone(timedelta(hours=2)))
    outside = datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc)
    _write(("approved", inside, {}), ("approved", outside, {}))
    assert events.metrics(3600, now=NOW)["approvals"] == 1


def test_metrics_on_an_empty_log():
    assert events.metrics(3600, now=NOW) == {"specs_written": 0, "plans_written": 0, "approvals": 0, "errors_last": None}
    assert events.daily("Spec ready", now=NOW) == [0] * 14


def test_daily_counts_moves_into_a_column_per_day_oldest_first():
    into = {"from": "Inbox", "to": "Spec ready"}
    _write(("moved", NOW, into), ("moved", NOW - timedelta(hours=1), into),
           ("moved", NOW - timedelta(days=2), into),
           ("moved", NOW - timedelta(days=13), into),
           ("moved", NOW - timedelta(days=14), into),                                   # 15th day back: out
           ("moved", NOW, {"from": "Inbox", "to": "Manual"}))
    d = events.daily("Spec ready", days=14, now=NOW)
    assert len(d) == 14 and d[-1] == 2 and d[-3] == 1 and d[0] == 1 and sum(d) == 4


def test_retention_keeps_29_days_drops_31_days_only_past_20000_lines(monkeypatch):
    monkeypatch.setattr(events, "_now", lambda: NOW)    # retention measures age from this clock, not the real one
    old, keep = NOW - timedelta(days=31), NOW - timedelta(days=29)
    path = _write(("moved", old, {"to": "Old"}), ("moved", keep, {"to": "Keep"}))
    events.emit("approved", "c1")                       # small file: nothing pruned, even the 31 day event
    assert len(_lines()) == 3
    _write(*[("approved", datetime.now(timezone.utc), {})] * 20000)
    events.emit("approved", "c2")
    kept = _lines()
    assert not any(e.get("to") == "Old" for e in kept)
    assert any(e.get("to") == "Keep" for e in kept)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_approve_and_reject_emit(monkeypatch):
    c = {"id": "card0002", "title": "t", "list_id": "L", "description": "", "metadata": {}}
    monkeypatch.setattr(commands, "find_card", lambda t: c)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Plan for review")
    monkeypatch.setattr(commands, "col_id", lambda n: "X")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a: None)
    monkeypatch.setattr(commands, "plan_path", lambda c: C.STATE_DIR / "nope.md")
    commands.cmd_approve(type("A", (), {"id": "card0002", "force": False})())
    commands.cmd_reject(type("A", (), {"id": "card0002", "notes": "private words"})())
    got = [(e["kind"], e["card"]) for e in _lines()]
    assert got == [("approved", "card0002"), ("rejected", "card0002")]
    assert "private words" not in (C.STATE_DIR / "events.jsonl").read_text()


def test_concurrent_writers_past_the_cap_lose_no_event(monkeypatch):
    import threading
    monkeypatch.setattr(events, "MAX_LINES", 40)
    now = datetime.now(timezone.utc)
    path = _write(*[("approved", now, {})] * 45)        # past the cap: every emit trims
    n_threads, per = 8, 40
    barrier = threading.Barrier(n_threads)

    def work(k):
        barrier.wait()
        for i in range(per):
            events.emit("moved", f"w{k}-{i}", **{"from": "Inbox", "to": "Spec ready"})
    ts = [threading.Thread(target=work, args=(k,)) for k in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    cards = {e["card"] for e in _lines()}
    lost = [f"w{k}-{i}" for k in range(n_threads) for i in range(per) if f"w{k}-{i}" not in cards]
    assert lost == [], f"{len(lost)} of {n_threads * per} events lost"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(C.STATE_DIR.glob("*.tmp*"))


def test_an_uncreatable_lock_file_still_appends_the_event():
    path = _write(("approved", NOW, {}))
    os.chmod(path, 0o600)
    os.chmod(C.STATE_DIR, 0o500)                     # the lock file cannot be created; events.jsonl stays writable
    try:
        events.emit("approved", "c2")
    finally:
        os.chmod(C.STATE_DIR, 0o700)
    assert [e["card"] for e in _lines()] == ["c1", "c2"]
