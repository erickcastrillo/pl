"""OAuth sign-in for connectors (the mcp SDK's OAuth 2.1 client) and the audit records for it.
Tokens and the registered client live in pl.secrets, never in config, logs or output."""
import asyncio
import concurrent.futures
import http.server
import sys
import time
import urllib.parse
import webbrowser

import httpx

from pl import audit
from pl import config as C
from pl import secrets

CALLBACK_WAIT = 300
_open = webbrowser.open
_transport = None   # tests put an httpx.MockTransport here


class NeedsSignIn(Exception):
    pass


def record_sign_in(connector: str, host: str | None = None, **kwargs) -> None:
    """Record an OAuth sign-in event with the client host."""
    audit.append("oauth.sign_in", connector=connector, host=host or C.HOST, **kwargs)


def record_token_use(connector: str, host: str | None = None, **kwargs) -> None:
    """Record a connector token usage event with the client host."""
    audit.append("oauth.token", connector=connector, host=host or C.HOST, **kwargs)


def http_client(headers=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, headers=headers, auth=auth, timeout=httpx.Timeout(30, read=300))


class Storage:
    """The SDK's TokenStorage on pl.secrets: tokens, expiry and the registered client in one entry."""

    def __init__(self, name):
        self.name = name

    def _read(self):
        return secrets.get(self.name) or {}

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = self._read()
        if not d.get("access_token"):
            return None
        left = max(0, int(d["expires_at"] - time.time())) if d.get("expires_at") else None
        return OAuthToken(access_token=d["access_token"], token_type="Bearer", refresh_token=d.get("refresh_token"),
                          scope=d.get("scope"), expires_in=left)

    async def set_tokens(self, tokens):
        d = {**self._read(), **tokens.model_dump(mode="json", exclude_none=True, exclude={"expires_in"})}
        if tokens.expires_in is not None:
            d["expires_at"] = time.time() + tokens.expires_in
        secrets.put(self.name, d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        client = self._read().get("client")
        return OAuthClientInformationFull.model_validate(client) if client else None

    async def set_client_info(self, client_info):
        secrets.put(self.name, {**self._read(), "client": client_info.model_dump(mode="json", exclude_none=True)})


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        ok = url.path == "/callback" and "code" in query
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"signed in, close this tab" if ok else b"sign-in failed, close this tab")
        if ok:
            self.server.result = (query["code"], query.get("state"))

    def log_message(self, *args):
        pass


def _wait_for_callback(server):
    server.timeout, server.result = 1, None
    deadline = time.monotonic() + CALLBACK_WAIT
    try:
        while server.result is None:
            if time.monotonic() > deadline:
                raise TimeoutError("sign-in timed out: no browser callback")
            server.handle_request()
        return server.result
    finally:
        server.server_close()


def provider(name: str, url: str, interactive: bool):
    """An OAuthClientProvider for connector `name`. The callback port is bound first so the registered redirect_uri matches."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pl.trackers import mcp
    storage = Storage(name)
    known = storage._read().get("client")
    port = urllib.parse.urlsplit(known["redirect_uris"][0]).port if known else 0
    server = http.server.HTTPServer(("127.0.0.1", port), _Callback)
    waiting = concurrent.futures.Future()

    async def redirect_handler(auth_url):
        if not interactive:
            server.server_close()
            raise NeedsSignIn(f"connector {name} needs sign in: run pl connectors add {name}")
        pool = concurrent.futures.ThreadPoolExecutor(1)
        waiting.set_running_or_notify_cancel()
        pool.submit(_wait_for_callback, server).add_done_callback(lambda f: _settle(waiting, f))
        pool.shutdown(wait=False)
        print(f"pl: opening the browser to sign in to {name}; if nothing opens, visit:\n  {auth_url}", file=sys.stderr)
        _open(auth_url)

    async def callback_handler():
        code, state = await asyncio.wrap_future(waiting)
        mcp._secrets.add(code)
        return code, state

    meta = OAuthClientMetadata(
        client_name="pl", redirect_uris=[f"http://127.0.0.1:{server.server_address[1]}/callback"],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"], token_endpoint_auth_method="none")
    return OAuthClientProvider(url, meta, storage, redirect_handler, callback_handler, timeout=CALLBACK_WAIT)


def _settle(future, done):
    if done.exception():
        future.set_exception(done.exception())
    else:
        future.set_result(done.result())
