"""Plan 11 slice 5: `pl connectors test` makes one read call, `pl connectors revoke` disables and forgets the sign-in."""
import argparse
import datetime
import json
import urllib.parse
import urllib.request

import httpx
import pytest
from test_connectors_oauth import ACCESS, CODE, ORIGIN, REFRESH, FakeServer, add_args

from pl import config as C
from pl import connectors, connectors_oauth, secrets

LOOKUP = {"name": "lookup", "description": "read", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}
NEEDS_ARG = {**LOOKUP, "name": "needs_arg", "inputSchema": {"type": "object", "required": ["id"]}}
REFUND = {"name": "refund", "description": "write", "inputSchema": {"type": "object"}}


class Server(FakeServer):
    """The fake OAuth server, plus tools/call, a configurable tool list and an RFC 7009 revocation endpoint."""

    def __init__(self):
        super().__init__()
        self.tools, self.calls, self.revoked, self.revoke_down = None, [], [], False

    def __call__(self, request):
        if request.url.path.startswith("/.well-known/oauth-authorization-server"):
            doc = super().__call__(request).json()
            return httpx.Response(200, json={**doc, "revocation_endpoint": f"{ORIGIN}/revoke"})
        if request.url.path == "/revoke":
            if self.revoke_down:
                raise httpx.ConnectError("down", request=request)
            self.revoked.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            return httpx.Response(200)
        return super().__call__(request)

    def mcp(self, request):
        msg = json.loads(request.content) if request.method == "POST" and request.content else {}
        if request.headers.get("authorization") == f"Bearer {ACCESS}" and "id" in msg:
            if msg["method"] == "tools/call":
                self.calls.append(msg["params"])
                text = f"3 payments since {msg['params']['arguments'].get('since')} (token {ACCESS})"
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}})
            if msg["method"] == "tools/list" and self.tools is not None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": self.tools}})
        return super().mcp(request)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("PL_KEYCHAIN", "off")
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    fake = Server()
    monkeypatch.setattr(connectors_oauth, "_transport", httpx.MockTransport(fake))

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        urllib.request.urlopen(query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": CODE, "state": query["state"]}), timeout=10).read()
        return True
    monkeypatch.setattr(connectors_oauth, "_open", browser)
    return fake


def sign_in(**kw):
    connectors.cmd_connectors(add_args(**kw))


def run(sub, name="billing", **kw):
    connectors.cmd_connectors(argparse.Namespace(**{"connectors_cmd": sub, "name": name, "json": False, "yes": False, **kw}))


def test_test_makes_one_read_call_and_prints_result_and_time(server, capsys):
    sign_in()
    capsys.readouterr()

    run("test")

    out = capsys.readouterr().out
    assert [c["name"] for c in server.calls] == ["payments_since"]
    assert server.calls[0]["arguments"] == {"since": datetime.date.today().isoformat()}
    assert "payments_since" in out and " ms" in out and "3 payments since" in out
    assert ACCESS not in out and "(token ***)" in out


def test_test_never_picks_a_write_tool(server, capsys):
    sign_in(role=None)
    server.tools = [REFUND, NEEDS_ARG]

    with pytest.raises(SystemExit) as exc:
        run("test")

    assert exc.value.code == 2 and "set [connectors.billing] test" in capsys.readouterr().err
    assert server.calls == []

    server.tools = [REFUND, NEEDS_ARG, LOOKUP]
    run("test")
    assert [c["name"] for c in server.calls] == ["lookup"]


def test_test_uses_the_test_mapping_and_exits_1_when_the_call_fails(server, capsys):
    sign_in()
    connectors._edit_config(lambda table: table["billing"].__setitem__("test", {"tool": "lookup", "args": {"id": 7}}))
    server.tools = [LOOKUP, REFUND]
    run("test")
    assert server.calls == [{"name": "lookup", "arguments": {"id": 7}}]

    secrets.delete("billing")
    with pytest.raises(SystemExit) as exc:
        run("test")
    assert exc.value.code == 1


def test_revoke_deletes_tokens_and_disables_in_one_command(server, capsys):
    sign_in()
    capsys.readouterr()

    run("revoke", yes=True)

    out = capsys.readouterr().out
    assert connectors.load()["billing"]["enabled"] is False
    assert secrets.get("billing") is None
    assert server.revoked and {f["token"] for f in server.revoked} == {REFRESH, ACCESS}
    assert "server" in out and "tokens" in out and ACCESS not in out and REFRESH not in out


def test_revoke_still_disables_when_the_server_is_unreachable(server, capsys):
    sign_in()
    server.revoke_down = True
    capsys.readouterr()

    run("revoke", yes=True)

    assert connectors.load()["billing"]["enabled"] is False
    assert secrets.get("billing") is None
    assert "not reached" in capsys.readouterr().out


def test_revoke_keeps_the_connector_disabled_and_exits_1_when_the_delete_fails(server, monkeypatch):
    sign_in()
    monkeypatch.setattr(secrets, "delete", lambda name: (_ for _ in ()).throw(OSError("denied")))

    with pytest.raises(SystemExit) as exc:
        run("revoke", yes=True)

    assert exc.value.code == 1 and connectors.load()["billing"]["enabled"] is False


def test_revoke_without_yes_and_without_a_terminal_changes_nothing(server, monkeypatch):
    sign_in()
    monkeypatch.setattr("sys.stdin", open("/dev/null"))

    with pytest.raises(SystemExit) as exc:
        run("revoke")

    assert exc.value.code == 2
    assert connectors.load()["billing"]["enabled"] is True and secrets.get("billing")
