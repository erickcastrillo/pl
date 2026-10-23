import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from pl import util

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MONEY_RE = re.compile(r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)")
ADDRESS_RE = re.compile(r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b", re.IGNORECASE)
DIGITS_RE = re.compile(r"\b\d{5,}\b")


def safe_text(s: str) -> str:
    s = util.mask(str(s))
    for rx in (EMAIL_RE, MONEY_RE, ADDRESS_RE, DIGITS_RE):
        s = rx.sub("…", s)
    return s[:2000]


def _pl(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "PL_SOURCE": "chat", **(env or {})})


def audit_record(kind: str, **fields: Any) -> None:
    try:
        from pl import audit
        audit.record(kind, **fields)
    except (ImportError, AttributeError):
        from pl import manager
        p = manager.machine_dir() / "audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"kind": kind, "ts": time.time(), **fields}) + "\n")


def parse_action_target(target: str) -> tuple[str, str]:
    parts = target.split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("default", parts[1] if len(parts) == 2 else target)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http(token: str, path: str, payload: dict[str, Any] | None = None, method: str = "POST") -> Any:
    endpoint = f"https://discord.com/api/v10/{path.lstrip('/')}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    req = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=30) as r:
            body = r.read(1_000_000)
            if not body:
                return {}
            return json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace") if e.fp else ""
        raise RuntimeError(util.mask(f"Discord HTTP {e.code}: {e.reason} ({err}) for {path}").replace(token, "***")) from None
    except Exception as e:
        raise RuntimeError(util.mask(f"Discord error {path}: {e}").replace(token, "***")) from None


def _ws(url: str):
    import websockets.sync.client
    return websockets.sync.client.connect(url)


def format_components(buttons: list[Any] | None) -> list[dict[str, Any]]:
    if not buttons:
        return []
    elements = []
    items = [x for b in (buttons if isinstance(buttons, list) else [buttons]) for x in (b if isinstance(b, list) else [b])]
    for it in items:
        if isinstance(it, dict) and it.get("type") == 2:
            elements.append(it)
            continue
        label = it.get("label", it.get("text", "")) if isinstance(it, dict) else (it[0] if isinstance(it, (tuple, list)) else str(it))
        val = it.get("custom_id", it.get("value", "")) if isinstance(it, dict) else (it[1] if isinstance(it, (tuple, list)) else str(it))
        url = it.get("url") if isinstance(it, dict) else None
        if url or str(val).startswith(("http://", "https://")):
            elements.append({"type": 2, "style": 5, "label": str(label), "url": url or str(val)})
        else:
            lbl_lower = str(label).lower()
            style = 3 if lbl_lower in ("approve", "confirm") else (4 if lbl_lower in ("send back", "reject") else 1)
            elements.append({"type": 2, "style": style, "label": str(label), "custom_id": str(val)})
    return [{"type": 1, "components": elements}] if elements else []


class Discord:
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.token_ref = cfg.get("token", "")
        self.allow = [str(x) for x in cfg.get("allow", [])]
        self.routes = cfg.get("routes", ["gates"])
        self._resolved_token = None
        self.session_id = None
        self.seq = None
        self.resume_url = None

    def _token(self) -> str:
        if self._resolved_token is None:
            from pl.trackers import mcp
            self._resolved_token = mcp._resolve(self.token_ref, [])
        return self._resolved_token

    def send(self, channel_id: str, text: str, buttons: list[Any] | None = None) -> Any:
        payload: dict[str, Any] = {"content": safe_text(text)}
        if components := format_components(buttons):
            payload["components"] = components
        return _http(self._token(), f"channels/{channel_id}/messages", payload, method="POST")

    def edit(self, channel_id: str, message_id: str, text: str, components: list[dict[str, Any]] | None = None) -> Any:
        payload: dict[str, Any] = {"content": safe_text(text)}
        if components is not None:
            payload["components"] = components
        return _http(self._token(), f"channels/{channel_id}/messages/{message_id}", payload, method="PATCH")

    def respond_interaction(self, interaction_id: str, token: str, response: dict[str, Any]) -> Any:
        return _http(self._token(), f"interactions/{interaction_id}/{token}/callback", response, method="POST")

    def open_notes_modal(self, interaction_id: str, token: str, custom_id: str) -> Any:
        modal_payload = {
            "type": 9,
            "data": {
                "title": "Send Back Notes",
                "custom_id": f"send_back_modal:{custom_id}",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "notes_input",
                                "style": 2,
                                "label": "Notes",
                                "required": False,
                            }
                        ],
                    }
                ],
            },
        }
        return self.respond_interaction(interaction_id, token, modal_payload)

    def get_gateway_url(self) -> str:
        try:
            res = _http(self._token(), "gateway/bot", method="GET")
            return str(res.get("url", "wss://gateway.discord.gg/?v=10&encoding=json"))
        except Exception as e:
            raise RuntimeError(util.mask(str(e)).replace(self._token(), "***")) from None

    def handle_interaction(self, d: dict[str, Any]) -> Any:
        u = d.get("member", {}).get("user") or d.get("user")
        user_id = str(u.get("id", "")) if isinstance(u, dict) else str(d.get("user_id", ""))
        if not user_id or user_id not in self.allow:
            audit_record("chat_unknown_sender", connector=self.name, sender_id=user_id)
            return None
        itype = d.get("type")
        int_id, int_tok = d.get("id", ""), d.get("token", "")
        if itype == 3:
            custom_id = d.get("data", {}).get("custom_id", "")
            if custom_id.startswith("reject:"):
                return self.open_notes_modal(int_id, int_tok, custom_id)
            if custom_id.startswith("approve:") or custom_id.startswith("app:"):
                profile, card = parse_action_target(custom_id)
                res = _pl([sys.executable, "-m", "pl", "--profile", profile, "approve", card])
                now_utc = time.strftime("%H:%M UTC", time.gmtime())
                self.respond_interaction(int_id, int_tok, {
                    "type": 7,
                    "data": {
                        "content": f"Approved by you at {now_utc}",
                        "components": [],
                    },
                })
                return res
        if itype == 5:
            custom_id = d.get("data", {}).get("custom_id", "")
            target = custom_id.removeprefix("send_back_modal:")
            profile, card = parse_action_target(target)
            notes = ""
            for row in d.get("data", {}).get("components", []):
                for comp in row.get("components", []):
                    if comp.get("custom_id") == "notes_input":
                        notes = comp.get("value", "")
            res = _pl([sys.executable, "-m", "pl", "--profile", profile, "reject", card, notes])
            self.respond_interaction(int_id, int_tok, {"type": 6})
            return res
        return None

    def handle_gateway_payload(self, msg: dict[str, Any], ws=None) -> Any:
        op = msg.get("op")
        seq = msg.get("s")
        if seq is not None:
            self.seq = seq
        if op == 10:
            if self.session_id and self.seq is not None:
                pkt = {"op": 6, "d": {"token": self._token(), "session_id": self.session_id, "seq": self.seq}}
            else:
                pkt = {
                    "op": 2,
                    "d": {
                        "token": self._token(),
                        "intents": 513,
                        "properties": {"os": "linux", "browser": "pl", "device": "pl"},
                    },
                }
            if ws:
                ws.send(json.dumps(pkt))
            return {"type": "hello"}
        if op == 1:
            if ws:
                ws.send(json.dumps({"op": 1, "d": self.seq}))
            return {"type": "heartbeat"}
        if op == 0:
            t = msg.get("t")
            d = msg.get("d", {})
            if t == "READY":
                self.session_id = d.get("session_id")
                self.resume_url = d.get("resume_gateway_url")
                return {"type": "ready", "session_id": self.session_id}
            if t == "RESUMED":
                return {"type": "resumed"}
            if t == "INTERACTION_CREATE":
                return self.handle_interaction(d)
        if op in (7, 9):
            return {"type": "reconnect"}
        return None

    def run_gateway_loop(self, stop_event=None) -> None:
        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                gateway_url = self.resume_url or self.get_gateway_url()
                with _ws(f"{gateway_url}/?v=10&encoding=json" if not gateway_url.startswith("wss://") else gateway_url) as ws:
                    backoff = 1.0
                    for raw in ws:
                        if stop_event and stop_event.is_set():
                            break
                        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                        self.handle_gateway_payload(data, ws=ws)
            except Exception:
                time.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

    def test(self) -> int:
        for chat in self.allow:
            self.send(chat, "pl test message")
            print(f"sent test message to {chat}")
        return 0
