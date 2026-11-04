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
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".outbound.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


@contextlib.contextmanager
def _locked_queue():
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name("outbound.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        items = _load(p)
        yield items
        _save(p, items)
    finally:
        os.close(fd)


def _cfg_val(section: str, key: str, default):
    sec = getattr(C, section.upper(), {})
    if isinstance(sec, dict) and key in sec:
        return sec[key]
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg := p / "config.toml").is_file():
        with contextlib.suppress(Exception):
            import tomllib
            d = tomllib.loads(cfg.read_text()).get(section, {})
            if key in d:
                return d[key]
    return default


def get_undo_minutes() -> float:
    return float(_cfg_val("outbound", "undo_minutes", 5.0))


def get_send_cap(connector: str) -> int:
    conns = getattr(C, "CONNECTORS", {})
    if isinstance(conns, dict) and "send_cap" in conns.get(connector, {}):
        return int(conns[connector]["send_cap"])
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg := p / "config.toml").is_file():
        with contextlib.suppress(Exception):
            import tomllib
            c = tomllib.loads(cfg.read_text()).get("connectors", {}).get(connector, {})
            if "send_cap" in c:
                return int(c["send_cap"])
    return 20


def has_reply(c: dict) -> bool:
    return "REPLY" in sections(c.get("description") or "")


def _extract_emails(val) -> list[str]:
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        return [e for v in val for e in _extract_emails(v)]
    return re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", str(val))


def flagged_recipients(c: dict, section: str = "REPLY") -> list[str]:
    args = parse_reply(c, section)
    recipients = _extract_emails(args.get("to"))
    meta = c.get("metadata") or {}
    sender = meta.get("from") or meta.get("sender") or (meta.get("refs") or {}).get("sender") or ""
    sender_emails = [e.lower() for e in _extract_emails(sender)]
    sender_domains = {e.split("@")[1] for e in sender_emails if "@" in e}
    contact_emails = {e.lower() for e in _extract_emails(meta.get("contact_emails") or [])}
    flagged = []
    for rec in recipients:
        r_low = rec.lower()
        dom = r_low.split("@")[1] if "@" in r_low else ""
        if r_low not in sender_emails and (not dom or dom not in sender_domains) and r_low not in contact_emails:
            flagged.append(f"outside: {rec}")
    return flagged


def parse_reply(c: dict, section: str = "REPLY") -> dict:
    sec = sections(c.get("description") or "").get(section, "").strip()
    to, subject, thread, body = None, None, None, ""
    if sec.startswith("{"):
        with contextlib.suppress(Exception):
            d = json.loads(sec)
            to, subject, thread, body = d.get("to"), d.get("subject"), d.get("thread"), d.get("body", "")
    if to is None and subject is None and not body:
        lines, in_hdr = [], True
        for line in sec.splitlines():
            low = line.lower()
            if in_hdr and low.startswith("to:"):
                to = line[3:].strip()
            elif in_hdr and low.startswith("subject:"):
                subject = line[8:].strip()
            elif in_hdr and low.startswith("thread:"):
                thread = line[7:].strip()
            elif in_hdr and not line.strip():
                in_hdr = False
            else:
                in_hdr = False
                lines.append(line)
        body = "\n".join(lines).strip()
    meta = c.get("metadata") or {}
    refs = meta.get("refs") or {}
    thread = thread or refs.get("thread") or meta.get("thread_id") or meta.get("thread") or refs.get("email") or c.get("id")
    to = to or meta.get("from") or meta.get("sender") or meta.get("reply_to")
    subject = subject or (f"Re: {c.get('title')}" if c.get("title") else "")
    return {"thread": thread, "to": to, "subject": subject, "body": body or sec}


def enqueue(c: dict, section: str = "REPLY", now: float | None = None) -> dict:
    now = _clock() if now is None else float(now)
    conn = (c.get("metadata") or {}).get("connector") or "email"
    item = {
        "id": f"out-{short_id(c.get('id', 'card'))}-{int(now)}",
        "card": c.get("id"), "connector": conn, "tool": "send_draft",
        "args": parse_reply(c, section), "not_before": now + get_undo_minutes() * 60.0,
        "status": "queued", "created_at": now_iso(),
    }
    with _locked_queue() as items:
        items.append(item)
    return item


def cancel(target: str) -> bool:
    with _locked_queue() as items:
        for it in items:
            if target in (it.get("id"), it.get("card"), short_id(it.get("card", "")), short_id(it.get("id", ""))):
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
    sent, p = [], queue_path()
    from pl import outward
    if not p.exists() or outward.state().get("on"):
        return sent
    with _locked_queue() as items:
        for it in items:
            if it.get("status") in ("queued", "waiting_cap") and it.get("not_before", 0) <= now:
                conn = it.get("connector", "email")
                cap = get_send_cap(conn)
                sends = sum(1 for x in items if x.get("connector") == conn and x.get("status") == "sent" and (now - x.get("sent_at", 0)) < 86400)
                if sends >= cap:
                    it["status"] = "waiting_cap"
                    with contextlib.suppress(Exception):
                        from pl import alerts
                        alerts.open(f"send_cap:{conn}", "warning", f"Daily send cap ({cap}) reached for {conn}",
                                    fix="wait for daily reset or increase send_cap in config")
                    continue
                if dry:
                    sent.append(it)
                    continue
                it["status"] = "sending"
                _save(p, items)
                call_tool(it["connector"], it.get("tool", "send_draft"), it.get("args") or {})
                it["status"] = "sent"
                it["sent_at"] = now
                sent.append(it)
    return sent
