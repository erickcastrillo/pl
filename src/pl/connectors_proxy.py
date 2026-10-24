"""HTTP proxy for team connectors and pl tools for sandboxed agents."""
from dataclasses import dataclass, field
import http.server
import json
from pathlib import Path
import secrets
import stat
import threading
import urllib.error
import urllib.request


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
