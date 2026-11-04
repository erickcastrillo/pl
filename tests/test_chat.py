import json
import os
import subprocess
import sys
import pytest


@pytest.fixture(autouse=True)
def machine_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("TG_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")


def _run_chat(*args, env=None):
    cmd = [sys.executable, "-m", "pl", "chat", *args]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return r


def test_add_writes_a_messaging_connector_with_a_var_reference():
    r = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r.returncode == 0, r.stderr
    from pl import chat
    connectors = chat.load()
    assert "telegram" in connectors
    c = connectors["telegram"]
    assert c["role"] == "messaging"
    assert c["adapter"] == "telegram"
    assert c["token"] == "${TG_TOKEN}"
    assert c["allow"] == ["123"]
    assert c["routes"] == ["gates"]


def test_a_literal_token_is_refused_with_exit_2():
    r = _run_chat("add", "telegram", "--token", "123456:ABC-DEF", "--allow", "123", "--yes")
    assert r.returncode == 2
    assert "token" in r.stderr.lower()
    assert ("--token-env" in r.stderr or "quote" in r.stderr)


def test_add_again_says_already_exists_unchanged():
    r1 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r1.returncode == 0
    r2 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r2.returncode == 0
    assert "already exists, unchanged" in r2.stdout

    r3 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "456", "--yes")
    assert r3.returncode != 0
    assert "--replace" in r3.stderr or "--replace" in r3.stdout


def test_list_json_never_shows_the_token():
    _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    r = _run_chat("list", "--json")
    assert r.returncode == 0, r.stderr
    assert "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" not in r.stdout
    data = json.loads(r.stdout)
    assert "connectors" in data or "telegram" in data
    # verify token in the json is '${TG_TOKEN}'
    val = data["connectors"]["telegram"] if "connectors" in data else data["telegram"]
    assert val["token"] == "${TG_TOKEN}"


def test_safe_text_strips_email_amount_address_and_token(monkeypatch):
    from pl.chat import safe_text
    from pl.trackers import mcp

    # Ensure token secret is tracked by mcp._secrets
    mcp._secrets.add("supersecrettoken123")

    msg = (
        "Hello alice@example.com, please pay $500 or 250 USD at 123 Main Street or 456 Elm Ave. "
        "Your account code is 12345678 and token is supersecrettoken123."
    )
    res = safe_text(msg)
    assert "alice@example.com" not in res
    assert "$500" not in res
    assert "250 USD" not in res
    assert "123 Main Street" not in res
    assert "456 Elm Ave" not in res
    assert "12345678" not in res
    assert "supersecrettoken123" not in res
    assert len(res) <= 300


def test_telegram_send_uses_the_fake_api_only(monkeypatch):
    from pl.chat.telegram import Telegram

    calls = []

    def fake_http(token, method, payload=None):
        calls.append((token, method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr("pl.chat.telegram._http", fake_http)

    tg = Telegram("telegram", {"token": "${TG_TOKEN}", "allow": ["123"]})
    tg.send("123", "hello world", buttons=[[{"text": "Approve", "callback_data": "app:1"}]])

    assert len(calls) == 1
    token, method, payload = calls[0]
    assert token == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert method == "sendMessage"
    assert payload["chat_id"] == "123"
    assert payload["text"] == "hello world"
    assert "reply_markup" in payload


def test_an_api_error_never_shows_the_token(monkeypatch):
    import urllib.error
    from pl.chat.telegram import _http

    def fake_open(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMessage",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(Exception) as excinfo:
        _http("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", "sendMessage", {})

    err_str = str(excinfo.value)
    assert "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" not in err_str


def test_token_absent_from_logs_events_and_json(tmp_path, monkeypatch):
    from pathlib import Path
    state_dir = tmp_path / "machine"
    monkeypatch.setenv("PL_MACHINE_DIR", str(state_dir))

    # Add connector
    _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")

    # Run test command with fake API
    script = (
        "import sys\n"
        "from pl.chat import cmd_chat\n"
        "import pl.chat.telegram\n"
        "def fake_http(token, method, payload=None):\n"
        "    return {'ok': True, 'result': {'message_id': 1}}\n"
        "pl.chat.telegram._http = fake_http\n"
        "cmd_chat(['test', 'telegram'])\n"
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=os.environ)
    assert r.returncode == 0

    secret = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert secret not in r.stdout
    assert secret not in r.stderr

    for p in Path(state_dir).rglob("*"):
        if p.is_file():
            assert secret not in p.read_text(errors="ignore"), f"secret leaked in {p}"



def _two_connectors(monkeypatch):
    from pl import manager
    conns = {
        "s": {"role": "messaging", "adapter": "slack", "token": "${SL_TOKEN}", "allow": ["U1"]},
        "d": {"role": "messaging", "adapter": "discord", "token": "${DC_TOKEN}", "allow": ["1"]},
        "i": {"role": "messaging", "adapter": "irc", "token": "${IRC_TOKEN}", "allow": ["1"]},
    }
    monkeypatch.setattr(manager, "_machine_toml", lambda: {"connectors": conns})


def test_adapter_builds_slack_and_discord_connectors(monkeypatch):
    from pl import chat
    from pl.chat.discord import Discord
    from pl.chat.slack import Slack
    _two_connectors(monkeypatch)
    assert isinstance(chat.adapter("s"), Slack)
    assert isinstance(chat.adapter("d"), Discord)


def test_adapter_unknown_type_still_raises(monkeypatch):
    from pl import chat
    _two_connectors(monkeypatch)
    with pytest.raises(KeyError, match="unknown adapter"):
        chat.adapter("i")


def test_route_with_no_names_clears_the_kind():
    _run_chat("add", "telegram", "--name", "tg", "--token", "${TG_TOKEN}", "--allow", "1", "--yes")
    _run_chat("add", "slack", "--name", "sl", "--token", "${TG_TOKEN}", "--allow", "U1", "--yes")
    assert _run_chat("route", "digest", "tg", "sl").returncode == 0
    assert _run_chat("route", "gates", "tg").returncode == 0
    r = _run_chat("route", "digest")
    assert r.returncode == 0, r.stderr
    listed = json.loads(_run_chat("list", "--json").stdout)["connectors"]
    assert listed["tg"]["routes"] == ["gates"]
    assert listed["sl"]["routes"] == []


def test_route_unknown_name_still_fails():
    _run_chat("add", "telegram", "--name", "tg", "--token", "${TG_TOKEN}", "--allow", "1", "--yes")
    r = _run_chat("route", "alerts", "nope")
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_route_keeps_token_reference_not_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "sekret-123456")
    _run_chat("add", "telegram", "--name", "tg", "--token-env", "TG_TOKEN", "--allow", "1", "--yes")
    assert _run_chat("route", "alerts", "tg").returncode == 0
    written = "".join(p.read_text() for p in (tmp_path / "machine").rglob("*.toml"))
    assert "${TG_TOKEN}" in written
    assert "sekret-123456" not in written
