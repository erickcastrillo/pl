"""Anomaly alerts: unknown chat user, send spike, sign-in from a new host."""
import json
import pytest

from pl import alerts, dispatch
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


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


def test_an_unknown_chat_user_opens_an_alert(clock):
    _write_audit({"action": "chat.unknown_user", "channel": "general", "when": clock["now"]})
    dispatch.check_alerts([], set())
    a = alerts.get("chat_unknown:general")
    assert a is not None
    assert "Unknown chat user in general" in a["title"]


def test_more_than_three_times_the_7_day_hourly_average_opens_a_send_spike(clock):
    now = clock["now"]
    for h in range(1, 168, 2):
        _write_audit({"action": "connector.write", "connector": "slack", "when": now - h * 3600})
    for _ in range(4):
        _write_audit({"action": "connector.write", "connector": "slack", "when": now - 60})
    dispatch.check_alerts([], set())
    a = alerts.get("send_spike:slack")
    assert a is not None
    assert "Send spike on slack" in a["title"]


def test_a_steady_rate_opens_nothing(clock):
    now = clock["now"]
    for h in range(168):
        for _ in range(4):
            _write_audit({"action": "connector.write", "connector": "slack", "when": now - h * 3600 + 10})
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
