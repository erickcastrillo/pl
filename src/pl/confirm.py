"""An agent's outward approval waits for the owner: one-time codes kept as a hash with an expiry."""
import hashlib
import json
import os
import secrets
import time

from pl import manager

CODE_SECONDS = 600


def agent_session() -> bool:
    return os.environ.get("PL_SOURCE") == "mcp" or os.environ.get("PL_ASSISTANT") == "1"


def pending(card: dict) -> dict | None:
    return (card.get("metadata") or {}).get("approval_pending") or None


def _path():
    return manager.machine_dir() / "confirm.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _digest(card_id: str, code: str) -> str:
    return hashlib.sha256(f"{card_id}:{code}".encode()).hexdigest()


def new_code(card: dict) -> str:
    """A fresh 6-digit code for this card; only its hash and expiry are kept."""
    code = f"{secrets.randbelow(10**6):06d}"
    data = {k: v for k, v in _load().items() if v.get("expires", 0) > time.time()}
    data[card["id"]] = {"sha256": _digest(card["id"], code), "expires": time.time() + CODE_SECONDS}
    _save(data)
    return code


def check(card: dict, code: str) -> bool:
    """True once for the right, unexpired code; the code is spent either way it matches."""
    data = _load()
    entry = data.get(card["id"])
    if not entry or entry.get("expires", 0) <= time.time():
        return False
    if not secrets.compare_digest(entry["sha256"], _digest(card["id"], str(code))):
        return False
    del data[card["id"]]
    _save(data)
    return True


def send_to_chat(text: str) -> None:
    """Queue a line for the chat connectors routed for gates; the owner's chat app only."""
    path = manager.machine_dir() / "chat-outbox.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"headline": text, "route": "gates"}) + "\n")
