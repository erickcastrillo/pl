import json
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import mcp.types as types

from pl import config as C
from pl.trackers import localstore


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


def run(args):
    if not getattr(args, "stdio", False) and not getattr(args, "listen", None):
        print("pl board serve: give --stdio or --listen HOST:PORT", file=sys.stderr)
        sys.exit(2)
    if C.TRACKER.get("type") != "local":
        print("pl board serve: only a local board can be served", file=sys.stderr)
        sys.exit(2)
    p = C.TRACKER.get("path") or "board.db"
    store_path = (C.CONFIG_DIR / p) if C.CONFIG_DIR else Path(p)
    if getattr(args, "stdio", False):
        srv = build(store_path)
        srv.run(transport="stdio")
    elif getattr(args, "listen", None):
        print("pl board serve: --listen is not yet implemented", file=sys.stderr)
        sys.exit(2)
