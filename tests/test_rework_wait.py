"""Merge-only reworks start before new builds and normal reworks; a rework that waits too long for a run slot
warns once and opens an alert."""
import time
import types

import pytest

from pl import alerts, dispatch, events, move_agent, rework
from pl import config as C

NOW = 1_800_000_000.0


def _iso(ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - ago * 60))


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"acme": tmp_path / ".claude"}
    C.ACCOUNTS = {"acme": {"harness": "claude"}}
    monkeypatch.setattr(C, "WATCHDOG", {}, raising=False)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(rework, "col_name", lambda lid: lid)
    monkeypatch.setattr(time, "time", lambda: NOW)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake(monkeypatch):
    ns = types.SimpleNamespace(started=[], events=[], sent=[])
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("dead", None))
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: ns.started.append(c["id"]))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: ns.sent.append(a))
    monkeypatch.setattr(rework, "sweep", lambda *a, **k: [])
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))
    monkeypatch.setattr(rework.events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))

    def run(cs, max_runs=1):
        monkeypatch.setattr(dispatch, "cards", lambda: cs)
        dispatch.dispatch_once(max_runs, False, pull=False)
    ns.run = run
    return ns


def _approved(cid, updated="2026-09-01"):
    return {"id": cid, "title": f"card {cid}", "list_id": "Approved", "tags": [], "updated_at": updated,
            "description": "# PIPELINE: PLAN\nWP1",
            "metadata": {"pipeline_mode": "auto", "profile": "acme"}}


def _reworking(cid, ago, merge_only=True, updated="2026-09-02", **meta):
    return {"id": cid, "title": f"card {cid}", "list_id": "In progress", "tags": [], "updated_at": updated,
            "description": "# PIPELINE: PLAN\nWP1",
            "metadata": {"pipeline_mode": "auto", "profile": "acme",
                         "rework": {"round": 1, "active": True, "merge_only": merge_only, "at": _iso(ago)}, **meta}}


def test_merge_only_rework_starts_before_older_new_build(fake):
    fake.run([_approved("new"), _reworking("mo", 5)])
    assert fake.started == ["mo"]


def test_longest_waiting_merge_only_starts_first(fake):
    fake.run([_reworking("late", 5, updated="2026-08-01"), _reworking("early", 10, updated="2026-09-09")])
    assert fake.started == ["early"]


def test_restarted_run_keeps_its_place(fake):
    dead = _reworking("dead", 50, merge_only=False, worker={"stage": "run", "attempts": 1, "session_id": "s1"})
    fake.run([_reworking("mo", 5), dead])
    assert fake.started == ["dead"]


def _waiting_pass(fake, cs):
    fake.run(cs, max_runs=0)


def _alert_events(fake):
    return [e for e in fake.events if e[0] == "rework_waiting"]


def test_rework_waiting_over_limit_warns_once_and_opens_alert(fake):
    c = _reworking("c1", 31)
    _waiting_pass(fake, [c])
    _waiting_pass(fake, [c])
    assert [e[2]["minutes"] for e in _alert_events(fake)] == [31]
    assert alerts.get("rework_waiting:c1") and not alerts.get("rework_waiting:c1").get("resolved_at")
    assert len(fake.sent) == 1
    assert "rework_waiting" in events.WARNING_KINDS


def test_rework_wait_alert_resolves_when_agent_starts(fake):
    c = _reworking("c1", 31)
    _waiting_pass(fake, [c])
    fake.run([c], max_runs=1)
    assert fake.started == ["c1"]
    assert alerts.get("rework_waiting:c1").get("resolved_at")
    _waiting_pass(fake, [c])
    assert len(_alert_events(fake)) == 1


def test_rework_wait_alert_resolves_when_card_leaves_in_progress(fake):
    c = _reworking("c1", 31)
    _waiting_pass(fake, [c])
    _waiting_pass(fake, [{**c, "list_id": "PR open"}])
    assert alerts.get("rework_waiting:c1").get("resolved_at")


def test_a_new_rework_round_counts_as_a_new_wait(fake):
    _waiting_pass(fake, [_reworking("c1", 40)])
    _waiting_pass(fake, [_reworking("c1", 31)])
    assert len(_alert_events(fake)) == 2


def test_rework_wait_under_limit_is_quiet(fake):
    _waiting_pass(fake, [_reworking("c1", 20)])
    assert _alert_events(fake) == [] and alerts.get("rework_waiting:c1") is None


def test_rework_wait_minutes_setting_is_read(fake, monkeypatch):
    monkeypatch.setattr(C, "WATCHDOG", {"rework_wait_minutes": 5})
    _waiting_pass(fake, [_reworking("c1", 6)])
    assert alerts.get("rework_waiting:c1")
