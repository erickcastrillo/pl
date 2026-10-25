import os
import subprocess
import sys
import tomlkit
import pytest

from pl import alerts
from pl.manager import machine_dir


@pytest.fixture(autouse=True)
def machine_env(tmp_path, monkeypatch):
    mdir = tmp_path / "machine"
    mdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PL_MACHINE_DIR", str(mdir))
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
        self.allow = cfg.get("allow", [])
        self.routes = cfg.get("routes", [])
        self.sent: list[tuple[str, str]] = []

    def send(self, chat: str, text: str, buttons=None):
        self.sent.append((str(chat), text))


def test_route_alerts_slack_sends_a_sign_in_alert_to_slack_not_telegram(monkeypatch):
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["111"],
            "routes": ["gates", "alerts"],
        },
        "slack": {
            "role": "messaging",
            "adapter": "slack",
            "token": "${SLACK_TOKEN}",
            "allow": ["222"],
            "routes": ["gates"],
        },
    })

    from pl import chat
    from pl.chat import gates

    slack_adapter = FakeAdapter("slack", {"allow": ["222"]})
    telegram_adapter = FakeAdapter("telegram", {"allow": ["111"]})
    adapters = {"slack": slack_adapter, "telegram": telegram_adapter}

    monkeypatch.setattr(chat, "adapter", lambda name: adapters[name])

    code, msg = chat.route("alerts", ["slack"])
    assert code == 0

    alerts.open(
        key="connector_auth:billing",
        severity="high",
        title="Billing connector sign-in expired",
        fix="Run pl connector login billing",
    )

    sent_count = gates.post_outbox()
    assert sent_count > 0

    assert len(slack_adapter.sent) == 1
    chat_id, text = slack_adapter.sent[0]
    assert chat_id == "222"
    assert "Billing connector sign-in expired" in text
    assert len(telegram_adapter.sent) == 0

    outbox = machine_dir() / "chat-outbox.jsonl"
    assert not outbox.exists() or outbox.read_text().strip() == ""


def test_route_is_idempotent():
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["111"],
            "routes": ["gates"],
        },
        "slack": {
            "role": "messaging",
            "adapter": "slack",
            "token": "${SLACK_TOKEN}",
            "allow": ["222"],
            "routes": ["gates"],
        },
    })

    from pl import chat

    code1, _ = chat.route("alerts", ["slack"])
    assert code1 == 0
    conns1 = chat.load()
    assert conns1["slack"]["routes"] == ["gates", "alerts"]
    assert conns1["telegram"]["routes"] == ["gates"]

    code2, _ = chat.route("alerts", ["slack"])
    assert code2 == 0
    conns2 = chat.load()
    assert conns2["slack"]["routes"] == ["gates", "alerts"]
    assert conns2["telegram"]["routes"] == ["gates"]


def test_an_outbox_write_failure_never_fails_the_alert(monkeypatch):
    outbox_file = machine_dir() / "chat-outbox.jsonl"

    real_open = os.open

    def fail_open(path, *args, **kwargs):
        if str(outbox_file) in str(path):
            raise OSError("Simulated disk error or permission failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_open)

    why = alerts.open(
        key="connector_auth:billing",
        severity="high",
        title="Sign-in expired",
        fix="Fix it",
    )
    assert why == "open"
    assert alerts.get("connector_auth:billing") is not None


def test_cli_route_alerts():
    _setup_machine_toml({
        "telegram": {
            "role": "messaging",
            "adapter": "telegram",
            "token": "${TG_TOKEN}",
            "allow": ["111"],
            "routes": ["gates", "alerts"],
        },
        "slack": {
            "role": "messaging",
            "adapter": "slack",
            "token": "${SLACK_TOKEN}",
            "allow": ["222"],
            "routes": ["gates"],
        },
    })
    cmd = [sys.executable, "-m", "pl", "chat", "route", "alerts", "slack"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)
    assert r.returncode == 0
    from pl import chat
    conns = chat.load()
    assert "alerts" in conns["slack"]["routes"]
    assert "alerts" not in conns["telegram"]["routes"]

