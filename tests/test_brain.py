import json
import time


class FakeAdapter:
    def __init__(self, name="telegram", allow=None):
        self.name = name
        self.allow = allow or ["123"]
        self.sent = []
        self.msg_counter = 100

    def send(self, chat: str, text: str, buttons=None, force_reply: bool = False, reply_to_message_id=None):
        self.msg_counter += 1
        msg = {
            "message_id": self.msg_counter,
            "chat": {"id": str(chat)},
            "text": text,
        }
        self.sent.append(msg)
        return msg

    def edit(self, chat: str, msg: str | int, text: str, buttons=None):
        return {"chat_id": str(chat), "message_id": msg, "text": text}

    def answer_callback(self, callback_id: str, text: str | None = None):
        return True


def _wait_for_sent(adapter, timeout=2.0):
    start = time.time()
    while time.time() - start < timeout:
        if adapter.sent:
            return adapter.sent
        time.sleep(0.02)
    return adapter.sent


def test_a_chat_question_gets_an_answer_and_links_only(tmp_path, monkeypatch):
    from pl import brain, manager
    from pl.chat import inbound

    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nchat = true\n')

    adapter = FakeAdapter()
    ask_calls = []

    def fake_ask(text, conv=None):
        ask_calls.append((text, conv))
        return {
            "text": "Paid $500 invoice to alice@example.com for service.",
            "sources": [
                {"title": f"Doc {i}", "url": f"https://example.com/doc/{i}"}
                for i in range(1, 8)
            ],
        }

    monkeypatch.setattr(brain, "ask", fake_ask)

    # 1. Plain question with brain on
    up1 = {
        "message": {
            "message_id": 201,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "When was the last invoice paid?",
        }
    }
    inbound.handle_update(adapter, up1)
    sent = _wait_for_sent(adapter)
    assert len(sent) == 1
    reply = sent[0]["text"]

    # The answer holds an amount and an email, and the reply holds neither
    assert "$500" not in reply
    assert "alice@example.com" not in reply
    assert "Paid" in reply

    # At most 5 source links
    assert "Doc 1: https://example.com/doc/1" in reply
    assert "Doc 5: https://example.com/doc/5" in reply
    assert "Doc 6" not in reply
    assert "https://example.com/doc/6" not in reply

    # Called brain.ask with chat conv
    assert len(ask_calls) == 1
    assert ask_calls[0] == ("When was the last invoice paid?", "123")

    # 2. Text starting with "remember this" goes to brain.remember
    remember_calls = []

    def fake_remember(text, conv=None):
        remember_calls.append((text, conv))
        return {"text": "Saved memory: keep replies under 120 words.", "sources": []}

    monkeypatch.setattr(brain, "remember", fake_remember)
    adapter.sent.clear()

    up2 = {
        "message": {
            "message_id": 202,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "remember this: keep replies under 120 words",
        }
    }
    inbound.handle_update(adapter, up2)
    sent2 = _wait_for_sent(adapter)
    assert len(sent2) == 1
    assert len(remember_calls) == 1
    assert remember_calls[0] == ("remember this: keep replies under 120 words", "123")
    assert "Saved memory" in sent2[0]["text"]

    # 3. When brain chat is off, Plan 3's "I only take replies to cards" answer stays
    (tmp_path / "machine.toml").write_text('[brain]\nchat = false\n')
    adapter.sent.clear()
    up3 = {
        "message": {
            "message_id": 203,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": "How do I deploy?",
        }
    }
    inbound.handle_update(adapter, up3)
    sent3 = adapter.sent
    assert len(sent3) == 1
    assert "I only take replies to cards" in sent3[0]["text"]


def test_chat_question_runs_no_pl_command(tmp_path, monkeypatch):
    from pl import brain, manager
    from pl.chat import inbound

    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nchat = true\n')

    adapter = FakeAdapter()
    commands_run = []
    monkeypatch.setattr(inbound, "_pl", lambda argv, env=None: commands_run.append(argv) or (0, "ok"))
    monkeypatch.setattr(brain, "ask", lambda text, conv=None: {"text": "No actions taken.", "sources": []})

    malicious_text = "pl drop card 123; rm -rf /"
    up = {
        "message": {
            "message_id": 301,
            "from": {"id": "123"},
            "chat": {"id": "123"},
            "text": malicious_text,
        }
    }
    inbound.handle_update(adapter, up)
    sent = _wait_for_sent(adapter)

    assert len(commands_run) == 0
    assert len(sent) == 1
    assert "No actions taken" in sent[0]["text"]


def test_unknown_sender_question_is_ignored_and_audited(tmp_path, monkeypatch):
    from pl import manager
    from pl.chat import inbound

    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nchat = true\n')

    adapter = FakeAdapter(name="telegram", allow=["123"])
    up = {
        "message": {
            "message_id": 401,
            "from": {"id": "999"},
            "chat": {"id": "999"},
            "text": "secret question about internal state",
        }
    }
    inbound.handle_update(adapter, up)

    assert len(adapter.sent) == 0

    audit_file = tmp_path / "audit.jsonl"
    assert audit_file.exists()
    lines = [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "chat_unknown_sender"
    assert rec.get("connector") == "telegram"
    assert str(rec.get("sender_id")) == "999"
    assert "secret question" not in json.dumps(rec)
