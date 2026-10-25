import hmac
import ipaddress
import json
import os
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import mcp.types as types

from pl import config as C
from pl.trackers import localstore, mcp


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


def build(store_path) -> FastMCP:
    p = Path(store_path)
    localstore.open_db(p).close()

    srv = FastMCP("local-board")

    def _exec(fn, *a, **kw):
        conn = localstore.open_db(p)
        try:
            reply = fn(conn, *a, **kw)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(reply))])
        except localstore.ProtocolError as e:
            msg = f"{e.code}: {e.message}" if e.message else e.code
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=msg)])
        finally:
            conn.close()

    @srv.tool()
    def pl_protocol(board_id: str = "") -> types.CallToolResult:
        return _exec(localstore.protocol, board_id=board_id or None)

    @srv.tool()
    def pl_columns(board_id: str = "") -> types.CallToolResult:
        return _exec(localstore.columns, board_id=board_id or None)

    @srv.tool()
    def pl_ensure_column(board_id: str = "", title: str = "", description: str = "") -> types.CallToolResult:
        return _exec(localstore.ensure_column, title, description=description, board_id=board_id or None)

    @srv.tool()
    def pl_cards(
        board_id: str = "",
        cursor: str | None = None,
        limit: int = 100,
        assigned_to: str | None = None,
        updated_since: str | None = None,
    ) -> types.CallToolResult:
        return _exec(
            localstore.cards,
            cursor=cursor,
            limit=limit,
            assigned_to=assigned_to,
            updated_since=updated_since,
            board_id=board_id or None,
        )

    @srv.tool()
    def pl_card(board_id: str = "", id: str = "") -> types.CallToolResult:
        return _exec(localstore.card, id, board_id=board_id or None)

    @srv.tool()
    def pl_create(
        board_id: str = "",
        column_id: str = "",
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        metadata: dict | None = None,
        assigned_to: str | None = None,
    ) -> types.CallToolResult:
        return _exec(
            localstore.create,
            column_id,
            title,
            description=description,
            tags=tags,
            metadata=metadata,
            assigned_to=assigned_to,
            board_id=board_id or None,
        )

    @srv.tool()
    def pl_update(board_id: str = "", id: str = "", fields: dict | None = None) -> types.CallToolResult:
        return _exec(localstore.update, id, fields=fields, board_id=board_id or None)

    @srv.tool()
    def pl_move(board_id: str = "", id: str = "", column_id: str = "") -> types.CallToolResult:
        return _exec(localstore.move, id, column_id, board_id=board_id or None)

    @srv.tool()
    def pl_delete(board_id: str = "", id: str = "") -> types.CallToolResult:
        return _exec(localstore.delete, id, board_id=board_id or None)

    return srv


def _store_path():
    p = C.TRACKER.get("path") or "board.db"
    return (C.CONFIG_DIR / p) if C.CONFIG_DIR else Path(p)


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
    if C.TRACKER.get("type") != "local":
        print("pl board serve: only a local board can be served", file=sys.stderr)
        raise SystemExit(2)
    srv = build(_store_path())
    if listen:
        import uvicorn
        app = _auth_app(srv.streamable_http_app(), token=token)
        uvicorn.run(app, host=host, port=port)
    else:
        srv.run(transport="stdio")
