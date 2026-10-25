"""Plan 11 slice 3: OAuth sign-in and `pl connectors add`, against a fake auth server on httpx.MockTransport."""
import argparse
import asyncio
import json
import logging
import os
import stat
import time
import urllib.parse
import urllib.request

import httpx
import mcp.client.stdio  # noqa: F401  (bind stdio_client's errlog=sys.stderr default now, not inside a capsys test)
import pytest

from pl import config as C
from pl import connectors, connectors_oauth, secrets

ORIGIN = "https://mcp.example.com"
URL = f"{ORIGIN}/mcp"
ACCESS, REFRESH, CODE = "at-fake-9f3a", "rt-fake-77b1", "code-fake-5c2d"


class FakeServer:
    def __init__(self):
        self.registered, self.token_forms = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"resource": URL, "authorization_servers": [ORIGIN]})
        if path.startswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={
                "issuer": ORIGIN, "authorization_endpoint": f"{ORIGIN}/authorize", "token_endpoint": f"{ORIGIN}/token",
                "registration_endpoint": f"{ORIGIN}/register", "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})
        if path == "/register":
            meta = json.loads(request.content)
            self.registered.append(meta)
            return httpx.Response(201, json={**meta, "client_id": "client-1"})
        if path == "/token":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.token_forms.append(form)
            if form.get("code") != CODE or not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer",
                                             "expires_in": 3600})
        if path == "/mcp":
            return self.mcp(request)
        return httpx.Response(404)

    def mcp(self, request):
        if request.headers.get("authorization") != f"Bearer {ACCESS}":
            return httpx.Response(401, headers={
                "www-authenticate": f'Bearer resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource"'})
        if request.method == "GET":
            return httpx.Response(405)
        if request.method != "POST":
            return httpx.Response(200)
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        results = {
            "initialize": {"protocolVersion": msg.get("params", {}).get("protocolVersion"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake", "version": "1.0.0"}},
            "tools/list": {"tools": [
                {"name": "payments_since", "description": "read", "inputSchema": {"type": "object"},
                 "annotations": {"readOnlyHint": True}},
                {"name": "refund", "description": "write", "inputSchema": {"type": "object"}}]},
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": results[msg["method"]]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("PL_KEYCHAIN", "off")
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    server = FakeServer()
    monkeypatch.setattr(connectors_oauth, "_transport", httpx.MockTransport(server))
    server.opened = []
    server.state = None

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        server.opened.append(url)
        callback = query["redirect_uri"] + "?" + urllib.parse.urlencode(
            {"code": CODE, "state": server.state or query["state"]})
        with urllib.request.urlopen(callback, timeout=10) as r:
            server.page = r.read().decode()
        return True
    monkeypatch.setattr(connectors_oauth, "_open", browser)
    return server


def add_args(**kw):
    base = dict(connectors_cmd="add", name="billing", command=[URL], url=None, pin=None, team=["billing"],
                role=["payments"], access="read", tier="vendor", yes=True, json=False)
    return argparse.Namespace(**{**base, **kw})


def test_add_discovers_registers_opens_the_browser_and_takes_the_callback(env, tmp_path, capsys):
    connectors.cmd_connectors(add_args())

    assert "connected (2 tools)" in capsys.readouterr().out
    assert len(env.opened) == 1 and env.opened[0].startswith(f"{ORIGIN}/authorize?")
    assert env.registered[0]["redirect_uris"][0].startswith("http://127.0.0.1:")
    assert "signed in" in env.page
    table = connectors.load()["billing"]
    assert (table["auth"], table["role"], table["teams"], table["access"]) == ("oauth", ["payments"], ["billing"], "read")
    stored = secrets.get("billing")
    assert (stored["access_token"], stored["refresh_token"]) == (ACCESS, REFRESH)
    assert 3500 < stored["expires_at"] - time.time() <= 3600
    assert stat.S_IMODE(os.stat(tmp_path / "machine" / "secrets" / "billing.json").st_mode) == 0o600


def test_a_wrong_state_on_the_callback_fails_and_stores_nothing(env):
    env.state = "not-the-state"

    with pytest.raises(SystemExit, match="(?i)state"):
        connectors.cmd_connectors(add_args())

    assert "access_token" not in (secrets.get("billing") or {})
    assert env.token_forms == []


def test_tokens_never_reach_config_events_logs_or_stdout(env, tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)

    connectors.cmd_connectors(add_args(json=True))

    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "connected"
    leaks = [f for f in (tmp_path / "machine" / "connectors.toml", *tmp_path.rglob("events.jsonl"), *tmp_path.rglob("mcp-*.log"))
             if f.exists() and any(t in f.read_text() for t in (ACCESS, REFRESH, CODE))]
    assert leaks == []
    for text in (out.out, out.err, caplog.text):
        assert not any(t in text for t in (ACCESS, REFRESH, CODE))


def test_add_without_yes_and_no_tty_exits_2(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", open(os.devnull))

    with pytest.raises(SystemExit) as e:
        connectors.cmd_connectors(add_args(yes=False))

    assert e.value.code == 2 and "--yes" in capsys.readouterr().err
    assert connectors.load() == {} and env.opened == []


def test_an_existing_name_is_left_unchanged(env, capsys):
    connectors.cmd_connectors(add_args())
    capsys.readouterr()

    connectors.cmd_connectors(add_args(role=["email"]))

    assert "already exists, unchanged" in capsys.readouterr().out
    assert connectors.load()["billing"]["role"] == ["payments"] and len(env.opened) == 1


def test_a_stored_token_reloads_with_the_time_left(env):
    connectors.cmd_connectors(add_args())

    tokens = asyncio.run(connectors_oauth.Storage("billing").get_tokens())

    assert tokens.access_token == ACCESS and 3500 < tokens.expires_in <= 3600


def test_a_session_that_cannot_open_a_browser_needs_sign_in(env):
    redirect = connectors_oauth.provider("billing", URL, interactive=False).context

    with pytest.raises(connectors_oauth.NeedsSignIn):
        asyncio.run(redirect.redirect_handler("https://x/authorize"))
    assert env.opened == []
