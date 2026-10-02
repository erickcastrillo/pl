"""The optional local model: an Ollama server on this machine (Gemma 4 by default) for small, bounded jobs such as
the standup summary. Off unless [local_model] enabled = true. Only a loopback url is used, so the text never leaves
the machine, and there is no API key. Any failure gives None and pl carries on as without it.

The model's answer is untrusted data: it is only displayed, after control characters are stripped and its length
is capped. It is never run, never put in a shell string and never read as Rich markup."""
import json
import re
import urllib.request
from urllib.parse import urlsplit

from pl import config as C

DEFAULTS = {"enabled": False, "url": "http://localhost:11434", "model": "gemma4", "timeout": 30}
LOOPBACK = ("localhost", "127.0.0.1", "::1")
MAX_CHARS = 2000
# C0 and C1 controls except newline and tab, DEL, and the bidi overrides that reorder text on screen
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


def settings() -> dict:
    """[local_model] over the defaults."""
    return {**DEFAULTS, **(C.LOCAL_MODEL or {})}


def url_problem(url) -> str | None:
    """Why url may not be used (None = fine): it must be http(s) on a loopback host."""
    try:
        u = urlsplit(url) if isinstance(url, str) else None
        host = u and u.hostname
    except ValueError:
        host = None
    if not host or u.scheme not in ("http", "https") or host not in LOOPBACK:
        return f"url {url!r} is refused: only a loopback host (localhost, 127.0.0.1, ::1) keeps the text on this machine"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None   # a redirect could point off the machine: treat it as an error


def _request(url, payload, timeout):
    """The one call out: GET url (payload None) or POST payload as JSON. Returns the body text. No proxy, no
    redirects, so the request stays on the loopback host it names."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return r.read(1_000_000).decode("utf-8", "replace")


def _timeout(s):
    t = s.get("timeout")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else DEFAULTS["timeout"]


def clean(text, max_chars=MAX_CHARS) -> str:
    """The answer as plain text to show: \\r\\n made \\n, control characters removed, at most max_chars."""
    text = CONTROL.sub("", str(text).replace("\r\n", "\n")).strip()
    return text if len(text) <= max_chars else text[:max_chars - 1].rstrip() + "…"


def chat(system, text, max_chars=MAX_CHARS):
    """(answer, "") from the local model, or (None, why) on any failure: off, a refused url, no answer, bad JSON,
    an empty answer."""
    s = settings()
    if s.get("enabled") is not True:
        return None, "off: set [local_model] enabled = true"
    if why := url_problem(s.get("url")):
        return None, why
    payload = {"model": str(s.get("model")), "stream": False,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    try:
        body = _request(s["url"].rstrip("/") + "/api/chat", payload, _timeout(s))
    except Exception as e:  # noqa: BLE001 - whatever the call raises, pl goes on without the model
        return None, f"the local model did not answer ({type(e).__name__})"
    try:
        answer = json.loads(body)["message"]["content"]
    except (ValueError, TypeError, KeyError):
        return None, "the local model's reply was not understood"
    answer = clean(answer, max_chars) if isinstance(answer, str) else ""
    return (answer, "") if answer else (None, "the local model gave an empty answer")


def ask(system, text, max_chars=MAX_CHARS) -> str | None:
    """The local model's answer to text under the system prompt, or None on any failure."""
    return chat(system, text, max_chars)[0]


def available():
    """(ok, line): Ollama answers at the url and the model is pulled. Checked whether or not it is enabled."""
    s = settings()
    url, model = s.get("url"), str(s.get("model"))
    if why := url_problem(url):
        return False, why
    try:
        tags = json.loads(_request(url.rstrip("/") + "/api/tags", None, _timeout(s)))
        names = {m.get("name") for m in tags.get("models") or [] if isinstance(m, dict)}
    except Exception as e:  # noqa: BLE001 - any failure means not available
        return False, f"Ollama does not answer at {url} ({type(e).__name__}): start it with ollama serve"
    if model in names or f"{model}:latest" in names:
        return True, f"ok: {model} is pulled at {url}"
    return False, f"{model} is not pulled at {url}: run ollama pull {model}"


def cmd_local_model(_a=None):
    s = settings()
    print(f"enabled: {'yes' if s.get('enabled') is True else 'no'}\nurl: {s.get('url')}\nmodel: {s.get('model')}")
    ok, line = available()
    print(line)
    raise SystemExit(0 if ok else 1)
