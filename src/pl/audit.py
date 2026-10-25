"""Two append-only logs, both 0600. record(): machine-wide (<machine dir>/audit.jsonl), one line per chat/remote event.
append(): this profile's hash-chained log (<state>/audit.jsonl) of who did what, checked by verify() and pl audit."""
import fcntl
import hashlib
import json
import os
import socket
import stat
import sys
from datetime import datetime, timezone
from typing import Any

from pl import config as C
from pl import manager
from pl import util

# The anomaly alerts in dispatch match on these action names.
CHAT_UNKNOWN_SENDER = "chat_unknown_sender"
CONNECTOR_TOOL = "connector.tool"


def record(kind: str, *args: Any, **fields: Any) -> None:
    """Append one audit entry to machine_dir()/audit.jsonl, opened 0600 with O_APPEND."""
    data = dict(fields)
    if args:
        if len(args) == 1:
            data.setdefault("target", args[0])
        elif len(args) == 2:
            data.setdefault("connector", args[0])
            data.setdefault("sender_id", args[1])
        else:
            data["args"] = list(args)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
    path = manager.machine_dir() / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass


def _path():
    return C.STATE_DIR / "audit.jsonl"


def _lock_path():
    return C.STATE_DIR / "audit.jsonl.lock"


def who() -> str:
    if os.environ.get("PL_STAGE"):
        return f"agent:{os.environ['PL_STAGE']}"
    if os.environ.get("PL_ASSISTANT"):
        return "assistant"
    if os.environ.get("PL_MCP_SESSION") or os.environ.get("PL_MCP"):
        return "mcp-session"
    return getattr(C, "USER_EMAIL", "") or os.environ.get("USER", "unknown")


def _format_what(action: str, detail=None) -> str:
    if detail is None:
        return action
    if isinstance(detail, (list, tuple, set)):
        fields = sorted(str(x) for x in detail)
        return f"{action}: {', '.join(fields)}" if fields else action
    if isinstance(detail, dict):
        if "tool" in detail:
            return " ".join(p for p in (action, detail.get("tool", ""), detail.get("access", ""), detail.get("result", "")) if p)
        fields = sorted(detail.keys())
        return f"{action}: {', '.join(fields)}" if fields else action
    return action


def append(action: str, *, card=None, target=None, detail=None, host=None, connector=None, channel=None, when=None,
           **extra) -> None:
    from pl import trackers
    if trackers.dry_run():
        return
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            lock = os.open(_lock_path(), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            lock = None
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _append_locked(action, card=card, target=target, detail=detail, host=host, connector=connector,
                               channel=channel, when=when, extra=extra)
            finally:
                os.close(lock)
        else:
            _append_locked(action, card=card, target=target, detail=detail, host=host, connector=connector,
                           channel=channel, when=when, extra=extra)
    except OSError:
        pass


def _append_locked(action: str, *, card=None, target=None, detail=None, host=None, connector=None, channel=None,
                   when=None, extra=None) -> None:
    path = _path()
    prev = None
    if path.exists():
        try:
            lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                prev = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
        except OSError:
            prev = None

    rec = {
        "who": who(),
        "action": action,
        "what": _format_what(action, detail),
        "when": (datetime.fromtimestamp(when, tz=timezone.utc).isoformat(timespec="seconds")
                 if isinstance(when, (int, float)) else str(when)) if when is not None
                else datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": os.environ.get("PL_SESSION"),
        "host": host or getattr(C, "HOST", None) or socket.gethostname(),
    }
    for k, v in (("connector", connector), ("channel", channel)):
        if v is not None:
            rec[k] = v
    rec.update(extra or {})
    if card is not None:
        rec["card"] = card
    if target is not None:
        rec["target"] = target
    rec["prev"] = prev
    rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass


def read() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def verify(path=None) -> bool:
    p = path or _path()
    if not p.exists():
        return True
    try:
        lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return False
    prev_hash = None
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            return False
        stored_hash = entry.get("hash")
        if not stored_hash:
            return False
        expected_hash = hashlib.sha256(json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True).encode("utf-8")).hexdigest()
        if stored_hash != expected_hash or entry.get("prev") != prev_hash:
            return False
        prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return True


def cmd_audit(a) -> None:
    if getattr(a, "verify", False):
        if not verify():
            print("audit verification failed: broken hash chain", file=sys.stderr)
            raise SystemExit(1)
        if not getattr(a, "json", False) and not getattr(a, "since", False):
            print("audit log verified: chain intact")
            return
    from pl import util
    from pl.standup import parse_since
    since_dt = parse_since(a.since).astimezone(timezone.utc) if getattr(a, "since", None) else None
    entries = read()
    if since_dt:
        entries = [e for e in entries if util.parse_iso(e["when"]) >= since_dt.timestamp()]
    if getattr(a, "json", False):
        for e in entries:
            print(json.dumps(e))
    else:
        if not entries:
            print("no audit entries")
            return
        for e in entries:
            sess = f" [{e['session']}]" if e.get("session") else ""
            card = f" {e['card']}" if e.get("card") else ""
            target = f" {e['target']}" if e.get("target") else ""
            print(f"{e.get('when', '')}  {e.get('who', ''):<20}  {e.get('what', '')}{card}{target}{sess}")


def read_entries(since: float | None = None) -> list[dict]:
    return _read_jsonl(_path(), since)


def read_machine_entries(since: float | None = None) -> list[dict]:
    return _read_jsonl(manager.machine_dir() / "audit.jsonl", since)


def _read_jsonl(p, since: float | None) -> list[dict]:
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
