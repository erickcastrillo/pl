"""The open watchdog alerts as plain "Needs you" rows, with what pl tried (Plan 54 slice 6)."""
import pytest

from pl import alerts
from pl import config as C


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(alerts, "_offer", lambda *a: None)
    monkeypatch.setattr(alerts, "_urgent", lambda on: None)
    monkeypatch.setattr(alerts, "_logged", lambda *a, **k: None)
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    return t


def test_needs_you_lists_an_open_stalled_alert_with_age_and_tried(state):
    alerts.open("stalled:o/r#1", "warn", "t", "f", cards=["o/r#1"], tried="nudged")
    state["now"] += 720
    [row] = alerts.needs_you()
    assert (row["card"], row["kind"], row["age_seconds"], row["tried"]) == ("o/r#1", "stalled", 720, "nudged")


def test_each_watchdog_kind_has_its_tried_default():
    for kind, title in [("stalled", "Card r#1: idle"), ("reconcile_failed", "Card r#2: pl could not move the card to PR open"),
                        ("queued_write_failed", "x"), ("rework_waiting", "Card r#4: waited 17 min for a slot"),
                        ("worktree_removed", "x")]:
        alerts.open(f"{kind}:o/r#9", "warn", title, "f", cards=["o/r#9"])
    got = {r["kind"]: r["tried"] for r in alerts.needs_you()}
    assert got == {"stalled": "nothing yet", "reconcile_failed": "move the card to PR open",
                   "queued_write_failed": "queued, replay failed", "rework_waiting": "waited 17 min for a slot",
                   "worktree_removed": "removed outside pl"}


def test_other_alert_kinds_are_not_listed():
    alerts.open("loop_failed:x", "warn", "t", "f")
    alerts.open("connector_signin:y", "warn", "t", "f")
    assert alerts.needs_you() == []


def test_a_resolved_alert_leaves_the_list():
    alerts.open("stalled:o/r#1", "warn", "t", "f", cards=["o/r#1"])
    alerts.resolve("stalled:o/r#1")
    assert alerts.needs_you() == []


def test_an_acked_alert_is_listed_and_marked_acked():
    alerts.open("stalled:o/r#1", "warn", "t", "f", cards=["o/r#1"])
    alerts.ack("stalled:o/r#1")
    assert [r["acked"] for r in alerts.needs_you()] == [True]


def test_a_row_carries_no_title_or_fix():
    alerts.open("stalled:o/r#1", "warn", "SECRET-TITLE", "SECRET-FIX", cards=["o/r#1"])
    assert "SECRET" not in repr(alerts.needs_you())


def test_tried_update_keeps_the_ack():
    alerts.open("stalled:o/r#1", "warn", "t", "f", cards=["o/r#1"])
    alerts.ack("stalled:o/r#1")
    alerts.open("stalled:o/r#1", "warn", "t", "f", cards=["o/r#1"], tried="nudged")
    assert alerts.get("stalled:o/r#1")["acked"] and alerts.needs_you()[0]["tried"] == "nudged"
