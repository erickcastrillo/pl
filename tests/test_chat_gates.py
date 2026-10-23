import pytest
import tomlkit

from pl import manager
from pl.manager import machine_dir


@pytest.fixture(autouse=True)
def machine_env(tmp_path, monkeypatch):
    mdir = tmp_path / "machine"
    mdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PL_MACHINE_DIR", str(mdir))
    monkeypatch.setenv("TG_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    sdir = tmp_path / "state"
    sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("pl.config.STATE_DIR", sdir)


def _setup_machine_toml(connectors: dict):
    doc = tomlkit.document()
    conns = doc.setdefault("connectors", tomlkit.table())
    for name, cfg in connectors.items():
        tbl = tomlkit.table()
        tbl.update(cfg)
        conns[name] = tbl
    (machine_dir() / "machine.toml").write_text(tomlkit.dumps(doc))


class FakeAdapter:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.allow = cfg.get("allow", ["123"])
        self.routes = cfg.get("routes", ["gates"])
        self.sent: list[dict] = []

    def send(self, chat: str, text: str, buttons=None):
        msg = {
            "chat": str(chat),
            "text": text,
            "buttons": buttons,
            "message_id": len(self.sent) + 1,
        }
        self.sent.append(msg)
        return msg


def test_plan_gate_posts_message_with_summary_link_and_buttons(monkeypatch):
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["123"],
            "routes": ["gates"],
        }
    })

    from pl import chat
    from pl.chat import gates

    adapter = FakeAdapter("telegram", {"allow": ["123"], "routes": ["gates"]})
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)

    fake_gates = [
        {
            "pipeline": "engineering",
            "card": "pl#40",
            "gate": "plan",
            "summary": "Implement slice 5 gate messages",
            "url": "https://github.com/your-org/your-repo/issues/40",
            "since": "2026-10-23T20:00:00Z",
        }
    ]
    monkeypatch.setattr(gates, "pending_gates", lambda: fake_gates)

    sent_count = gates.post_gates()
    assert sent_count == 1
    assert len(adapter.sent) == 1

    msg = adapter.sent[0]
    assert msg["chat"] == "123"
    assert "Plan gate" in msg["text"] or "plan" in msg["text"].lower()
    assert "Implement slice 5 gate messages" in msg["text"]
    assert "https://github.com/your-org/your-repo/issues/40" in msg["text"]

    buttons = msg["buttons"]
    assert buttons
    row = buttons[0] if isinstance(buttons[0], list) else buttons
    labels = [b.get("text") if isinstance(b, dict) else b[0] for b in row]
    assert "Approve" in labels
    assert "Edit" in labels
    assert "Send back" in labels

    # Edit button links to the card URL
    edit_btn = next(b for b in row if (b.get("text") if isinstance(b, dict) else b[0]) == "Edit")
    if isinstance(edit_btn, dict):
        assert edit_btn.get("url") == "https://github.com/your-org/your-repo/issues/40"


def test_gate_is_not_sent_again(monkeypatch):
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["123"],
            "routes": ["gates"],
        }
    })

    from pl import chat
    from pl.chat import gates

    adapter = FakeAdapter("telegram", {"allow": ["123"], "routes": ["gates"]})
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)

    fake_gates = [
        {
            "pipeline": "engineering",
            "card": "pl#40",
            "gate": "plan",
            "summary": "Implement slice 5 gate messages",
            "url": "https://github.com/your-org/your-repo/issues/40",
            "since": "2026-10-23T20:00:00Z",
        }
    ]
    monkeypatch.setattr(gates, "pending_gates", lambda: fake_gates)

    count1 = gates.post_gates()
    assert count1 == 1
    assert len(adapter.sent) == 1

    count2 = gates.post_gates()
    assert count2 == 0
    assert len(adapter.sent) == 1


def test_nonces_registered_for_approve_and_send_back(monkeypatch):
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["123"],
            "routes": ["gates"],
        }
    })

    from pl import chat
    from pl.chat import gates, inbound

    adapter = FakeAdapter("telegram", {"allow": ["123"], "routes": ["gates"]})
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)

    fake_gates = [
        {
            "pipeline": "engineering",
            "card": "pl#40",
            "gate": "plan",
            "summary": "Implement slice 5",
            "url": "https://github.com/your-org/your-repo/issues/40",
            "since": "2026-10-23T20:00:00Z",
        }
    ]
    monkeypatch.setattr(gates, "pending_gates", lambda: fake_gates)

    gates.post_gates()
    msg = adapter.sent[0]
    row = msg["buttons"][0] if isinstance(msg["buttons"][0], list) else msg["buttons"]

    approve_btn = next(b for b in row if (b.get("text") if isinstance(b, dict) else b[0]) == "Approve")
    approve_nonce = approve_btn.get("callback_data") if isinstance(approve_btn, dict) else approve_btn[1]

    send_back_btn = next(b for b in row if (b.get("text") if isinstance(b, dict) else b[0]) == "Send back")
    send_back_nonce = send_back_btn.get("callback_data") if isinstance(send_back_btn, dict) else send_back_btn[1]

    app_data = inbound.get_nonce(approve_nonce)
    assert app_data is not None
    assert app_data["action"] == "approve"
    assert app_data["card"] == "pl#40"
    assert app_data["profile"] == "engineering"

    sb_data = inbound.get_nonce(send_back_nonce)
    assert sb_data is not None
    assert sb_data["action"] == "send_back"
    assert sb_data["card"] == "pl#40"
    assert sb_data["profile"] == "engineering"


def test_manager_tick_calls_post_gates_and_never_fails(monkeypatch):
    from pl.chat import gates

    called = []

    def mock_post_gates():
        called.append(True)
        raise RuntimeError("Simulated transient network error")

    monkeypatch.setattr(gates, "post_gates", mock_post_gates)
    monkeypatch.setattr(manager, "managed", lambda: [])
    monkeypatch.setattr(manager, "_caps", lambda state, profs: (0, 8, 10, None, {}))
    monkeypatch.setattr(manager, "_machine_check", lambda state: None)

    state = {}
    st = manager.tick(state)
    assert len(called) == 1
    assert isinstance(st, dict)
    assert st["at"] > 0


def test_safe_text_and_token_not_leaked_in_gate_messages(monkeypatch):
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["123"],
            "routes": ["gates"],
        }
    })

    from pl import chat
    from pl.chat import gates

    adapter = FakeAdapter("telegram", {"allow": ["123"], "routes": ["gates"]})
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)

    fake_gates = [
        {
            "pipeline": "default",
            "card": "pl#40",
            "gate": "plan",
            "summary": "Pay $500 to bob@example.com at 123 Main St token 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            "url": "https://github.com/your-org/your-repo/issues/40",
            "since": "2026-10-23T20:00:00Z",
        }
    ]
    monkeypatch.setattr(gates, "pending_gates", lambda: fake_gates)

    gates.post_gates()
    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "bob@example.com" not in text
    assert "$500" not in text
    assert "123 Main St" not in text
    assert "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" not in text
