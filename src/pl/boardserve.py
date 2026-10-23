import hmac
import ipaddress
import json
import os
import sys
from mcp.server.fastmcp import FastMCP
import uvicorn
from pl import config as C
from pl.trackers import mcp


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _auth_app(app, token: str | None):
    if token:
        mcp._secrets.add(token)
        mcp._mask_loggers()

    async def asgi_app(scope, receive, send):
        if scope["type"] == "http":
            client = scope.get("client")
            client_host = client[0] if client else None
            if not token:
                if not client_host or not _is_loopback(client_host):
                    res = b"Forbidden"
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(res)).encode())]})
                    await send({"type": "http.response.body", "body": res})
                    return
            else:
                auth = next((v.decode("latin1") for k, v in scope.get("headers", []) if k.lower() == b"authorization"), None)
                if not auth or not hmac.compare_digest(auth, f"Bearer {token}"):
                    res = b"Unauthorized"
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(res)).encode())]})
                    await send({"type": "http.response.body", "body": res})
                    return
        await app(scope, receive, send)

    return asgi_app


def build(store_path=None):
    srv = FastMCP("pl-board")

    def _call(name, fallback, *args, **kwargs):
        try:
            from pl.trackers import localstore
            db = localstore.open_db(store_path or (C.CONFIG_DIR / "board.db"))
            return json.dumps(getattr(localstore, name)(db, *args, **kwargs))
        except (ImportError, AttributeError, Exception):
            return json.dumps(fallback() if callable(fallback) else fallback)

    @srv.tool()
    def pl_protocol() -> str:
        return _call("protocol", {"version": 1, "boards": [{"id": "b-1", "name": "Local board"}]})

    @srv.tool()
    def pl_columns(board_id: str = "") -> str:
        return _call("columns", lambda: {"columns": [{"id": f"col-{i+1}", "title": n} for i, n in enumerate(C.COLUMNS)]},
                     board_id=board_id or None)

    @srv.tool()
    def pl_ensure_column(board_id: str = "", title: str = "", description: str = "") -> str:
        return _call("ensure_column", {"column": {"id": "col-new", "title": title}},
                     title, description=description, board_id=board_id or None)

    @srv.tool()
    def pl_cards(board_id: str = "", cursor: str | None = None, limit: int = 100,
                 assigned_to: str | None = None, updated_since: str | None = None) -> str:
        return _call("cards", {"cards": [], "next_cursor": None},
                     cursor=cursor, limit=limit, assigned_to=assigned_to, updated_since=updated_since, board_id=board_id or None)

    @srv.tool()
    def pl_card(board_id: str = "", id: str = "") -> str:
        return _call("card", {"card": {"id": id, "title": "", "description": "", "metadata": {}}}, id, board_id=board_id or None)

    @srv.tool()
    def pl_create(board_id: str = "", column_id: str = "", **fields) -> str:
        return _call("create", {"card": {"id": "new-1", "column_id": column_id, **fields}},
                     column_id, **fields, board_id=board_id or None)

    @srv.tool()
    def pl_update(board_id: str = "", id: str = "", fields: dict | None = None) -> str:
        return _call("update", {"card": {"id": id, **(fields or {})}}, id, fields=fields, board_id=board_id or None)

    @srv.tool()
    def pl_move(board_id: str = "", id: str = "", column_id: str = "") -> str:
        return _call("move", {"card": {"id": id, "column_id": column_id}}, id, column_id, board_id=board_id or None)

    @srv.tool()
    def pl_delete(board_id: str = "", id: str = "") -> str:
        return _call("delete", {"deleted": True}, id, board_id=board_id or None)

    return srv


def run(args):
    listen = getattr(args, "listen", None)
    token_env = getattr(args, "token_env", None)
    stdio = getattr(args, "stdio", False)
    if not listen and not stdio:
        print("pl board serve: give --stdio or --listen HOST:PORT", file=sys.stderr)
        raise SystemExit(2)
    token = None
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            print(f"pl board serve: --token-env {token_env} is empty or not set", file=sys.stderr)
            raise SystemExit(2)
    if listen:
        host, port_str = listen.rsplit(":", 1)
        port = int(port_str)
        if not token_env and not _is_loopback(host):
            print("--listen on a non-loopback address needs --token-env VAR", file=sys.stderr)
            raise SystemExit(2)
        srv = build()
        app = _auth_app(srv.streamable_http_app(), token=token)
        uvicorn.run(app, host=host, port=port)
    elif stdio:
        srv = build()
        srv.run(transport="stdio")
