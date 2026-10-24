"""Append-only audit log for cards, connectors, and security events."""
import fcntl
import json
import os
from datetime import datetime, timezone

from pl import config as C
from pl import util


def _path():
    return C.STATE_DIR / "audit.jsonl"


def append(action: str, *, card: str | None = None, target: str | None = None,
           host: str | None = None, connector: str | None = None,
           channel: str | None = None, when: str | float | None = None,
           **detail) -> None:
    now_str = util.now_iso() if when is None else (
        datetime.fromtimestamp(when, tz=timezone.utc).isoformat(timespec="seconds")
        if isinstance(when, (int, float)) else str(when)
    )
    extra = {k: v for k, v in {"card": card, "target": target, "connector": connector, "channel": channel}.items() if v is not None}
    rec = {
        "action": action, "what": action, "when": now_str,
        "who": C.USER_EMAIL or os.environ.get("PL_STAGE") or os.environ.get("USER", "unknown"),
        "session": os.environ.get("PL_SESSION"), "host": host or C.HOST,
        **extra, **detail,
    }
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
    except OSError:
        pass


def read_entries(since: float | None = None) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        lines = [json.loads(L) for L in p.read_text().splitlines() if L.strip()]
        return [r for r in lines if since is None or parse_entry_ts(r) >= since]
    except (OSError, ValueError):
        return []


def parse_entry_ts(rec: dict) -> float:
    val = rec.get("when") or rec.get("ts") or rec.get("time")
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val) if isinstance(val, str) else 0.0
    except ValueError:
        return util.parse_iso(val) if isinstance(val, str) else 0.0
