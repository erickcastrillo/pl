"""Outward-action queue for email sends with undo window and caps."""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from pl import config as C
from pl.board import sections
from pl.util import now_iso, short_id


def _clock() -> float:
    return time.time()


def queue_path() -> Path:
    base = C.STATE_DIR or (C.CONFIG_DIR and C.CONFIG_DIR / "state") or Path("state")
    return base / "outbound.json"


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def _save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".outbound.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked_queue():
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_file = p.with_name("outbound.lock")
    fd = os.open(lock_file, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        items = _load(p)
        yield items
        _save(p, items)
    finally:
        os.close(fd)


def get_undo_minutes() -> float:
    if hasattr(C, "OUTBOUND") and isinstance(C.OUTBOUND, dict) and "undo_minutes" in C.OUTBOUND:
        return float(C.OUTBOUND["undo_minutes"])
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg := p / "config.toml").is_file():
        try:
            import tomllib
            d = tomllib.loads(cfg.read_text())
            if "outbound" in d and "undo_minutes" in d["outbound"]:
                return float(d["outbound"]["undo_minutes"])
        except Exception:
            pass
    return 5.0


def has_reply(c: dict) -> bool:
    return "REPLY" in sections(c.get("description") or "")


def parse_reply(c: dict) -> dict:
    sec = sections(c.get("description") or "").get("REPLY", "").strip()
    to, subject, thread, body = None, None, None, ""
    if sec.startswith("{"):
        try:
            d = json.loads(sec)
            to = d.get("to")
            subject = d.get("subject")
            thread = d.get("thread")
            body = d.get("body", "")
        except Exception:
            pass
    if to is None and subject is None and not body:
        body_lines, in_hdr = [], True
        for line in sec.splitlines():
            if in_hdr:
                if not line.strip():
                    in_hdr = False
                    continue
                low = line.lower()
                if low.startswith("to:"):
                    to = line[3:].strip()
                elif low.startswith("subject:"):
                    subject = line[8:].strip()
                elif low.startswith("thread:"):
                    thread = line[7:].strip()
                else:
                    in_hdr = False
                    body_lines.append(line)
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()

    meta = c.get("metadata") or {}
    refs = meta.get("refs") or {}
    thread = thread or refs.get("thread") or meta.get("thread_id") or meta.get("thread") or refs.get("email") or c.get("id")
    to = to or meta.get("from") or meta.get("sender") or meta.get("reply_to")
    subject = subject or (f"Re: {c.get('title')}" if c.get("title") else "")
    return {"thread": thread, "to": to, "subject": subject, "body": body or sec}


def enqueue(c: dict, now: float | None = None) -> dict:
    now = _clock() if now is None else float(now)
    undo = get_undo_minutes() * 60.0
    meta = c.get("metadata") or {}
    connector = meta.get("connector") or "email"
    args = parse_reply(c)
    item = {
        "id": f"out-{short_id(c.get('id', 'card'))}-{int(now)}",
        "card": c.get("id"),
        "connector": connector,
        "tool": "send_draft",
        "args": args,
        "not_before": now + undo,
        "status": "queued",
        "created_at": now_iso(),
    }
    with _locked_queue() as items:
        items.append(item)
    return item


def cancel(target: str) -> bool:
    with _locked_queue() as items:
        for it in items:
            if (it.get("id") == target or it.get("card") == target or
                    short_id(it.get("card", "")) == target or short_id(it.get("id", "")) == target):
                if it.get("status") in ("queued", "waiting_cap"):
                    it["status"] = "cancelled"
                    return True
    return False


def call_tool(connector: str, tool: str, args: dict) -> dict:
    try:
        from pl import connectors
        if hasattr(connectors, "call"):
            return connectors.call(connector, tool, args)
    except ImportError:
        pass
    raise NotImplementedError(f"no connector runner for {connector}:{tool}")


def send_due(dry: bool = False, now: float | None = None) -> list[dict]:
    now = _clock() if now is None else float(now)
    sent = []
    p = queue_path()
    if not p.exists():
        return sent
    with _locked_queue() as items:
        for it in items:
            if it.get("status") == "queued" and it.get("not_before", 0) <= now:
                if dry:
                    sent.append(it)
                    continue
                it["status"] = "sending"
                _save(p, items)
                try:
                    call_tool(it["connector"], it.get("tool", "send_draft"), it.get("args") or {})
                    it["status"] = "sent"
                    it["sent_at"] = now
                    sent.append(it)
                except Exception:
                    raise
    return sent
