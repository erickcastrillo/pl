"""The steward pass: when it runs, and the one reason it gives a stuck card."""
import pytest

from pl import agent_screen, alerts, commands, dispatch, events, steward
from pl import config as C

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(steward.inbox, "col_name", lambda lid: lid)
    monkeypatch.setattr(events, "_read", lambda: [])
    monkeypatch.setattr(commands, "retry", lambda ref, stage=None: [])
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
def screen(monkeypatch):
    s = {"text": "working"}
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: (s["text"], "working", ""))
    return s


@pytest.fixture
def status(monkeypatch):
    s = {"v": "alive"}
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: (s["v"], None))
    return s


def _card(col="In progress", minutes=300, worker=True, **meta):
    m = {"pipeline_mode": "auto", **meta}
    if worker:
        m["worker"] = {"stage": "run", "pane": "%1"}
    return {"id": "o/r#1", "title": "Secret title", "list_id": col, "description": "body",
            "updated_at": datetime_iso(NOW - minutes * 60), "metadata": m}


def datetime_iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _pass(cards, st=None):
    st = {} if st is None else st
    return steward.run_pass(cards, st, reg={}, now=NOW), st


def test_default_runs_every_30_minutes():
    assert steward.due({}, NOW)
    st = {"steward": {"ran_at": NOW}}
    assert not steward.due(st, NOW + 29 * 60)
    assert steward.due(st, NOW + 30 * 60)


def test_every_10m_setting():
    C.STEWARD = {**C.STEWARD, "every": "10m"}
    assert steward.due({"steward": {"ran_at": NOW}}, NOW + 10 * 60)


def test_disabled_never_runs():
    C.STEWARD = {**C.STEWARD, "enabled": False}
    assert not steward.due({}, NOW)


def test_under_threshold_no_reason(screen, status):
    rows, _ = _pass([_card(minutes=10)])
    assert rows == []


def test_plan_for_review_waits_owner_after_24h(screen, status):
    rows, _ = _pass([_card("Plan for review", minutes=1500, worker=False)])
    assert [r["reason"] for r in rows] == ["waiting_owner"]
    rows, _ = _pass([_card("Plan for review", minutes=1000, worker=False)])
    assert rows == []


def test_open_pr_person_alert_is_waiting_owner(screen, status):
    alerts.open("pr_person:o/r#1", "warn", "t", "f", cards=["o/r#1"])
    rows, _ = _pass([_card("PR open", minutes=300, worker=False)])
    assert rows[0]["reason"] == "waiting_owner"


def test_question_metadata_is_waiting_owner(screen, status):
    rows, _ = _pass([_card(question="which one?")])
    assert rows[0]["reason"] == "waiting_owner"


def test_unmerged_dep_is_blocked(screen, status, monkeypatch):
    monkeypatch.setattr(steward.agents, "deps_open", lambda c, cs: ["o/r#9"])
    rows, _ = _pass([_card(depends_on=["o/r#9"])])
    assert rows[0]["reason"] == "blocked"


def test_max_lines_screen_is_blocked(screen, status):
    screen["text"] = "the diff is over max_lines"
    rows, _ = _pass([_card()])
    assert rows[0]["reason"] == "blocked"


def test_dead_worker_is_dead(screen, status):
    status["v"] = "dead"
    rows, _ = _pass([_card()])
    assert rows[0]["reason"] == "dead"


def test_changing_screen_is_slow(screen, status):
    _, st = _pass([_card()])
    screen["text"] = "something new"
    rows, st = _pass([_card()], st)
    assert rows[0]["reason"] == "slow"


def test_threshold_override_from_config(screen, status):
    C.STEWARD = {**C.STEWARD, "thresholds": {"In progress": "400m"}}
    rows, _ = _pass([_card(minutes=300, question="q")])
    assert rows == []


def test_event_has_no_card_text(screen, status, emitted):
    screen["text"] = "SECRET screen over max_lines"
    _, st = _pass([_card()])
    [(kind, card, d)] = [e for e in emitted if e[0] == "steward"]
    assert card == "o/r#1" and d == {"reason": "blocked", "action": "none", "by": "rule"}
    assert "SECRET" not in repr(st) and st["steward"]["ran_at"] == NOW
