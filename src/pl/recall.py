"""Recall: agent-memory store (spec 9). Holds durable entries across conversations."""
import os
import re
import secrets
import sqlite3
from pl import config as C, util


class Refused(Exception):
    pass


TRUSTED = ("run:", "card:", "approval:", "rejection:", "owner:", "conversation:", "brain:")


def _luhn(s: str) -> bool:
    d = [int(c) for c in s if c.isdigit()]
    return 13 <= len(d) <= 19 and sum((x * 2 - 9 if x * 2 > 9 else x * 2) if i % 2 else x for i, x in enumerate(reversed(d))) % 10 == 0


def _clean(text: str) -> str:
    if util.mask(text) != text:
        raise Refused("memory takes no text holding secrets")
    return re.sub(r"\b(?:\d[ -]*?){13,19}\b", lambda m: ("****" + re.sub(r"\D", "", m.group(0))[-4:]) if _luhn(m.group(0)) else m.group(0), text)


def _db():
    p = C.STATE_DIR / "memory.db"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not p.exists():
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, kind TEXT, text TEXT, source TEXT, link TEXT, card TEXT, pipeline TEXT, pod TEXT, client TEXT, contact TEXT, author TEXT, created_at TEXT, replaced_by TEXT)")
    conn.commit()
    return conn


def add(text: str, kind: str, source: str, link: str | None = None, **kwargs) -> str:
    if os.environ.get("PL_UNTRUSTED") == "1" or not any(source.startswith(pfx) for pfx in TRUSTED):
        raise Refused("untrusted source")
    clean = _clean(text)
    eid, now = f"m-{secrets.token_hex(4)}", util.now_iso()
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (eid, kind, clean, source, link or "", kwargs.get("card"), getattr(C, "PROFILE_NAME", "default"),
                 getattr(C, "PROFILE_NAME", "default"), kwargs.get("client"), kwargs.get("contact"), kwargs.get("author"), now)
            )
    except Exception:
        pass
    return eid


def forget(eid: str) -> bool:
    try:
        with _db() as conn:
            return conn.execute("DELETE FROM entries WHERE id = ?", (eid,)).rowcount > 0
    except Exception:
        return False
