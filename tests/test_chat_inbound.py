import json
import os
import stat
import time
from unittest.mock import patch

from pl import audit, outward
from pl.chat import inbound


class FakeAdapter:
    def __init__(self, name="telegram", allow=None):
        self.name = name
        self.allow = allow or ["123"]
        self.updates_queue = []
        self.sent = []
        self.edits = []
        self.callbacks = []

    def updates(self, offset=None):
        res = list(self.updates_queue)
        self.updates_queue.clear()
        return res

    def send(self, chat, text, buttons=None, force_reply=False, reply_to_message_id=None):
        msg = {"message_id": len(self.sent) + 100, "chat": {"id": str(chat)}, "text": text}
        self.sent.append(msg)
        return msg

    def edit(self, chat, msg, text, buttons=None):
        self.edits.append({"chat": str(chat), "msg": msg, "text": text, "buttons": buttons})
        return {"chat_id": str(chat), "message_id": msg, "text": text}

    def answer_callback(self, callback_id, text=None):
        self.callbacks.append({"id": callback_id, "text": text})
        return True


def test_approve_tap_runs_pl_approve_and_edits_the_message(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = FakeAdapter(allow=["123"])
    calls = []

    def fake_pl(argv, env=None):
        calls.append((argv, env))
        return 0, "ok"

    monkeypatch.setattr(inbound, "_pl", fake_pl)

    inbound.register_nonce("nonce-app-1", {
        "action": "approve",
        "card": "pl#41",
        "profile": "default",
        "chat_id": "123",
    })

    adapter.updates_queue.append({
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": "123"},
            "message": {"message_id": 50, "chat": {"id": "123"}},
            "data": "nonce-app-1",
        },
    })

    inbound.poll(adapter)

    assert len(calls) == 1
    argv, env = calls[0]
    assert "--profile" in argv and "default" in argv
    assert "approve" in argv and "pl#41" in argv
    assert env is None or env.get("PL_SOURCE") == "chat" or os.environ.get("PL_SOURCE") == "chat"
    assert inbound.get_nonce("nonce-app-1") is None
    assert len(adapter.edits) == 1
    assert adapter.edits[0]["chat"] == "123"
    assert adapter.edits[0]["msg"] == 50
    assert "Approved by you at " in adapter.edits[0]["text"]
    assert len(adapter.callbacks) == 1


def test_unknown_chat_gets_no_reply_and_one_audit_entry(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = FakeAdapter(name="telegram", allow=["123"])

    adapter.updates_queue.append({
        "update_id": 2,
        "message": {
            "message_id": 51,
            "from": {"id": "999"},
            "chat": {"id": "999"},
            "text": "secret attack string",
        },
    })

    inbound.poll(adapter)

    assert len(adapter.sent) == 0
    assert len(adapter.edits) == 0
    assert len(adapter.callbacks) == 0

    audit_file = tmp_path / "audit.jsonl"
    assert audit_file.exists()
    lines = [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "chat_unknown_sender"
    assert rec.get("connector") == "telegram"
    assert str(rec.get("sender_id")) == "999"
    assert "secret attack string" not in json.dumps(rec)
    assert "text" not in rec


def test_a_forged_or_old_nonce_does_nothing(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = FakeAdapter(allow=["123"])
    calls = []
    monkeypatch.setattr(inbound, "_pl", lambda argv, env=None: calls.append((argv, env)) or (0, "ok"))

    adapter.updates_queue.append({
        "update_id": 3,
        "callback_query": {
            "id": "cb_forged",
            "from": {"id": "123"},
            "message": {"message_id": 52, "chat": {"id": "123"}},
            "data": "forged-nonce-12345",
        },
    })

    inbound.poll(adapter)

    assert len(calls) == 0
    assert len(adapter.edits) == 0


def test_audit_file_is_0600_and_append_only(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)

    audit_file = tmp_path / "audit.jsonl"
    assert not audit_file.exists()

    audit.record("test_first", user="u1")
    assert audit_file.exists()
    mode = stat.S_IMODE(audit_file.stat().st_mode)
    assert mode == 0o600

    audit.record("test_second", user="u2")
    lines = [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "test_first"
    assert lines[0]["user"] == "u1"
    assert lines[1]["kind"] == "test_second"
    assert lines[1]["user"] == "u2"
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600


def test_remote_action_event_names_the_source(tmp_path, monkeypatch):
    from pl import config as C
    from pl import cli

    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(C, "PROFILE_NAME", "testprof")
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("pl.config.load", lambda p: None)
    monkeypatch.setenv("PL_SOURCE", "chat")

    with patch.dict(cli.__dict__, {"cmd_approve": lambda a: 0}):
        monkeypatch.setattr("sys.argv", ["pl", "--profile", "testprof", "approve", "pl#41"])
        try:
            cli.main()
        except SystemExit:
            pass

    events_file = tmp_path / "events.jsonl"
    assert events_file.exists()
    events_lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    remote_events = [e for e in events_lines if e.get("kind") == "remote_action"]
    assert len(remote_events) >= 1
    ev = remote_events[0]
    assert ev["source"] == "chat"
    assert ev["command"] == "approve"
    assert ev["card"] == "pl#41"


class ChatFake:
    def __init__(self, allow=None):
        self.allow = allow or ["123"]
        self.sent = []
        self.edits = []
        self.callbacks = []
        self.msg_counter = 100

    def send(self, chat: str, text: str, buttons=None, force_reply: bool = False, reply_to_message_id=None):
        self.msg_counter += 1
        msg = {
            "message_id": self.msg_counter,
            "chat": {"id": str(chat)},
            "text": text,
            "reply_markup": {"force_reply": True} if force_reply else buttons,
        }
        self.sent.append(msg)
        return msg

    def edit(self, chat: str, msg: str | int, text: str, buttons=None):
        self.edits.append({"chat": str(chat), "msg": msg, "text": text, "buttons": buttons})
        return {"chat_id": str(chat), "message_id": msg, "text": text}

    def answer_callback(self, callback_id: str, text: str | None = None):
        self.callbacks.append({"id": callback_id, "text": text})
        return True


def test_send_back_then_reply_writes_review_notes(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = ChatFake()
    calls = []
    monkeypatch.setattr(inbound, "_pl", lambda argv, env=None: calls.append((argv, env)) or (0, "ok"))

    inbound.register_nonce("nonce-sb", {
        "action": "send_back",
        "card": "pl#42",
        "profile": "default",
        "chat_id": "123",
    })

    cb_update = {
        "callback_query": {
            "id": "cb1",
            "from": {"id": "123"},
            "message": {"message_id": 50, "chat": {"id": "123"}},
            "data": "nonce-sb",
        }
    }
    inbound.handle_update(adapter, cb_update)

    assert len(adapter.sent) == 1
    sent_prompt = adapter.sent[0]
    assert "Reply to this message with your notes" in sent_prompt["text"]
    assert sent_prompt["reply_markup"] == {"force_reply": True}
    prompt_id = sent_prompt["message_id"]

    long_notes = "Refactor this component. " + ("x" * 5000)
    reply_update = {
        "message": {
            "message_id": 105,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": long_notes,
            "reply_to_message": {"message_id": prompt_id},
        }
    }
    inbound.handle_update(adapter, reply_update)

    assert len(calls) == 1
    argv, _ = calls[0]
    assert argv[:4] == ["--profile", "default", "reject", "pl#42"]
    passed_notes = argv[4]
    assert len(passed_notes) == 4000
    assert passed_notes == long_notes[:4000]


def test_risky_approve_needs_confirm_and_expires(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = ChatFake()
    calls = []
    monkeypatch.setattr(inbound, "_pl", lambda argv, env=None: calls.append((argv, env)) or (0, "ok"))

    card_data = {"metadata": {"outward": {"amount": 500}}}
    inbound.register_nonce("nonce-risk-1", {
        "action": "approve",
        "card": "pl#99",
        "profile": "default",
        "chat_id": "123",
        "card_data": card_data,
    })

    cb_update = {
        "callback_query": {
            "id": "cb2",
            "from": {"id": "123"},
            "message": {"message_id": 60, "chat": {"id": "123"}},
            "data": "nonce-risk-1",
        }
    }
    inbound.handle_update(adapter, cb_update)

    assert len(calls) == 0
    assert len(adapter.edits) == 1
    edit = adapter.edits[0]
    assert "This sends money, deletes, or mails more than 10 people. Tap Confirm to approve" in edit["text"]
    buttons = edit["buttons"]
    assert buttons
    confirm_nonce = None
    if isinstance(buttons, list):
        row = buttons[0] if isinstance(buttons[0], list) else buttons
        btn = row[0]
        confirm_nonce = btn[1] if isinstance(btn, (list, tuple)) else btn.get("callback_data")
    assert confirm_nonce is not None

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 660)
    confirm_update = {
        "callback_query": {
            "id": "cb3",
            "from": {"id": "123"},
            "message": {"message_id": 60, "chat": {"id": "123"}},
            "data": confirm_nonce,
        }
    }
    inbound.handle_update(adapter, confirm_update)
    assert len(calls) == 0
    assert "expired" in adapter.edits[-1]["text"].lower()

    inbound.register_nonce("nonce-risk-2", {
        "action": "approve",
        "card": "pl#99",
        "profile": "default",
        "chat_id": "123",
        "card_data": card_data,
    })
    cb_update2 = {
        "callback_query": {
            "id": "cb4",
            "from": {"id": "123"},
            "message": {"message_id": 70, "chat": {"id": "123"}},
            "data": "nonce-risk-2",
        }
    }
    monkeypatch.setattr(time, "time", lambda: now + 700)
    inbound.handle_update(adapter, cb_update2)
    buttons2 = adapter.edits[-1]["buttons"]
    row2 = buttons2[0] if isinstance(buttons2[0], list) else buttons2
    btn2 = row2[0]
    confirm_nonce2 = btn2[1] if isinstance(btn2, (list, tuple)) else btn2.get("callback_data")

    monkeypatch.setattr(time, "time", lambda: now + 800)
    confirm_update2 = {
        "callback_query": {
            "id": "cb5",
            "from": {"id": "123"},
            "message": {"message_id": 70, "chat": {"id": "123"}},
            "data": confirm_nonce2,
        }
    }
    inbound.handle_update(adapter, confirm_update2)
    assert len(calls) == 1
    assert calls[0][0][:4] == ["--profile", "default", "approve", "pl#99"]


def test_free_text_runs_no_command_and_says_replies_only(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = ChatFake()
    calls = []
    monkeypatch.setattr(inbound, "_pl", lambda argv, env=None: calls.append((argv, env)) or (0, "ok"))

    msg_update = {
        "message": {
            "message_id": 200,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "Status update please",
        }
    }
    inbound.handle_update(adapter, msg_update)

    assert len(calls) == 0
    assert len(adapter.sent) == 1
    assert "I only take replies to cards" in adapter.sent[0]["text"]


def test_stop_turns_the_kill_switch_on_and_off_is_refused_from_chat(tmp_path, monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    adapter = ChatFake()

    assert outward.state()["on"] is False

    stop_update = {
        "message": {
            "message_id": 300,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "/stop",
        }
    }
    inbound.handle_update(adapter, stop_update)

    st = outward.state()
    assert st["on"] is True
    assert st["by"] == "chat"
    assert len(adapter.sent) >= 1
    assert "kill switch" in adapter.sent[-1]["text"].lower()

    off_update = {
        "message": {
            "message_id": 301,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "/stop off",
        }
    }
    inbound.handle_update(adapter, off_update)

    st_after = outward.state()
    assert st_after["on"] is True
    assert "cannot be turned off from chat" in adapter.sent[-1]["text"].lower() or "refused" in adapter.sent[-1]["text"].lower()
