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


def _cards_path(conv: str) -> Path:
    d = machine_dir() / "brain" / "conversations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{conv}.cards.jsonl"


def log_source(conv_or_source: Any, source_or_none: dict[str, Any] | None = None) -> None:
    """Append a source to the conversation's source log."""
    if isinstance(conv_or_source, dict):
        conv, source = "default", conv_or_source
    else:
        conv, source = str(conv_or_source), source_or_none or {}
    p = _sources_path(conv)
    with open(p, "a") as f:
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


def log_card(conv_or_card: Any, card_or_none: dict[str, Any] | None = None) -> None:
    """Append a filed card to the conversation's card log."""
    if isinstance(conv_or_card, dict):
        conv, card = "default", conv_or_card
    else:
        conv, card = str(conv_or_card), card_or_none or {}
    p = _cards_path(conv)
    with open(p, "a") as f:
        f.write(json.dumps(card) + "\n")


def get_logged_cards(conv: str) -> list[dict[str, Any]]:
    """Read every logged card for a conversation."""
    p = _cards_path(conv)
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
                text = getattr(item, "text", None)
                if text:
                    _collect(text)
            return
        if isinstance(val, str):
            val_str = val.strip()
            if (val_str.startswith("{") and val_str.endswith("}")) or (val_str.startswith("[") and val_str.endswith("]")):
                try:
                    _collect(json.loads(val_str))
                    return
                except Exception:
                    pass
            for m in URL_RE.finditer(val_str):
                found.append({"url": m.group(0)})
            return
        if isinstance(val, list):
            for item in val:
                _collect(item)
            return
        if isinstance(val, dict):
            entry: dict[str, Any] = {}
            for k in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount"):
                if k in val and val[k] is not None:
                    entry[k] = val[k]
            if "links" in val and isinstance(val["links"], list):
                for link in val["links"]:
                    if isinstance(link, str) and URL_RE.match(link):
                        found.append({"url": link})
            if "url" in entry or "link" in entry or "id" in entry:
                found.append(entry)
            for k, v in val.items():
                if k not in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount"):
                    if isinstance(v, (dict, list)):
                        _collect(v)

    _collect(result)

    seen = set()
    logged = []
    for item in found:
        key = (item.get("url"), item.get("link"), item.get("id"))
        if key not in seen:
            seen.add(key)
            log_source(conv, item)
            logged.append(item)
    return logged
