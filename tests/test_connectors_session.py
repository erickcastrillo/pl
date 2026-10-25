"""Plan 11 slice 4: connector sessions refresh on their own, a failed refresh needs a new sign-in, tools are marked read or write."""
import sys
import textwrap
import time
import urllib.parse
import urllib.request

import httpx
import pytest
from test_connectors_oauth import ACCESS, CODE, REFRESH, FakeServer, add_args

from pl import alerts, connectors, connectors_oauth, dispatch
from pl import config as C

READ_ONLY_SERVER = textwrap.dedent('''
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    srv = FastMCP("annotated")

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def lookup() -> str:
        return "ok"

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=False))
    def explicit_write() -> str:
        return "ok"

    @srv.tool()
    def unmarked() -> str:
        return "ok"

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def listed_as_write() -> str:
        return "ok"

    srv.run(transport="stdio")
''')


class ExpiringServer(FakeServer):
    """Issues access tokens that last one second; a refresh gets a new one unless refresh_ok is off."""

    def __init__(self):
        super().__init__()
        self.valid, self.expires, self.refresh_ok, self.refreshes = None, 0, True, 0

    def __call__(self, request):
        if request.url.path == "/token":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if form.get("grant_type") == "refresh_token":
                if not self.refresh_ok:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refreshes += 1
                self.valid, self.expires = f"at-refreshed-{self.refreshes}", time.time() + 3600
            elif form.get("code") == CODE:
                self.valid, self.expires = ACCESS, time.time() + 1
            else:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": self.valid, "refresh_token": REFRESH, "token_type": "Bearer",
                                             "expires_in": 1 if self.valid == ACCESS else 3600})
        return super().__call__(request)

    def mcp(self, request):
        if request.headers.get("authorization") != f"Bearer {self.valid}" or time.time() > self.expires:
            return httpx.Response(401, headers={
                "www-authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'})
        request.headers["authorization"] = f"Bearer {ACCESS}"
        return super().mcp(request)


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("PL_KEYCHAIN", "off")
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    server = ExpiringServer()
    monkeypatch.setattr(connectors_oauth, "_transport", httpx.MockTransport(server))
    server.opened = []

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        server.opened.append(url)
        callback = query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": CODE, "state": query["state"]})
        urllib.request.urlopen(callback, timeout=10).read()
        return True
    monkeypatch.setattr(connectors_oauth, "_open", browser)
    connectors.cmd_connectors(add_args())
    time.sleep(1.2)
    return server


def test_an_expired_token_is_refreshed_without_asking(signed_in):
    listed = connectors.tools("billing")

    assert {t["name"] for t in listed} == {"payments_since", "refund"}
    assert signed_in.refreshes == 1 and len(signed_in.opened) == 1
    assert alerts.get("connector_signin:billing") is None
    assert connectors.status()["billing"] == "connected"


def test_a_failed_refresh_opens_an_alert_and_marks_needs_signin(signed_in, tmp_path):
    signed_in.refresh_ok = False

    with pytest.raises(connectors_oauth.NeedsSignIn):
        connectors.tools("billing")

    alert = alerts.get("connector_signin:billing")
    assert alert["title"] == "Connector billing needs sign-in" and alert["fix"] == "pl connectors add billing --reauth"
    assert connectors.load_state()["billing"]["status"] == "needs_signin"
    assert connectors.status()["billing"] == "needs sign-in"
    assert len(signed_in.opened) == 1
    state_file = tmp_path / "machine" / "connectors-state.json"
    assert oct(state_file.stat().st_mode & 0o777) == "0o600"
    assert not any(t in state_file.read_text() for t in (ACCESS, REFRESH, CODE))


def test_the_dispatcher_keeps_the_sign_in_alert_until_the_connector_works_again(signed_in):
    signed_in.refresh_ok = False
    with pytest.raises(connectors_oauth.NeedsSignIn):
        connectors.call("billing", "payments_since")

    dispatch.check_alerts([], set(), st={})
    assert not alerts.get("connector_signin:billing").get("resolved_at")

    signed_in.refresh_ok = True
    connectors.tools("billing")
    dispatch.check_alerts([], set(), st={})

    assert alerts.get("connector_signin:billing").get("resolved_at")
    assert connectors.status()["billing"] == "connected"


@pytest.fixture
def annotated(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    script = tmp_path / "annotated.py"
    script.write_text(READ_ONLY_SERVER)
    return script


def test_tools_without_read_only_hint_are_write_tools(annotated):
    connectors.add("notes", command=[sys.executable, str(annotated)])

    listed = {t["name"]: t for t in connectors.tools("notes")}

    assert {n: t["write"] for n, t in listed.items()} == {
        "lookup": False, "explicit_write": True, "unmarked": True, "listed_as_write": False}
    assert all("description" in t for t in listed.values())
    saved = connectors.load_state()["notes"]
    assert set(saved["approved_snapshot"]) == set(listed) and saved["approved_version"]


def test_write_tools_override_wins(annotated):
    connectors._edit_config(lambda table: table.__setitem__("notes", {
        "command": [sys.executable, str(annotated)], "write_tools": ["listed_as_write"], "read_tools": ["unmarked", "listed_as_write"]}))

    listed = {t["name"]: t["write"] for t in connectors.tools("notes")}

    assert listed["listed_as_write"] is True and listed["unmarked"] is False
