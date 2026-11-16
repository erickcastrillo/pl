"""The steward's safe fixes: a park past its reset time, and a loop that missed two intervals."""
from datetime import datetime, timezone

import pytest

from pl import accounts, alerts, dispatch, events, steward
from pl import config as C

NOW = 1_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    yield
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, card=None, **d: out.append((kind, card, d)))
    return out


@pytest.fixture
def park(monkeypatch):
    rec = {"until": iso(NOW - 3 * 3600), "next_check": iso(NOW + 3600)}
    monkeypatch.setattr(C, "PROFILES", {"acct": "/x"})
    monkeypatch.setattr(accounts, "parking", lambda name: rec)
    monkeypatch.setattr(accounts, "checkable", lambda name: True)
    monkeypatch.setattr(accounts, "_check_lock", lambda name: __import__("contextlib").nullcontext(True))
    calls = []
    monkeypatch.setattr(accounts, "_check", lambda name: calls.append(name) or "ok")
    return rec, calls


def test_expired_park_is_checked_and_unparked(park, emitted):
    _, calls = park
    rows = steward.fix_accounts(NOW)
    assert calls == ["acct"] and rows[0]["action"] == "account check: ok"
    assert ("steward", None, {"account": "acct", "reason": "dead", "action": "account check", "result": "ok", "by": "rule"}) in emitted


def test_park_not_expired_is_left(park, emitted):
    rec, calls = park
    rec["until"] = iso(NOW + 3600)
    assert steward.fix_accounts(NOW) == [] and calls == []


def test_check_error_is_logged(park, emitted, monkeypatch):
    monkeypatch.setattr(accounts, "_check", lambda name: "error")
    assert steward.fix_accounts(NOW)[0]["action"] == "account check: error"
    assert emitted[-1][2]["result"] == "error"


@pytest.fixture
def loops(monkeypatch):
    killed = []
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: killed.append(a) or ("@5 review\n@6 other" if a[0] == "list-windows" else ""))
    C.SERVICES = {"review": {"prompt": "/loop 30m /review", "profile": "p"},
                  "daily": {"prompt": "/x", "every": "30m", "profile": "p"}}
    return killed


def _st(name, ago):
    return {"services": {name: {"ran_at": iso(NOW - ago), "next_at": "2026-01-01T00:00:00"}}}


def test_stale_loop_restarted_once(loops, emitted):
    st = _st("review", 4 * 3600)
    rows = steward.fix_loops(st, NOW)
    assert ("kill-window", "-t", "@5") == loops[-1][:3] and rows[0]["action"] == "restart"
    assert [e[2]["loop"] for e in emitted if e[0] == "steward"] == ["review"]
    assert steward.fix_loops(st, NOW) == []


def test_fresh_loop_left_alone(loops, emitted):
    assert steward.fix_loops(_st("review", 3600), NOW) == []


def test_disabled_loop_ignored(loops, emitted):
    C.SERVICES["review"]["enabled"] = False
    assert steward.fix_loops(_st("review", 4 * 3600), NOW) == []


def test_scheduled_loop_gets_run_now(loops, emitted):
    st = _st("daily", 4 * 3600)
    steward.fix_loops(st, NOW)
    s = st["services"]["daily"]
    assert s["run_now"] is True and "next_at" not in s and not any(a[0] == "kill-window" for a in loops)


def test_auto_merge_stale_gets_run_now(loops, emitted):
    C.AUTO_MERGE = {"enabled": True, "every": "15m"}
    st = _st("auto-merge", 3 * 3600)
    steward.fix_loops(st, NOW)
    assert st["services"]["auto-merge"]["run_now"] is True and "next_at" not in st["services"]["auto-merge"]


from pl import agents, commands  # noqa: E402


@pytest.fixture
def acts(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "retry", lambda ref, stage=None: calls.append(("retry", ref)) or [])
    monkeypatch.setattr(commands, "unhold", lambda ref: calls.append(("unhold", ref)) or [])
    monkeypatch.setattr(commands, "update", lambda cid, **k: calls.append(("update", cid, k)))
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    return calls


def _dead(st, n=1, now=NOW):
    return [steward.fix_dead({"id": "o/r#1"}, st, now + i) for i in range(n)]


def test_dead_card_retried(acts):
    assert _dead({}) == ["retry"] and acts == [("retry", "o/r#1")]


def test_third_death_escalates_not_retries(acts):
    assert _dead({}, 3) == ["retry", "retry", "needs you"] and len(acts) == 2


def test_retry_count_resets_after_a_day(acts):
    st = {}
    _dead(st, 2)
    assert steward.fix_dead({"id": "o/r#1"}, st, NOW + 86401 + 2) == "retry"


def _held(reason="waiting on o/r#9", deps=("o/r#9",)):
    return {"id": "o/r#1", "tags": ["parked"], "list_id": "Approved",
            "metadata": {"depends_on": list(deps), "hold_reason": reason}}


def test_held_card_with_merged_deps_unheld(acts, monkeypatch):
    monkeypatch.setattr(agents, "deps_open", lambda c, cs: [])
    assert steward.fix_held(_held(), []) == "unhold" and acts == [("unhold", "o/r#1")]


def test_held_card_with_open_dep_kept(acts, monkeypatch):
    monkeypatch.setattr(agents, "deps_open", lambda c, cs: ["o/r#9"])
    assert steward.fix_held(_held(), [{"id": "o/r#9", "list_id": "In progress"}]) is None and acts == []


def test_stale_hold_reason_replaced_and_logged(acts, monkeypatch, emitted):
    monkeypatch.setattr(agents, "deps_open", lambda c, cs: ["o/r#8"])
    others = [{"id": "o/r#9", "list_id": "Done"}, {"id": "o/r#8", "list_id": "In progress"}]
    assert steward.fix_held(_held(deps=("o/r#9", "o/r#8")), others) == "refresh reason"
    assert acts == [("update", "o/r#1", {"metadata": {"hold_reason": "waiting on r#8"}})]
    assert emitted[-1][2]["old_reason"] == "waiting on o/r#9" and emitted[-1][2]["new_reason"] == "waiting on r#8"


def test_held_card_without_deps_left_alone(acts, monkeypatch):
    monkeypatch.setattr(agents, "deps_open", lambda c, cs: [])
    assert steward.fix_held(_held(deps=()), []) is None and acts == []
