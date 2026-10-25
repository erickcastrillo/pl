import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from pl import util
from pl.audit import CHAT_UNKNOWN_SENDER
from pl.chat.inbound import InboundEvent, audit_record, dispatch_action
from pl.trackers import mcp
import websockets.sync.client as wsc

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MONEY_RE = re.compile(r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)")
ADDRESS_RE = re.compile(r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b", re.IGNORECASE)
DIGITS_RE = re.compile(r"\b\d{5,}\b")


def safe_text(s: str) -> str:
    s = util.mask(str(s))
    for rx in (EMAIL_RE, MONEY_RE, ADDRESS_RE, DIGITS_RE):
        s = rx.sub("…", s)
    return s[:300]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=30) as r:
            res = json.loads(r.read(1_000_000).decode("utf-8", "replace"))
            if not res.get("ok"):
                raise RuntimeError(util.mask(f"Slack API error {method}: {res.get('error', 'error')}").replace(token, "***"))
            return res
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace") if e.fp else ""
        raise RuntimeError(util.mask(f"Slack HTTP {e.code}: {e.reason} ({err}) for {method}").replace(token, "***")) from None
    except Exception as e:
        raise RuntimeError(util.mask(f"Slack error {method}: {e}").replace(token, "***")) from None


def _ws(url: str):
    return wsc.connect(url)


def _format_blocks(text: str, buttons: list[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": safe_text(text)}}]
    if not buttons:
        return blocks
    if isinstance(buttons, dict) and "blocks" in buttons:
        return buttons["blocks"]
    elements = []
    items = [x for b in (buttons if isinstance(buttons, list) else [buttons]) for x in (b if isinstance(b, list) else [b])]
    for it in items:
        if isinstance(it, dict) and it.get("type") == "button":
            elements.append(it)
            continue
        t = it.get("text", "") if isinstance(it, dict) else (it[0] if isinstance(it, (tuple, list)) else str(it))
        val = it.get("callback_data", it.get("value", it.get("action_id", ""))) if isinstance(it, dict) else (it[1] if isinstance(it, (tuple, list)) else str(it))
        elem = {"type": "button", "text": {"type": "plain_text", "text": str(t)}}
        if (url := it.get("url") if isinstance(it, dict) else None) or str(val).startswith(("http://", "https://")):
            elem["url"] = url or str(val)
        else:
            elem["action_id"] = elem["value"] = str(val)
        if st := ("primary" if str(t).lower() in ("approve", "confirm") else ("danger" if str(t).lower() in ("send back", "reject") else None)):
            elem["style"] = st
        elements.append(elem)
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks


class Slack:
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name, self.cfg = name, cfg
        self.token_ref = cfg.get("token", "")
        self.app_token_ref = cfg.get("app_token", self.token_ref)
        self.allow = [str(x) for x in cfg.get("allow", [])]
        self.routes = cfg.get("routes", ["gates"])
        self._resolved_token = self._resolved_app_token = None

    def _resolve(self, attr: str, ref: str) -> str:
        if getattr(self, attr) is None:
            setattr(self, attr, mcp._resolve(ref, []))
        return getattr(self, attr)

    def _token(self) -> str:
        return self._resolve("_resolved_token", self.token_ref)

    def _app_token(self) -> str:
        return self._resolve("_resolved_app_token", self.app_token_ref)

    def send(self, chat: str, text: str, buttons: list[Any] | dict[str, Any] | None = None) -> Any:
        return _http(self._token(), "chat.postMessage", {"channel": str(chat), "text": safe_text(text), "blocks": _format_blocks(text, buttons)})

    def edit(self, chat: str, msg: str | int, text: str, blocks: list[dict[str, Any]] | None = None) -> Any:
        b = blocks if blocks is not None else [{"type": "section", "text": {"type": "mrkdwn", "text": safe_text(text)}}]
        return _http(self._token(), "chat.update", {"channel": str(chat), "ts": str(msg), "text": safe_text(text), "blocks": b})

    def open_notes_modal(self, trigger_id: str, private_metadata: str = "") -> Any:
        view = {
            "type": "modal", "callback_id": "send_back_modal", "private_metadata": private_metadata,
            "title": {"type": "plain_text", "text": "Send Back Notes"}, "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": "notes_block", "element": {"type": "plain_text_input", "action_id": "notes_input", "multiline": True}, "label": {"type": "plain_text", "text": "Notes"}}]
        }
        return _http(self._token(), "views.open", {"trigger_id": trigger_id, "view": view})

    def connect_socket_mode(self) -> str:
        try:
            return str(_http(self._app_token(), "apps.connections.open", {}).get("url", ""))
        except Exception as e:
            raise RuntimeError(util.mask(str(e)).replace(self._app_token(), "***")) from None

    def handle_envelope(self, envelope: dict[str, Any], ws=None) -> Any:
        if (env_id := envelope.get("envelope_id")) and ws:
            ws.send(json.dumps({"envelope_id": env_id}))
        payload = envelope.get("payload", {})
        u = payload.get("user")
        user_id = str(u.get("id", "")) if isinstance(u, dict) else str(payload.get("user_id", ""))
        if not user_id or user_id not in self.allow:
            audit_record(CHAT_UNKNOWN_SENDER, connector=self.name, sender_id=user_id)
            return None
        itype = payload.get("type")
        if itype == "block_actions":
            trig_id, chan, msg_ts = payload.get("trigger_id", ""), payload.get("channel", {}).get("id", ""), payload.get("message", {}).get("ts", "")
            for act in payload.get("actions", []):
                act_id = act.get("action_id", "")
                val = act.get("value", act_id)
                if act_id.startswith("reject:") or val.startswith("reject:"):
                    if trig_id:
                        self.open_notes_modal(trig_id, private_metadata=val)
                    return {"type": "modal_opened", "action": val}
                return dispatch_action(self, InboundEvent("action", user_id, act_id, val, chat_id=chan, message_id=msg_ts, trigger_id=trig_id, adapter="slack", raw=payload))
        if itype == "view_submission":
            view = payload.get("view", {})
            vals = [a["value"] for b in view.get("state", {}).get("values", {}).values() for a in b.values() if a.get("value")]
            return dispatch_action(self, InboundEvent("modal_submission", user_id, view.get("callback_id", ""), view.get("private_metadata", ""), notes=(vals[0] if vals else ""), adapter="slack", raw=payload))
        return None

    def run_socket_loop(self, stop_event=None) -> None:
        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                if not (ws_url := self.connect_socket_mode()):
                    raise RuntimeError("No WebSocket URL")
                with _ws(ws_url) as ws:
                    backoff = 1.0
                    for msg in ws:
                        if stop_event and stop_event.is_set():
                            break
                        self.handle_envelope(json.loads(msg.decode("utf-8") if isinstance(msg, bytes) else msg), ws=ws)
            except Exception:
                time.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

    def test(self) -> int:
        for chat in self.allow:
            self.send(chat, "pl test message")
            print(f"sent test message to {chat}")
        return 0
