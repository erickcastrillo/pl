"""Outward-action queue for email sends with undo window and caps."""
import contextlib
import fcntl
import json
import os
import re
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


def get_send_cap(connector: str) -> int:
    if hasattr(C, "CONNECTORS") and isinstance(C.CONNECTORS, dict):
        cfg = C.CONNECTORS.get(connector, {})
        if "send_cap" in cfg:
            return int(cfg["send_cap"])
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg := p / "config.toml").is_file():
        try:
            import tomllib
            d = tomllib.loads(cfg.read_text())
            conns = d.get("connectors", {})
            if connector in conns and "send_cap" in conns[connector]:
                return int(conns[connector]["send_cap"])
        except Exception:
            pass
    return 20


def has_reply(c: dict) -> bool:
    return "REPLY" in sections(c.get("description") or "")


def _extract_emails(val) -> list[str]:
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        out = []
        for v in val:
            out.extend(_extract_emails(v))
        return out
    return re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", str(val))


def flagged_recipients(c: dict) -> list[str]:
    args = parse_reply(c)
    recipients = _extract_emails(args.get("to"))
    meta = c.get("metadata") or {}
    refs = meta.get("refs") or {}
    sender = meta.get("from") or meta.get("sender") or refs.get("sender") or ""
    sender_emails = [e.lower() for e in _extract_emails(sender)]
    sender_domains = {e.split("@")[1] for e in sender_emails if "@" in e}
    contact_emails = {e.lower() for e in _extract_emails(meta.get("contact_emails") or [])}

    flagged = []
    for rec in recipients:
        r_low = rec.lower()
        if r_low in sender_emails:
            continue
        domain = r_low.split("@")[1] if "@" in r_low else ""
        if domain and domain in sender_domains:
            continue
        if r_low in contact_emails:
            continue
        flagged.append(f"outside: {rec}")
    return flagged


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
            if it.get("status") in ("queued", "waiting_cap") and it.get("not_before", 0) <= now:
                conn = it.get("connector", "email")
                cap = get_send_cap(conn)
                sends_today = sum(
                    1 for x in items
                    if x.get("connector") == conn and x.get("status") == "sent" and (now - x.get("sent_at", 0)) < 86400
                )
                if sends_today >= cap:
                    it["status"] = "waiting_cap"
                    try:
                        from pl import alerts
                        alerts.open(f"send_cap:{conn}", "warning", f"Daily send cap ({cap}) reached for {conn}",
                                    fix="wait for daily reset or increase send_cap in config")
                    except Exception:
                        pass
                    try:
                        from pl.util import notify
                        notify(f"Send cap reached: {conn}", f"Reached daily send cap ({cap}) for {conn}.")
                    except Exception:
                        pass
                    try:
                        from pl import chat
                        if hasattr(chat, "route"):
                            chat.route(f"send_cap:{conn}", f"Reached daily send cap ({cap}) for {conn}.")
                    except Exception:
                        pass
                    continue
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
