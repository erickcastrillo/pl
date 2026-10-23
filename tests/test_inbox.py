"""Tests for pl inbox across profiles (WP1.1 and WP1.2)."""
import json
import stat
import time

import pytest

from pl import config as C
from pl.tui.needs import review_kind


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-p1"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("p1")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(cid, title, col, pipeline_mode="auto", **meta):
    return {
        "id": cid,
        "title": title,
        "list_id": f"col-{col}",
        "updated_at": "2026-10-01T12:00:00+00:00",
        "metadata": {"pipeline_mode": pipeline_mode, **meta},
    }


def test_gate_of_agrees_with_needs_groups(monkeypatch):
    from pl.inbox import gate_of

    monkeypatch.setattr(C, "GATES", {"spec": True})

    # Auto card in Spec ready waiting for review
    c_spec = _card("c1", "Spec Card", "Spec ready")
    r_spec = {"card": c_spec, "col": "Spec ready", "kind": "row"}
    assert gate_of(c_spec, "Spec ready") == "spec"
    assert review_kind(r_spec) == "spec"

    # When spec gate is off
    monkeypatch.setattr(C, "GATES", {"spec": False})
    assert gate_of(c_spec, "Spec ready") is None
    assert review_kind(r_spec) is None

    # When spec is already approved
    monkeypatch.setattr(C, "GATES", {"spec": True})
    c_spec_app = _card("c1", "Spec Card", "Spec ready", spec_approved_at="2026-10-01T13:00:00+00:00")
    r_spec_app = {"card": c_spec_app, "col": "Spec ready", "kind": "row"}
    assert gate_of(c_spec_app, "Spec ready") is None
    assert review_kind(r_spec_app) is None

    # Card in Plan for review
    c_plan = _card("c2", "Plan Card", "Plan for review")
    r_plan = {"card": c_plan, "col": "Plan for review", "kind": "review"}
    assert gate_of(c_plan, "Plan for review") == "plan"
    assert review_kind(r_plan) == "plan"

    # Card not in auto mode
    c_manual = _card("c3", "Manual Card", "Plan for review", pipeline_mode="manual")
    r_manual = {"card": c_manual, "col": "Plan for review", "kind": "row"}
    assert gate_of(c_manual, "Plan for review") is None
    assert review_kind(r_manual) is None

    # Card in other columns
    for col in ("Inbox", "Approved", "In progress", "PR open", "Done"):
        c = _card("c4", "Other Card", col)
        assert gate_of(c, col) is None


def test_two_profiles_listed_oldest_first_with_pipeline_card_gate_summary_age(fake_home, monkeypatch):
    from pl.inbox import listing

    # Current profile is p1
    C.PROFILE_NAME = "p1"
    c_p1 = _card("repo#10", "A gate in profile 1", "Plan for review")
    c_p1["updated_at"] = "2026-10-10T10:00:00+00:00"

    monkeypatch.setattr("pl.inbox.cards", lambda: [c_p1])
    monkeypatch.setattr("pl.inbox.col_name", lambda lid: lid.removeprefix("col-"))
    monkeypatch.setattr("pl.inbox.card_url", lambda cid: f"https://example.test/{cid}")

    # Profile 2 is on disk
    d2 = fake_home / ".pl-p2"
    d2.mkdir()
    (d2 / "config.toml").write_text("")
    (d2 / "state").mkdir()

    item_p2 = {
        "pipeline": "p2",
        "card": "repo#5",
        "gate": "spec",
        "summary": "Older gate in profile 2",
        "since": "2026-10-01T10:00:00+00:00",
        "age_seconds": 900000,
        "url": "https://example.test/repo#5",
        "command": "pl --profile p2 approve 'repo#5'",
    }
    (d2 / "state" / "inbox.json").write_text(json.dumps({"at": time.time(), "items": [item_p2]}))

    items = listing()
    assert len(items) == 2
    # Oldest first: item from p2 (since 2026-10-01) should precede item from p1 (since 2026-10-10)
    assert items[0]["pipeline"] == "p2"
    assert items[0]["card"] == "repo#5"
    assert items[0]["gate"] == "spec"
    assert items[0]["summary"] == "Older gate in profile 2"
    assert items[0]["age_seconds"] >= 900000

    assert items[1]["pipeline"] == "p1"
    assert items[1]["card"] == "repo#10"
    assert items[1]["gate"] == "plan"
    assert items[1]["summary"] == "A gate in profile 1"
    assert items[1]["command"] == "pl --profile p1 approve 'repo#10'"


def test_a_stale_or_broken_inbox_file_is_marked_not_fatal(fake_home, monkeypatch):
    from pl import manager
    from pl.inbox import listing

    C.PROFILE_NAME = "p1"
    monkeypatch.setattr("pl.inbox.cards", lambda: [])
    monkeypatch.setattr("pl.inbox.col_name", lambda lid: lid.removeprefix("col-"))

    # Profile 2 has a stale inbox file
    d2 = fake_home / ".pl-p2"
    d2.mkdir()
    (d2 / "config.toml").write_text("")
    (d2 / "state").mkdir()
    item_stale = {
        "pipeline": "p2",
        "card": "repo#5",
        "gate": "plan",
        "summary": "Stale item",
        "since": "2026-10-01T10:00:00+00:00",
        "age_seconds": 5000,
        "url": "https://example.test/repo#5",
        "command": "pl --profile p2 approve 'repo#5'",
    }
    stale_at = time.time() - (manager.WAITS_FRESH + 100)
    (d2 / "state" / "inbox.json").write_text(json.dumps({"at": stale_at, "items": [item_stale]}))

    # Profile 3 has a broken inbox file
    d3 = fake_home / ".pl-p3"
    d3.mkdir()
    (d3 / "config.toml").write_text("")
    (d3 / "state").mkdir()
    (d3 / "state" / "inbox.json").write_text("{broken json")

    items = listing()
    # Profile 3 didn't crash listing()
    assert len(items) == 1
    assert items[0]["card"] == "repo#5"
    assert items[0]["stale"] is True


def test_dispatch_pass_writes_inbox_json_0600(fake_home, monkeypatch):
    from pl import dispatch

    c_waiting = _card("repo#20", "Needs plan review", "Plan for review")
    monkeypatch.setattr(dispatch, "cards", lambda: [c_waiting])
    monkeypatch.setattr("pl.board.col_name", lambda lid: lid.removeprefix("col-"))
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid.removeprefix("col-"))
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "paused", lambda: False)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr("pl.accounts.check_parked", lambda: None)
    monkeypatch.setattr("pl.memory.guard_runaways", lambda *a, **k: None)
    monkeypatch.setattr("pl.memory.check_starts", lambda *a, **k: None)
    monkeypatch.setattr("pl.manager.read_status", lambda: None)
    monkeypatch.setattr("pl.manager.room_for", lambda *a, **k: 1)

    st = {"notified": {}, "run_waits": {}, "screens": {}}
    dispatch.dispatch_once(st, dry=False)

    inbox_file = C.STATE_DIR / "inbox.json"
    assert inbox_file.is_file()
    # Mode is 0600
    assert stat.S_IMODE(inbox_file.stat().st_mode) == 0o600
    data = json.loads(inbox_file.read_text())
    assert "at" in data
    assert isinstance(data["at"], (int, float))
    assert len(data["items"]) == 1
    assert data["items"][0]["card"] == "repo#20"
    assert data["items"][0]["gate"] == "plan"
