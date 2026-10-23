import json
import urllib.error
import pytest


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data) if isinstance(data, str) else data)


@pytest.fixture(autouse=True)
def discord_env(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-discord-bot-token-12345")


def test_gate_message_components_and_approve_interaction(monkeypatch):
    from pl.chat.discord import Discord

    http_calls = []

    def fake_http(token, path, payload=None, method="POST"):
        http_calls.append((token, path, payload, method))
        if "messages" in path and method == "POST":
            return {"id": "msg_999", "channel_id": "chan_888"}
        if "callback" in path:
            return {"ok": True}
        return {"ok": True}

    monkeypatch.setattr("pl.chat.discord._http", fake_http)

    pl_calls = []

    def fake_pl(argv, env=None):
        pl_calls.append((argv, env))
        class Res:
            returncode = 0
            stdout = "approved"
            stderr = ""
        return Res()

    monkeypatch.setattr("pl.chat.discord._pl", fake_pl)

    discord = Discord("discord", {
        "token": "${DISCORD_BOT_TOKEN}",
        "allow": ["user_allowed_123"],
    })

    # 1. Send gate message with buttons
    buttons = [
        ("Approve", "approve:pl:pl#45"),
        ("Edit", "https://github.com/your-org/your-repo/issues/45"),
        ("Send back", "reject:pl:pl#45"),
    ]
    res = discord.send("chan_888", "Plan 3 slice 10 gate message", buttons=buttons)
    assert res["id"] == "msg_999"
    assert len(http_calls) == 1
    token, path, payload, method = http_calls[0]
    assert token == "secret-discord-bot-token-12345"
    assert path == "channels/chan_888/messages"
    assert method == "POST"
    assert "Plan 3 slice 10" in payload["content"]

    components = payload["components"]
    assert len(components) == 1
    action_row = components[0]
    assert action_row["type"] == 1
    elements = action_row["components"]
    assert len(elements) == 3

    assert elements[0]["type"] == 2
    assert elements[0]["label"] == "Approve"
    assert elements[0]["custom_id"] == "approve:pl:pl#45"
    assert elements[0]["style"] in (1, 3)

    assert elements[1]["type"] == 2
    assert elements[1]["label"] == "Edit"
    assert elements[1]["url"] == "https://github.com/your-org/your-repo/issues/45"
    assert elements[1]["style"] == 5

    assert elements[2]["type"] == 2
    assert elements[2]["label"] == "Send back"
    assert elements[2]["custom_id"] == "reject:pl:pl#45"
    assert elements[2]["style"] == 4

    # 2. Inbound interaction: Approve button clicked
    ws = FakeWebSocket()
    interaction_payload = {
        "op": 0,
        "s": 1,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "int_1",
            "token": "tok_1",
            "type": 3,
            "channel_id": "chan_888",
            "message": {"id": "msg_999"},
            "user": {"id": "user_allowed_123"},
            "data": {
                "custom_id": "approve:pl:pl#45",
                "component_type": 2,
            },
        },
    }
    discord.handle_gateway_payload(interaction_payload, ws=ws)

    # Subprocess runs pl approve
    assert len(pl_calls) == 1
    argv, env = pl_calls[0]
    assert "--profile" in argv and "pl" in argv
    assert "approve" in argv and "pl#45" in argv

    # Interaction callback answered with UPDATE_MESSAGE (type 7)
    callback_calls = [c for c in http_calls if "interactions/int_1/tok_1/callback" in c[1]]
    assert len(callback_calls) == 1
    _, cb_path, cb_payload, _ = callback_calls[0]
    assert cb_payload["type"] == 7
    assert "Approved" in cb_payload["data"]["content"]


def test_send_back_modal_notes_reach_reject(monkeypatch):
    from pl.chat.discord import Discord

    http_calls = []

    def fake_http(token, path, payload=None, method="POST"):
        http_calls.append((token, path, payload, method))
        return {"ok": True}

    monkeypatch.setattr("pl.chat.discord._http", fake_http)

    pl_calls = []

    def fake_pl(argv, env=None):
        pl_calls.append((argv, env))
        class Res:
            returncode = 0
            stdout = "rejected"
            stderr = ""
        return Res()

    monkeypatch.setattr("pl.chat.discord._pl", fake_pl)

    discord = Discord("discord", {
        "token": "${DISCORD_BOT_TOKEN}",
        "allow": ["user_allowed_123"],
    })

    # 1. User clicks Send back button
    ws1 = FakeWebSocket()
    btn_interaction = {
        "op": 0,
        "s": 2,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "int_2",
            "token": "tok_2",
            "type": 3,
            "channel_id": "chan_888",
            "message": {"id": "msg_999"},
            "user": {"id": "user_allowed_123"},
            "data": {
                "custom_id": "reject:pl:pl#45",
                "component_type": 2,
            },
        },
    }
    discord.handle_gateway_payload(btn_interaction, ws=ws1)

    # Modal (type 9) must be returned in callback
    modal_calls = [c for c in http_calls if "interactions/int_2/tok_2/callback" in c[1]]
    assert len(modal_calls) == 1
    _, _, modal_payload, _ = modal_calls[0]
    assert modal_payload["type"] == 9
    modal_data = modal_payload["data"]
    assert "send_back_modal" in modal_data["custom_id"]
    assert "reject:pl:pl#45" in modal_data["custom_id"]
    text_input = modal_data["components"][0]["components"][0]
    assert text_input["type"] == 4
    assert text_input["custom_id"] == "notes_input"

    # 2. User submits the modal with review notes
    ws2 = FakeWebSocket()
    modal_submit_interaction = {
        "op": 0,
        "s": 3,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "int_3",
            "token": "tok_3",
            "type": 5,
            "channel_id": "chan_888",
            "message": {"id": "msg_999"},
            "user": {"id": "user_allowed_123"},
            "data": {
                "custom_id": modal_data["custom_id"],
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "notes_input",
                                "value": "Needs more tests for heartbeat reconnect",
                            }
                        ],
                    }
                ],
            },
        },
    }
    discord.handle_gateway_payload(modal_submit_interaction, ws=ws2)

    # pl reject must be called with card and notes
    assert len(pl_calls) == 1
    argv, env = pl_calls[0]
    assert "--profile" in argv and "pl" in argv
    assert "reject" in argv and "pl#45" in argv
    assert "Needs more tests for heartbeat reconnect" in argv


def test_unknown_user_ignored_and_audited(monkeypatch, tmp_path):
    from pl.chat.discord import Discord

    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    http_calls = []
    monkeypatch.setattr("pl.chat.discord._http", lambda *a, **k: http_calls.append(a))

    pl_calls = []
    monkeypatch.setattr("pl.chat.discord._pl", lambda *a, **k: pl_calls.append(a))

    discord = Discord("discord", {
        "token": "${DISCORD_BOT_TOKEN}",
        "allow": ["user_allowed_123"],
    })

    ws = FakeWebSocket()
    unknown_interaction = {
        "op": 0,
        "s": 4,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "int_unauth",
            "token": "tok_unauth",
            "type": 3,
            "channel_id": "chan_888",
            "user": {"id": "unknown_attacker_999"},
            "data": {
                "custom_id": "approve:pl:pl#45",
                "component_type": 2,
            },
        },
    }
    discord.handle_gateway_payload(unknown_interaction, ws=ws)

    # Must NOT call pl command or send HTTP callback
    assert len(pl_calls) == 0
    assert len(http_calls) == 0

    # Must write audit record
    audit_file = tmp_path / "machine" / "audit.jsonl"
    assert audit_file.exists()
    audit_data = [json.loads(line) for line in audit_file.read_text().splitlines() if line]
    assert len(audit_data) >= 1
    rec = audit_data[-1]
    assert rec["kind"] == "chat_unknown_sender"
    assert rec["connector"] == "discord"
    assert rec["sender_id"] == "unknown_attacker_999"


def test_tokens_masked_in_errors(monkeypatch):
    import io
    from pl.chat.discord import Discord, _http

    def fake_open(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://discord.com/api/v10/channels/chan_888/messages?token=secret-discord-bot-token-12345",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b"token secret-discord-bot-token-12345 invalid"),
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(Exception) as excinfo:
        _http("secret-discord-bot-token-12345", "channels/chan_888/messages", {})

    err_str = str(excinfo.value)
    assert "secret-discord-bot-token-12345" not in err_str
    assert "***" in err_str

    discord = Discord("discord", {
        "token": "${DISCORD_BOT_TOKEN}",
        "allow": ["user_allowed_123"],
    })

    def fake_gateway(token, path, payload=None, method="GET"):
        raise RuntimeError(f"Failed gateway connect with {token}")

    monkeypatch.setattr("pl.chat.discord._http", fake_gateway)

    with pytest.raises(Exception) as excinfo:
        discord.get_gateway_url()

    err_str2 = str(excinfo.value)
    assert "secret-discord-bot-token-12345" not in err_str2
