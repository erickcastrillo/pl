"""Anomaly alerts: unknown chat user, send spike, sign-in from a new host."""
import json
import time

import pytest

from datetime import datetime, timezone

from pl import alerts, audit, connectors_proxy, dispatch
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class _Sent(list):
    direct = None


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    out, direct = _Sent(), []
    monkeypatch.setattr("pl.notifications.notify", lambda t, m: out.append((t, m)))
    monkeypatch.setattr(dispatch, "notify", lambda t, m: direct.append((t, m)))
    out.direct = direct
    return out


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    return t


def _write_audit(entry):
    p = C.STATE_DIR / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _write_machine_audit(entry):
    p = audit.manager.machine_dir() / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _unknown_sender(now, sender="U1"):
    return {"kind": audit.CHAT_UNKNOWN_SENDER, "connector": "slack", "sender_id": sender, "ts": _iso(now)}


def _write_call(when, access="write"):
    return {"action": audit.CONNECTOR_TOOL, "connector": "slack", "access": access, "when": when}


def test_an_unknown_chat_user_opens_an_alert(clock):
    _write_machine_audit(_unknown_sender(clock["now"]))
    dispatch.check_alerts([], set())
    a = alerts.get("chat_unknown:slack")
    assert a is not None
    assert "Unknown chat user in slack" in a["title"]


def test_two_unknown_senders_on_one_channel_open_one_alert(clock, sent):
    _write_machine_audit(_unknown_sender(clock["now"], "U1"))
    _write_machine_audit(_unknown_sender(clock["now"], "U2"))
    dispatch.check_alerts([], set())
    assert alerts.get("chat_unknown:slack") is not None
    assert len(sent) == 1


def test_slack_adapter_unknown_sender_raises_the_alert(clock, monkeypatch):
    from pl.chat.slack import Slack

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setattr("pl.chat.slack._http", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("pl.chat.inbound._pl", lambda *a, **k: None)
    clock["now"] = time.time()
    slack = Slack("slack", {"token": "${SLACK_BOT_TOKEN}", "app_token": "${SLACK_APP_TOKEN}", "allow": ["U12345"]})

    class Ws:
        def send(self, data):
            pass

    envelope = {"envelope_id": "e1", "type": "interactive", "payload": {
        "type": "block_actions", "user": {"id": "U_UNKNOWN"},
        "actions": [{"action_id": "approve:pl:pl#44", "value": "approve:pl:pl#44"}]}}
    slack.handle_envelope(envelope, ws=Ws())
    dispatch.check_alerts([], set())
    assert alerts.get("chat_unknown:slack") is not None


def test_more_than_three_times_the_7_day_hourly_average_opens_a_send_spike(clock):
    now = clock["now"]
    for h in range(1, 168, 2):
        _write_audit(_write_call(now - h * 3600))
    for _ in range(4):
        _write_audit(_write_call(now - 60))
    dispatch.check_alerts([], set())
    a = alerts.get("send_spike:slack")
    assert a is not None
    assert "Send spike on slack" in a["title"]


def test_a_read_only_burst_opens_no_send_spike(clock):
    now = clock["now"]
    for h in range(1, 168, 2):
        _write_audit(_write_call(now - h * 3600, access="read"))
    for _ in range(4):
        _write_audit(_write_call(now - 60, access="read"))
    dispatch.check_alerts([], set())
    assert alerts.get("send_spike:slack") is None


def test_proxy_write_calls_raise_a_send_spike(clock):
    clock["now"] = time.time()
    for _ in range(5):
        connectors_proxy.record_tool_call("slack", "post", access="write")
    dispatch.check_alerts([], set())
    assert alerts.get("send_spike:slack") is not None


def test_a_steady_rate_opens_nothing(clock):
    now = clock["now"]
    for h in range(168):
        for _ in range(4):
            _write_audit(_write_call(now - h * 3600 + 10))
    dispatch.check_alerts([], set())
    assert alerts.get("send_spike:slack") is None


def test_a_sign_in_from_a_new_host_opens_an_alert(clock):
    now = clock["now"]
    _write_audit({"action": "oauth.sign_in", "connector": "github", "host": "office-laptop", "when": now - 3600 * 24})
    _write_audit({"action": "oauth.sign_in", "connector": "github", "host": "foreign-vps", "when": now - 60})
    dispatch.check_alerts([], set())
    a = alerts.get("connector_new_place:github")
    assert a is not None
    assert "foreign-vps" in a["title"]


def test_connectors_oauth_records_sign_in_and_token(clock):
    from pl import connectors_oauth
    connectors_oauth.record_sign_in("slack", host="host-a")
    connectors_oauth.record_token_use("slack", host="host-b")
    p = C.STATE_DIR / "audit.jsonl"
    lines = [json.loads(line) for line in p.read_text().splitlines() if line]
    assert len(lines) == 2
    assert lines[0]["connector"] == "slack" and lines[0]["host"] == "host-a"
    assert lines[1]["connector"] == "slack" and lines[1]["host"] == "host-b"


def test_an_old_chat_event_does_not_reopen_a_closed_alert(clock):
    st = {}
    _write_machine_audit(_unknown_sender(clock["now"] - 10))
    dispatch._check_anomaly_alerts(clock["now"], st)
    assert alerts.get("chat_unknown:slack") is not None
    alerts.resolve("chat_unknown:slack")
    clock["now"] += 600
    dispatch._check_anomaly_alerts(clock["now"], st)
    a = alerts.get("chat_unknown:slack")
    assert a is None or a.get("resolved_at") or a.get("status") == "resolved"


def test_reading_the_machine_log_leaves_the_profile_chain_verified(clock):
    audit.append("card.move", card="x#1")
    audit.append("card.move", card="x#2")
    _write_machine_audit(_unknown_sender(clock["now"]))
    dispatch.check_alerts([], set())
    assert audit.verify()
    assert [e["kind"] for e in audit.read_machine_entries()] == [audit.CHAT_UNKNOWN_SENDER]
    assert [e["action"] for e in audit.read_entries()] == ["card.move", "card.move"]


def _spike(now):
    for h in range(1, 168, 2):
        _write_audit(_write_call(now - h * 3600))
    for _ in range(4):
        _write_audit(_write_call(now - 60))


def _new_place(now):
    _write_audit({"action": "oauth.sign_in", "connector": "github", "host": "office-laptop", "when": now - 3600 * 24})
    _write_audit({"action": "oauth.sign_in", "connector": "github", "host": "foreign-vps", "when": now - 60})


def _unknown(now):
    _write_machine_audit(_unknown_sender(now))


def test_an_unknown_chat_user_sends_one_alert_opened(clock, sent):
    _unknown(clock["now"])
    dispatch.check_alerts([], set())
    assert len(sent) == 1 and "Unknown chat user in slack" in sent[0][0]
    assert sent.direct == []


def test_a_send_spike_sends_one_alert_opened(clock, sent):
    _spike(clock["now"])
    dispatch.check_alerts([], set())
    assert len(sent) == 1 and "slack" in sent[0][0]
    assert sent.direct == []


def test_a_new_place_sign_in_sends_one_alert_opened(clock, sent):
    _new_place(clock["now"])
    dispatch.check_alerts([], set())
    assert len(sent) == 1 and "github" in sent[0][0]
    assert sent.direct == []


@pytest.mark.parametrize("write", [_unknown, _spike, _new_place])
def test_a_second_pass_on_an_open_anomaly_alert_sends_nothing(clock, sent, write):
    write(clock["now"])
    dispatch.check_alerts([], set())
    clock["now"] += 600
    dispatch.check_alerts([], set())
    assert len(sent) == 1


def test_alert_opened_off_sends_nothing_but_keeps_the_alert(clock, sent):
    C.NOTIFY = {"events": ["merged"]}
    _unknown(clock["now"])
    dispatch.check_alerts([], set())
    assert sent == [] and sent.direct == []
    assert not (audit.manager.machine_dir() / "chat-outbox.jsonl").exists()
    assert alerts.get("chat_unknown:slack") is not None
