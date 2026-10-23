import json
from typing import Any
import urllib.error
import urllib.request


def _http(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    from pl import util
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read(1_000_000).decode("utf-8", "replace"))
            if not res.get("ok"):
                masked = util.mask(f"Telegram API error {method}: {res.get('description', 'error')}").replace(token, "***")
                raise RuntimeError(masked)
            return res.get("result")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace") if e.fp else ""
        masked = util.mask(f"Telegram HTTP {e.code}: {e.reason} ({err}) for {method}").replace(token, "***")
        raise RuntimeError(masked) from None
    except Exception as e:
        masked = util.mask(f"Telegram error {method}: {e}").replace(token, "***")
        raise RuntimeError(masked) from None


class Telegram:
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name, self.cfg = name, cfg
        self.token_ref = cfg.get("token", "")
        self.allow = [str(x) for x in cfg.get("allow", [])]
        self.routes = cfg.get("routes", ["gates"])
        self._resolved_token: str | None = None

    def _token(self) -> str:
        if self._resolved_token is None:
            from pl.trackers import mcp
            self._resolved_token = mcp._resolve(self.token_ref, [])
        return self._resolved_token

    def send(self, chat: str, text: str, buttons: list[Any] | dict[str, Any] | None = None, force_reply: bool = False, reply_to_message_id: int | None = None) -> Any:
        from pl.chat import safe_text
        payload: dict[str, Any] = {"chat_id": str(chat), "text": safe_text(text)}
        if force_reply:
            payload["reply_markup"] = {"force_reply": True}
        elif isinstance(buttons, dict):
            payload["reply_markup"] = buttons
        elif isinstance(buttons, list):
            payload["reply_markup"] = {"inline_keyboard": [[[{"text": str(b[0]), "callback_data": str(b[1])} if isinstance(b, (list, tuple)) else (b if isinstance(b, dict) else {"text": str(b), "callback_data": str(b)}) for b in (row if isinstance(row, list) else [row])]] for row in buttons]}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        return _http(self._token(), "sendMessage", payload)

    def edit(self, chat: str, msg: str | int, text: str, buttons: list[Any] | dict[str, Any] | None = None) -> Any:
        from pl.chat import safe_text
        payload: dict[str, Any] = {"chat_id": str(chat), "message_id": int(msg), "text": safe_text(text)}
        if isinstance(buttons, dict):
            payload["reply_markup"] = buttons
        elif isinstance(buttons, list):
            payload["reply_markup"] = {"inline_keyboard": [[[{"text": str(b[0]), "callback_data": str(b[1])} if isinstance(b, (list, tuple)) else (b if isinstance(b, dict) else {"text": str(b), "callback_data": str(b)}) for b in (row if isinstance(row, list) else [row])]] for row in buttons]}
        return _http(self._token(), "editMessageText", payload)

    def updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        res = _http(self._token(), "getUpdates", {"offset": offset} if offset is not None else {})
        return res if isinstance(res, list) else []

    def answer_callback(self, callback_id: str, text: str | None = None) -> Any:
        from pl.chat import safe_text
        payload: dict[str, Any] = {"callback_query_id": str(callback_id)}
        if text:
            payload["text"] = safe_text(text)
        return _http(self._token(), "answerCallbackQuery", payload)
