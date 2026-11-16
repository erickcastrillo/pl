"""needs-you items: one item and one alert per card and reason, one notification, a daily digest, a late push."""
from datetime import datetime

import pytest

from pl import alerts, dispatch, inbox, notifications, steward
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
    monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    monkeypatch.setattr(alerts, "_offer", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 11, 16, 8, 0))
    yield
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notifications, "send", lambda event, title, msg: out.append((event, title, msg)) or True)
    return out


def _row(card="o/r#1", reason="waiting_owner", line="PR needs a person", action="none", column="PR open"):
    return {"card": card, "column": column, "reason": reason, "line": line, "action": action, "by": "rule"}


def _pass(rows, st, now=NOW):
    steward.items_update(rows, st, now)
    steward.notify_items(st, now)


def _keys():
    return [a["key"] for a in alerts.listing()]


def test_waiting_owner_opens_one_item_and_alert(sent):
    st = {}
    _pass([_row()], st)
    assert list(st["steward"]["items"]) == ["o/r#1"] and _keys() == ["steward:o/r#1:waiting_owner"]
    assert len(sent) == 1


def test_same_reason_next_pass_no_new_alert_or_notify(sent):
    st = {}
    _pass([_row()], st)
    _pass([_row()], st, NOW + 1800)
    assert _keys() == ["steward:o/r#1:waiting_owner"] and len(sent) == 1


def test_reason_change_replaces_item(sent):
    st = {}
    _pass([_row()], st)
    _pass([_row(reason="blocked", line="size limit (max_lines)")], st, NOW + 1800)
    assert _keys() == ["steward:o/r#1:blocked"] and st["steward"]["items"]["o/r#1"]["reason"] == "blocked"
    assert len(sent) == 2


def test_card_moved_clears_item_and_resolves_alert(sent):
    st = {}
    _pass([_row()], st)
    _pass([], st, NOW + 1800)
    assert st["steward"]["items"] == {} and _keys() == []


def test_existing_pr_person_alert_is_reused_not_duplicated(sent):
    alerts.open("pr_person:o/r#1", "warn", "t", "f", cards=["o/r#1"])
    st = {}
    _pass([_row()], st)
    assert _keys() == ["pr_person:o/r#1"] and st["steward"]["items"]["o/r#1"]["attached_to"] == "pr_person:o/r#1"
    assert alerts.get("pr_person:o/r#1")["tried"] == "steward: waiting_owner"


def test_fixed_rows_make_no_item(sent):
    st = {}
    _pass([_row(action="retry", reason="dead"), _row(card="o/r#2", reason="slow")], st)
    assert st["steward"]["items"] == {} and sent == []


def _local(monkeypatch, hour):
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 11, 16, hour, 30))


def test_digest_once_per_day_after_digest_at(sent, monkeypatch):
    st = {}
    _pass([_row()], st)
    sent.clear()
    _local(monkeypatch, 8)
    steward.notify_items(st, NOW)
    assert sent == []
    _local(monkeypatch, 10)
    steward.notify_items(st, NOW)
    steward.notify_items(st, NOW)
    assert [s[0] for s in sent] == ["alert_opened"] and sent[0][1] == "1 cards need you"


def test_no_digest_without_items(sent, monkeypatch):
    _local(monkeypatch, 10)
    steward.notify_items({}, NOW)
    assert sent == []


def test_push_after_4h_once_per_item(sent, monkeypatch):
    _local(monkeypatch, 8)
    st = {}
    _pass([_row()], st)
    sent.clear()
    steward.notify_items(st, NOW + 3 * 3600)
    assert sent == []
    steward.notify_items(st, NOW + 5 * 3600)
    steward.notify_items(st, NOW + 6 * 3600)
    assert len(sent) == 1 and sent[0][0] == "alert_opened"


def test_new_item_notifies_once(sent):
    st = {}
    _pass([_row()], st)
    steward.notify_items(st, NOW)
    assert len(sent) == 1 and sent[0][2] == "PR needs a person"


def test_inbox_shows_needs_you_line(monkeypatch, capsys):
    st = {}
    steward.items_update([_row()], st, NOW)
    monkeypatch.setattr(inbox, "steward_items", lambda: st["steward"]["items"])
    monkeypatch.setattr(inbox, "cards", lambda: [])
    monkeypatch.setattr(inbox.profiles, "list_profiles", lambda check_lock=False: [])
    inbox.cmd_inbox(type("A", (), {"json": False})())
    assert "needs you: r#1 PR needs a person" in capsys.readouterr().out
