"""Plan 51 slice 1: notifications for PR opened, merged and done, filtered by [notify] events."""
import json

import pytest
import tomlkit

from pl import config as C
from pl import notifications
from pl.manager import machine_dir

NINE = ["plan_ready", "approved", "pr_opened", "review_passed", "review_rework", "merged", "done", "alert_opened",
        "agent_waiting"]


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "notify", lambda title, message: calls.append((title, message)))
    return calls


class FakeAdapter:
    def __init__(self, name):
        self.name, self.sent = name, []

    def send(self, chat, text, buttons=None):
        self.sent.append((str(chat), text))


def _connectors(**routes):
    doc = tomlkit.document()
    conns = doc.setdefault("connectors", tomlkit.table())
    for name, r in routes.items():
        t = tomlkit.table()
        t.update({"role": "messaging", "adapter": "telegram", "token": "${TG}", "allow": ["1"], "routes": r})
        conns[name] = t
    machine_dir().mkdir(parents=True, exist_ok=True)
    (machine_dir() / "machine.toml").write_text(tomlkit.dumps(doc))


def test_default_events_without_a_notify_table():
    assert C.NOTIFY["events"] == NINE


def test_send_calls_attention_once_and_names_the_card(desktop):
    assert notifications.send("pr_opened", "PR opened: card0001 #77", "Add a thing") is True
    assert desktop == [("PR opened: card0001 #77", "Add a thing")]


def test_send_reaches_only_connectors_routed_for_alerts(desktop, monkeypatch):
    from pl import chat
    from pl.chat import gates
    _connectors(slack=["alerts"], tg=["gates"])
    fakes = {n: FakeAdapter(n) for n in ("slack", "tg")}
    monkeypatch.setattr(chat, "adapter", lambda n: fakes[n])
    notifications.send("merged", "Merged: card0001 #77", "Add a thing")
    gates.post_outbox()
    assert fakes["slack"].sent == [("1", "Merged: card0001 #77: Add a thing")]
    assert fakes["tg"].sent == []


def test_event_left_out_sends_nothing(desktop):
    C.NOTIFY = {"events": ["merged"]}
    assert notifications.send("pr_opened", "PR opened: card0001 #77", "x") is False
    assert desktop == []
    assert not (machine_dir() / "chat-outbox.jsonl").exists()


def test_send_in_dry_run_sends_nothing(desktop, monkeypatch):
    monkeypatch.setattr(notifications.trackers, "dry_run", lambda: True)
    assert notifications.send("merged", "t", "m") is False
    assert desktop == []


def test_a_broken_outbox_never_raises(desktop, monkeypatch):
    monkeypatch.setattr(notifications.manager, "machine_dir", lambda: (_ for _ in ()).throw(OSError("full")))
    assert notifications.send("merged", "t", "m") is True


def test_bad_events_value_is_a_config_error():
    errs = C.validate(tomlkit.parse('[notify]\nevents = ["merged", "nope"]\n'))
    assert any("notify.events" in e for e in errs)
    assert not any("notify.events" in e for e in C.validate(tomlkit.parse('[notify]\nevents = ["merged"]\n')))
    assert any("notify.events" in e for e in C.validate(tomlkit.parse('[notify]\nevents = "merged"\n')))


def test_outbox_line_is_private_json(desktop):
    notifications.send("done", "Done: card0001", "x")
    path = machine_dir() / "chat-outbox.jsonl"
    assert json.loads(path.read_text()) == {"key": "notify", "headline": "Done: card0001: x", "route": "alerts"}
    assert path.stat().st_mode & 0o777 == 0o600
