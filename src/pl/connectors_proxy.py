"""connector tool calls with write gating and audit, plus the HTTP proxy that gives sandboxed agents scoped access."""
import asyncio
import http.server
import json
import secrets
import stat
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from pl import alerts, audit, connectors, outward
from pl import config as C


def is_write_forbidden(connector: str, tool: str) -> bool:
    return connectors.is_write_forbidden(connector, tool)


def record_tool_call(connector: str, tool: str, access: str = "read", result: str = "ok") -> None:
    audit.append(audit.CONNECTOR_TOOL, target=connector, detail={"tool": tool, "access": access, "result": result}, access=access)


def call_tool(connector: str, tool: str, args: dict | None = None, access: str = "read", handler=None):
    """One connector tool call: refused while a write tool's definition is unapproved; every call is audited."""
    result = "ok"
    try:
        if is_write_forbidden(connector, tool):
            raise PermissionError(f"Connector {connector}: write tool {tool} forbidden while tools are unapproved")
        if handler:
            return handler(tool, args or {})
        return connectors.call(connector, tool, args)
    except PermissionError:
        result = "forbidden"
        raise
    finally:
        record_tool_call(connector, tool, access=access, result=result)


class ProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


@dataclass
class SessionScope:
    session_id: str
    card_id: str
    stage: str
    team: str
    bearer: str
    connectors: dict = field(default_factory=dict)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        auth = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        scope = self.server.sessions.get(auth)
        if not scope:
            return self._send_json(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        args = body.get("arguments") or {}
        card = body.get("card_id") or body.get("id") or args.get("card_id") or args.get("id")
        if card and str(card) != str(scope.card_id):
            return self._send_json(403, {"error": f"refused: card {card} does not match scoped card {scope.card_id}"})
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/connectors/"):
            name = path.removeprefix("/connectors/")
            handler = scope.connectors.get(name)
            if not handler:
                return self._send_json(404, {"error": f"no connector {name}"})
            res = handler(body.get("tool", ""), args) if callable(handler) else handler
            return self._send_json(200, res)
        if path in ("/mcp", "/pl"):
            tool = body.get("tool", "")
            if tool == "pl_card":
                return self._send_json(200, {"id": scope.card_id, "stage": scope.stage})
            if tool == "pl_section":
                return self._send_json(200, {"section": args.get("section", ""), "content": ""})
        return self._send_json(200, {"ok": True})


class ProxyServer:
    def __init__(self, host: str, port: int = 0):
        if host in ("0.0.0.0", "", "::"):
            raise ValueError("Refusing to bind connectors_proxy to 0.0.0.0: must bind to bridge address")
        self.host = host
        self.server = http.server.HTTPServer((host, port), _ProxyHandler)
        self.server.sessions = {}
        self.port, self._thread = self.server.server_port, None

    def register_session(self, session_id: str, card_id: str, stage: str, team: str,
                         bearer: str | None = None, connectors: dict | None = None) -> SessionScope:
        token = bearer or secrets.token_hex(16)
        scope = SessionScope(session_id, str(card_id), stage, team, token, connectors or {})
        self.server.sessions[token] = scope
        return scope

    def start(self):
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if self._thread:
            self._thread.join(timeout=2)


def call_proxy(url: str, bearer: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ProxyError(e.code, e.read().decode("utf-8")) from e


def make_mcp_config(proxy_url: str, bearer: str, connectors: list[str] | None = None) -> dict:
    base = proxy_url.rstrip("/")
    h = {"Authorization": f"Bearer {bearer}"}
    return {"mcpServers": {"pl": {"type": "http", "url": f"{base}/mcp", "headers": h},
                           **{c: {"type": "http", "url": f"{base}/connectors/{c}", "headers": h} for c in (connectors or [])}}}


def write_mcp_config(path: Path, config: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2))
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return p


_PROXY: ProxyServer | None = None
_LOCK = threading.Lock()


def get_proxy(host: str | None = None, port: int = 0) -> ProxyServer:
    global _PROXY
    with _LOCK:
        if _PROXY is None:
            if host is None:
                from pl import sandbox
                host = sandbox.bridge_address()
            _PROXY = ProxyServer(host, port)
            _PROXY.start()
        return _PROXY


def stop_proxy():
    global _PROXY
    with _LOCK:
        if _PROXY:
            _PROXY.stop()
            _PROXY = None


class Forbidden(Exception):
    pass


def _read_write(name: str) -> bool:
    return (connectors.get(name) or {}).get("access") == "read-write"


def visible_tools(name: str) -> list[dict]:
    write_ok = _read_write(name)
    return [t for t in connectors.tools(name) if write_ok or not t.get("write")]


def _gate_before(stage: str) -> str | None:
    """The owner-gated stage nearest before `stage`: the person approved its output, which `stage` then sends."""
    names = list(((C.PIPELINE or {}).get("stages") or {}))
    if stage not in names:
        return None
    gated = [n for n in names[:names.index(stage)] if C.PIPELINE["stages"][n].get("gate") == "owner"]
    return gated[-1] if gated else None


def _gate_approved(card_id: str | None, stage: str | None) -> bool:
    """True when the gate before `stage` was approved in the card's current review round; a reject starts a new one."""
    from pl import board
    gate = _gate_before(stage) if (card_id and stage) else None
    if not gate:
        return False
    board.fresh_next()
    meta = board.find_card(card_id).get("metadata") or {}
    approval = (meta.get("gates") or {}).get(gate)
    return bool(approval) and approval.get("review_round", 0) == int(meta.get("review_round") or 0)


def proxy_call(name: str, tool: str, args: dict | None, card: str | None = None, stage: str | None = None):
    listed = {t["name"]: t for t in connectors.tools(name)}
    if not listed.get(tool, {"write": True}).get("write"):
        return connectors.call(name, tool, args, mode="stage")
    if not _read_write(name):
        alerts.open(f"connector_forbidden:{name}:{stage}", "warn", "a stage tried to write through a read-only connector",
                    f"set access = \"read-write\" on connector {name} if stage {stage} should write")
        raise Forbidden(f"forbidden: {tool} is not available on connector {name}")
    if tool not in listed:
        raise Forbidden(f"forbidden: {tool} is not available on connector {name}")
    if not _gate_approved(card, stage):
        raise Forbidden("gate_required: approve the card's gate first")
    if outward.state().get("on"):
        raise Forbidden("kill_switch: outward writes are paused; turn the kill switch off and call again")
    return connectors.call(name, tool, args, mode="stage")


def build_server(name: str, card: str | None = None, stage: str | None = None):
    from mcp import types
    from mcp.server.lowlevel import Server
    server = Server(f"pl-{name}")

    @server.list_tools()
    async def _list():
        return [types.Tool(name=t["name"], description=t.get("description") or "",
                           inputSchema=t.get("inputSchema") or {"type": "object"}) for t in visible_tools(name)]

    @server.call_tool()
    async def _call(tool, args):
        return [types.TextContent(type="text", text=str(await asyncio.to_thread(proxy_call, name, tool, args, card, stage)))]

    return server


def serve(name: str, card: str | None = None, stage: str | None = None) -> None:
    from mcp.server.stdio import stdio_server

    async def _run():
        server = build_server(name, card, stage)
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    asyncio.run(_run())
