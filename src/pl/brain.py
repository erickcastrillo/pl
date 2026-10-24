"""Central read-only brain: conversation sources and tools."""
import json
import re
from pathlib import Path
from typing import Any

from pl.manager import machine_dir

URL_RE = re.compile(r"https?://[^\s\"'>]+")


def _sources_path(conv: str) -> Path:
    d = machine_dir() / "brain" / "conversations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{conv}.sources.jsonl"


def log_source(conv: str, source: dict[str, Any]) -> None:
    """Append a source to the conversation's source log."""
    with open(_sources_path(conv), "a") as f:
        f.write(json.dumps(source) + "\n")


def get_logged_sources(conv: str) -> list[dict[str, Any]]:
    """Read every logged source for a conversation."""
    p = _sources_path(conv)
    if not p.is_file():
        return []
    try:
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:
        return []


def record_sources(conv: str | None, result: Any) -> list[dict[str, Any]]:
    """Extract links and record ids from a tool result and append them to the conversation source log."""
    conv = conv or "default"
    found: list[dict[str, Any]] = []

    def _collect(val: Any) -> None:
        if val is None:
            return
        if hasattr(val, "content"):
            for item in getattr(val, "content", []):
                if text := getattr(item, "text", None):
                    _collect(text)
            return
        if isinstance(val, str):
            s = val.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    _collect(json.loads(s))
                    return
                except Exception:
                    pass
            for m in URL_RE.finditer(s):
                found.append({"url": m.group(0)})
            return
        if isinstance(val, list):
            for item in val:
                _collect(item)
            return
        if isinstance(val, dict):
            entry = {k: val[k] for k in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount") if k in val and val[k] is not None}
            for link in val.get("links") or []:
                if isinstance(link, str) and URL_RE.match(link):
                    found.append({"url": link})
            if "url" in entry or "link" in entry or "id" in entry:
                found.append(entry)
            for k, v in val.items():
                if k not in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount") and isinstance(v, (dict, list)):
                    _collect(v)

    _collect(result)
    seen, logged = set(), []
    for item in found:
        key = (item.get("url"), item.get("link"), item.get("id"))
        if key not in seen:
            seen.add(key)
            log_source(conv, item)
            logged.append(item)
    return logged
