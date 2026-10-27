"""Plan 51 slice 4: [notify] quiet holds notifications inside a local-time window and sends one summary after it."""
import json
from datetime import datetime

import pytest

from pl import alerts, dispatch, notifications, util
from pl import config as C
from pl.chat import gates
from pl.manager import machine_dir
from test_events import _pass
from test_notifications import FakeAdapter, _connectors, fake_home  # noqa: F401 - fake_home is the autouse fixture


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "notify", lambda title, message: calls.append((title, message)))
    return calls


@pytest.fixture
def clock(monkeypatch):
    t = {"now": datetime(2026, 10, 26, 23, 0), "mono": 100.0}
    monkeypatch.setattr(notifications, "_now", lambda: t["now"])
    monkeypatch.setattr(notifications, "_mono", lambda: t["mono"])
    monkeypatch.setitem(notifications._URGENT, "at", None)

    def at(hhmm):
        h, m = hhmm.split(":")
        t["now"] = t["now"].replace(hour=int(h), minute=int(m))
        t["mono"] += 3600
    return at


@pytest.fixture
def quiet(clock):
    C.NOTIFY = {**C.NOTIFY, "quiet": "22:00-07:00"}
    return clock


def held_path():
    return C.STATE_DIR / "notify-held.jsonl"


def held_lines():
    return [json.loads(ln) for ln in held_path().read_text().splitlines()] if held_path().exists() else []


def test_pr_opened_at_23_is_held_not_sent(quiet, desktop):
    quiet("23:00")
    notifications.send("pr_opened", "PR opened: card0001 #7", "Add a thing")
    assert desktop == []
    assert not (machine_dir() / "chat-outbox.jsonl").exists()
    assert held_lines() == [{"at": "23:00", "title": "PR opened: card0001 #7", "message": "Add a thing"}]
    assert held_path().stat().st_mode & 0o777 == 0o600


def test_release_after_window_sends_one_summary_listing_every_event(quiet, desktop, monkeypatch):
    _connectors(slack=["alerts"])
    fake = FakeAdapter("slack")
    monkeypatch.setattr("pl.chat.adapter", lambda n: fake)
    quiet("23:00")
    notifications.send("pr_opened", "PR opened: card0001 #7", "First")
    notifications.send("merged", "Merged: card0002 #8", "Second")
    quiet("07:01")
    notifications.release()
    assert len(desktop) == 1
    title, message = desktop[0]
    assert title == "Quiet hours: 2 held"
    assert "PR opened: card0001 #7" in message and "Merged: card0002 #8" in message
    gates.post_outbox()
    assert len(fake.sent) == 1
    assert not held_path().exists()


def test_second_release_sends_nothing(quiet, desktop):
    quiet("23:00")
    notifications.send("done", "Done: card0001", "x")
    quiet("07:01")
    notifications.release()
    quiet("07:02")
    notifications.release()
    assert len(desktop) == 1


def test_release_with_nothing_held_sends_nothing(quiet, desktop):
    quiet("07:01")
    notifications.release()
    assert desktop == []
    assert not (machine_dir() / "chat-outbox.jsonl").exists()


def test_release_inside_the_window_keeps_the_file(quiet, desktop):
    quiet("23:00")
    notifications.send("done", "Done: card0001", "x")
    quiet("23:30")
    notifications.release()
    assert desktop == []
    assert len(held_lines()) == 1


def test_day_window_holds_at_noon_sends_at_18(clock, desktop):
    C.NOTIFY = {**C.NOTIFY, "quiet": "09:00-17:00"}
    clock("12:00")
    notifications.send("done", "Done: card0001", "x")
    assert desktop == [] and len(held_lines()) == 1
    clock("18:00")
    notifications.send("done", "Done: card0002", "y")
    assert desktop == [("Done: card0002", "y")]


@pytest.mark.parametrize("value", [None, ""])
def test_no_quiet_key_or_empty_sends_at_once(clock, desktop, value):
    if value is not None:
        C.NOTIFY = {**C.NOTIFY, "quiet": value}
    clock("23:00")
    notifications.send("done", "Done: card0001", "x")
    assert desktop == [("Done: card0001", "x")]
    assert not held_path().exists()


@pytest.mark.parametrize("value", ["10pm-7am", "25:00-07:00", "22:00", "07:00-07:00", 7])
def test_bad_quiet_value_sends_at_once(clock, desktop, value):
    C.NOTIFY = {**C.NOTIFY, "quiet": value}
    clock("23:00")
    notifications.send("done", "Done: card0001", "x")
    assert desktop == [("Done: card0001", "x")]
    assert notifications.quiet_problem() and "notify.quiet" in notifications.quiet_problem()


def test_summary_cuts_after_20_lines(quiet, desktop):
    quiet("23:00")
    for n in range(23):
        notifications.send("done", f"Done: card{n:04}", "x")
    quiet("07:01")
    notifications.release()
    lines = desktop[0][1].splitlines()
    assert len(lines) == 21 and lines[-1] == "and 3 more"
    assert desktop[0][0] == "Quiet hours: 23 held"


def test_held_text_is_masked(quiet, desktop):
    quiet("23:00")
    notifications.send("done", "Done: card0001", "token ghp_" + "a" * 36)
    assert "ghp_" not in held_path().read_text()


def test_unwritable_hold_sends_instead(quiet, desktop, monkeypatch):
    quiet("23:00")
    monkeypatch.setattr(notifications.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert notifications.hold("t", "m") is False


@pytest.fixture
def attention(tmp_path, monkeypatch):
    script = tmp_path / "attention"
    script.write_text("")
    C.ATTENTION = script
    calls = []
    monkeypatch.setattr(util.subprocess, "run", lambda argv, **k: calls.append(argv))
    return calls


def test_util_notify_is_held_inside_the_window(quiet, attention):
    quiet("23:00")
    util.notify("Runaway agent: x", "msg")
    assert attention == [] and len(held_lines()) == 1
    quiet("12:00")
    util.notify("Runaway agent: x", "msg")
    assert len(attention) == 1


def test_high_alert_opened_at_23_is_sent_and_not_held(quiet, attention):
    quiet("23:00")
    alerts.open("memory_low", "high", "Memory low", "free some")
    util.notify("Memory low", "msg")
    assert len(attention) == 1
    assert not held_path().exists()
    assert "Memory low" in (machine_dir() / "chat-outbox.jsonl").read_text()


def test_warn_alert_opened_at_23_is_held(quiet, attention):
    quiet("23:00")
    alerts.open("memory_low", "warn", "Near cap", "wait")
    util.notify("Near cap", "msg")
    assert attention == [] and len(held_lines()) == 1
    assert not (machine_dir() / "chat-outbox.jsonl").exists()


def test_urgent_mark_does_not_outlive_the_next_open(quiet, attention):
    quiet("23:00")
    alerts.open("memory_low", "high", "Memory low", "free some")
    alerts.open("memory_low", "warn", "Near cap", "wait")
    util.notify("Near cap", "msg")
    assert attention == [] and len(held_lines()) == 1


def test_urgent_mark_expires_after_two_seconds(quiet, attention):
    quiet("23:00")
    alerts.open("memory_low", "high", "Memory low", "free some")
    quiet("23:00")
    util.notify("late", "msg")
    assert attention == [] and len(held_lines()) == 1


def test_dispatcher_pass_after_the_window_sends_the_summary(quiet, desktop, monkeypatch):
    quiet("23:00")
    notifications.send("done", "Done: card0001", "x")
    notifications.send("done", "Done: card0002", "y")
    quiet("07:01")
    _pass(monkeypatch, {"now": "Inbox"})
    assert len(desktop) == 1 and desktop[0][0] == "Quiet hours: 2 held"
    quiet("07:02")
    _pass(monkeypatch, {"now": "Inbox"})
    assert len(desktop) == 1


def test_dry_pass_keeps_the_held_file(quiet, desktop, monkeypatch):
    quiet("23:00")
    notifications.send("done", "Done: card0001", "x")
    quiet("07:01")
    for name, fake in (("registry", lambda: {}), ("cards", lambda: []), ("ensure_services", lambda *a, **k: None),
                       ("sweep_untracked", lambda *a, **k: None), ("tmux", lambda *a, **k: "")):
        monkeypatch.setattr(dispatch, name, fake)
    monkeypatch.setattr(dispatch.rework, "sweep", lambda *a, **k: None)
    dispatch.dispatch_once(1, True, pull=False)
    assert desktop == [] and len(held_lines()) == 1
