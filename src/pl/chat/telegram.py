import json
import urllib.error
import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    from pl import util
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:
            body = r.read(1_000_000).decode("utf-8", "replace")
            res = json.loads(body)
            if not res.get("ok"):
                masked = util.mask(f"Telegram API error {method}: {res.get('description', 'error')}").replace(token, "***")
                raise RuntimeError(masked)
            return res.get("result")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            if e.fp:
                err_body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        masked = util.mask(f"Telegram HTTP {e.code}: {e.reason} ({err_body}) for {method}").replace(token, "***")
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
            secrets: list[str] = []
            self._resolved_token = mcp._resolve(self.token_ref, secrets)
        return self._resolved_token

    def send(self, chat: str, text: str, buttons: list[Any] | dict[str, Any] | None = None) -> Any:
        from pl.chat import safe_text
        payload: dict[str, Any] = {"chat_id": str(chat), "text": safe_text(text)}
        if isinstance(buttons, dict):
            payload["reply_markup"] = buttons
        elif isinstance(buttons, list):
            rows = [[self._format_button(x) for x in (b if isinstance(b, list) else [b])] for b in buttons]
            payload["reply_markup"] = {"inline_keyboard": rows}
        return _http(self._token(), "sendMessage", payload)

    @staticmethod
    def _format_button(b: Any) -> dict[str, str]:
        if isinstance(b, dict):
            return b
        if isinstance(b, (tuple, list)) and len(b) >= 2:
            return {"text": str(b[0]), "callback_data": str(b[1])}
        return {"text": str(b), "callback_data": str(b)}
