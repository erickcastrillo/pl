import json
import urllib.error
import pytest


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data) if isinstance(data, str) else data)


@pytest.fixture(autouse=True)
def slack_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret-bot-token-987")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret-app-token-654")


def test_gate_message_blocks_and_approve_action(monkeypatch):
    from pl.chat.slack import Slack

    http_calls = []

    def fake_http(token, method, payload=None):
        http_calls.append((token, method, payload))
        if method == "chat.postMessage":
            return {"ok": True, "ts": "12345.678", "channel": "C123"}
        if method == "chat.update":
            return {"ok": True, "ts": payload.get("ts"), "channel": payload.get("channel")}
        return {"ok": True}

    monkeypatch.setattr("pl.chat.slack._http", fake_http)

    pl_calls = []

    def fake_pl(argv, env=None):
        pl_calls.append((argv, env))
        class Res:
            returncode = 0
            stdout = "approved"
            stderr = ""
        return Res()

    monkeypatch.setattr("pl.chat.inbound._pl", fake_pl)

    slack = Slack("slack", {
        "token": "${SLACK_BOT_TOKEN}",
        "app_token": "${SLACK_APP_TOKEN}",
        "allow": ["U12345"],
    })

    # 1. Send gate message
    buttons = [
        ("Approve", "approve:pl:pl#44"),
        ("Edit", "https://github.com/your-org/your-repo/issues/44"),
        ("Send back", "reject:pl:pl#44"),
    ]
    res = slack.send("C123", "Plan 3 slice 9 gate message", buttons=buttons)
    assert res["ok"] is True
    assert len(http_calls) == 1
    token, method, payload = http_calls[0]
    assert token == "xoxb-secret-bot-token-987"
    assert method == "chat.postMessage"
    assert payload["channel"] == "C123"
    blocks = payload["blocks"]
    assert any(b.get("type") == "section" for b in blocks)
    actions = next(b for b in blocks if b.get("type") == "actions")
    elements = actions["elements"]
    assert len(elements) == 3
    assert elements[0]["text"]["text"] == "Approve"
    assert elements[0]["action_id"] == "approve:pl:pl#44"
    assert elements[1]["text"]["text"] == "Edit"
    assert elements[1]["url"] == "https://github.com/your-org/your-repo/issues/44"
    assert elements[2]["text"]["text"] == "Send back"
    assert elements[2]["action_id"] == "reject:pl:pl#44"

    # 2. Inbound approve action
    ws = FakeWebSocket()
    envelope = {
        "envelope_id": "env_001",
        "type": "interactive",
        "payload": {
            "type": "block_actions",
            "user": {"id": "U12345"},
            "channel": {"id": "C123"},
            "message": {"ts": "12345.678"},
            "actions": [{"action_id": "approve:pl:pl#44", "value": "approve:pl:pl#44"}],
        },
    }
    slack.handle_envelope(envelope, ws=ws)

    # Envelope must be acknowledged
    assert {"envelope_id": "env_001"} in ws.sent

    # Subprocess must run pl approve
    assert len(pl_calls) == 1
    argv, env = pl_calls[0]
    assert "--profile" in argv and "pl" in argv
    assert "approve" in argv and "pl#44" in argv

    # Message must be edited to show approval
    update_calls = [c for c in http_calls if c[1] == "chat.update"]
    assert len(update_calls) == 1
    _, _, update_payload = update_calls[0]
    assert update_payload["channel"] == "C123"
    assert update_payload["ts"] == "12345.678"
    assert "Approved" in update_payload["text"]


def test_send_back_modal_notes_reach_reject(monkeypatch):
    from pl.chat.slack import Slack

    http_calls = []

    def fake_http(token, method, payload=None):
        http_calls.append((token, method, payload))
        if method == "views.open":
            return {"ok": True, "view": {"id": "V123"}}
        return {"ok": True}

    monkeypatch.setattr("pl.chat.slack._http", fake_http)

    pl_calls = []

    def fake_pl(argv, env=None):
        pl_calls.append((argv, env))
        class Res:
            returncode = 0
            stdout = "rejected"
            stderr = ""
        return Res()

    monkeypatch.setattr("pl.chat.inbound._pl", fake_pl)

    slack = Slack("slack", {
        "token": "${SLACK_BOT_TOKEN}",
        "app_token": "${SLACK_APP_TOKEN}",
        "allow": ["U12345"],
    })

    # 1. Click Send back button
    ws1 = FakeWebSocket()
    btn_envelope = {
        "envelope_id": "env_002",
        "type": "interactive",
        "payload": {
            "type": "block_actions",
            "user": {"id": "U12345"},
            "trigger_id": "trig_789",
            "actions": [{"action_id": "reject:pl:pl#44", "value": "reject:pl:pl#44"}],
        },
    }
    slack.handle_envelope(btn_envelope, ws=ws1)

    assert {"envelope_id": "env_002"} in ws1.sent
    # Modal must be opened via views.open
    view_calls = [c for c in http_calls if c[1] == "views.open"]
    assert len(view_calls) == 1
    _, _, view_payload = view_calls[0]
    assert view_payload["trigger_id"] == "trig_789"
    view = view_payload["view"]
    assert view["type"] == "modal"
    assert view["callback_id"] == "send_back_modal"
    assert view["private_metadata"] == "reject:pl:pl#44"

    # 2. Submit notes in modal
    ws2 = FakeWebSocket()
    sub_envelope = {
        "envelope_id": "env_003",
        "type": "interactive",
        "payload": {
            "type": "view_submission",
            "user": {"id": "U12345"},
            "view": {
                "callback_id": "send_back_modal",
                "private_metadata": "reject:pl:pl#44",
                "state": {
                    "values": {
                        "notes_block": {
                            "notes_input": {"value": "Please add more unit tests for socket timeout"}
                        }
                    }
                },
            },
        },
    }
    slack.handle_envelope(sub_envelope, ws=ws2)

    assert {"envelope_id": "env_003"} in ws2.sent
    assert len(pl_calls) == 1
    argv, env = pl_calls[0]
    assert "--profile" in argv and "pl" in argv
    assert "reject" in argv and "pl#44" in argv
    assert "Please add more unit tests for socket timeout" in argv


def test_unknown_user_ignored_and_audited(monkeypatch, tmp_path):
    from pl.chat.slack import Slack

    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    http_calls = []
    monkeypatch.setattr("pl.chat.slack._http", lambda *a, **k: http_calls.append(a))

    pl_calls = []
    monkeypatch.setattr("pl.chat.inbound._pl", lambda *a, **k: pl_calls.append(a))

    slack = Slack("slack", {
        "token": "${SLACK_BOT_TOKEN}",
        "app_token": "${SLACK_APP_TOKEN}",
        "allow": ["U12345"],
    })

    ws = FakeWebSocket()
    envelope = {
        "envelope_id": "env_unknown",
        "type": "interactive",
        "payload": {
            "type": "block_actions",
            "user": {"id": "U_UNKNOWN_ATTACKER"},
            "actions": [{"action_id": "approve:pl:pl#44", "value": "approve:pl:pl#44"}],
        },
    }
    slack.handle_envelope(envelope, ws=ws)

    # Must acknowledge envelope to stop retries
    assert {"envelope_id": "env_unknown"} in ws.sent
    # Must NOT call pl approve or any HTTP API
    assert len(pl_calls) == 0
    assert len(http_calls) == 0

    # Must write audit log entry
    audit_file = tmp_path / "machine" / "audit.jsonl"
    assert audit_file.exists()
    audit_data = [json.loads(line) for line in audit_file.read_text().splitlines() if line]
    assert len(audit_data) >= 1
    rec = audit_data[-1]
    assert rec["kind"] == "chat_unknown_sender"
    assert rec["connector"] == "slack"
    assert rec["sender_id"] == "U_UNKNOWN_ATTACKER"


def test_tokens_masked_in_errors(monkeypatch):
    import io
    from pl.chat.slack import Slack, _http

    def fake_open(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://slack.com/api/chat.postMessage?token=xoxb-secret-bot-token-987",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b"token xoxb-secret-bot-token-987 invalid"),
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(Exception) as excinfo:
        _http("xoxb-secret-bot-token-987", "chat.postMessage", {})

    err_str = str(excinfo.value)
    assert "xoxb-secret-bot-token-987" not in err_str
    assert "***" in err_str

    slack = Slack("slack", {
        "token": "${SLACK_BOT_TOKEN}",
        "app_token": "${SLACK_APP_TOKEN}",
        "allow": ["U12345"],
    })

    def fake_open_conn(token, method, payload=None):
        raise RuntimeError(f"Failed apps.connections.open with {token}")

    monkeypatch.setattr("pl.chat.slack._http", fake_open_conn)

    with pytest.raises(Exception) as excinfo:
        slack.connect_socket_mode()

    err_str2 = str(excinfo.value)
    assert "xapp-secret-app-token-654" not in err_str2
