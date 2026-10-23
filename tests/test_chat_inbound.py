import time
from pl.chat import inbound
from pl import outward


class FakeAdapter:
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
    adapter = FakeAdapter()
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
    adapter = FakeAdapter()
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
    adapter = FakeAdapter()
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
    adapter = FakeAdapter()

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
