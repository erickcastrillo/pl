"""Agent memory: entries about runs, approvals and decisions, in a SQLite store with full-text search."""
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from pl import config as C

KINDS = ("run", "approval", "rejection", "decision", "fact", "preference")
_SCHEMA = """
CREATE TABLE entries(id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
  card TEXT, pipeline TEXT, pod TEXT, client TEXT, contact TEXT, author TEXT, created_at TEXT NOT NULL,
  replaced_by TEXT);
CREATE VIRTUAL TABLE entries_fts USING fts5(text, content='entries');
CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER entries_au AFTER UPDATE OF text ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO entries_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TABLE vectors(id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, blob BLOB NOT NULL);
PRAGMA user_version = 1;
"""


def _db():
    path = C.STATE_DIR / "memory.db"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] == 0:
            con.executescript(_SCHEMA)
    except sqlite3.OperationalError as e:
        con.close()
        if "fts5" in str(e).lower():
            raise RuntimeError("pl: this Python's SQLite has no FTS5") from e
        raise
    return con


def add(text, kind, source, card=None, pipeline=None, pod=None, client=None, contact=None, author=None,
        replaces=None):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    pipeline = pipeline or C.PROFILE_NAME
    pod = pod or pipeline
    entry_id = "m-" + secrets.token_hex(4)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = _db()
    try:
        with con:
            con.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        (entry_id, kind, text, source, card, pipeline, pod, client, contact, author, now))
            if replaces:
                con.execute("UPDATE entries SET replaced_by=? WHERE id=?", (entry_id, replaces))
    finally:
        con.close()
    return entry_id


def get(entry_id):
    con = _db()
    try:
        row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def _match(q):
    return " ".join('"' + w.replace('"', '""') + '"' for w in q.split())


def search(q, limit=10, pod=None):
    match = _match(q)
    if not match:
        return []
    sql = ("SELECT e.* FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
           "WHERE entries_fts MATCH ? AND e.replaced_by IS NULL")
    args = [match]
    if pod:
        sql += " AND e.pod = ?"
        args.append(pod)
    con = _db()
    try:
        rows = con.execute(sql + " ORDER BY bm25(entries_fts) LIMIT ?", (*args, limit)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _clients(card):
    meta = card.get("metadata") or {}
    contacts = (meta.get("refs") or {}).get("contacts") or []
    return [c for c in [meta.get("client"), *contacts] if isinstance(c, str) and c]


def for_prompt(card, pod, cap=8, max_tokens=1500):
    """The lines of entries a stage on this card may see: its own, its client's and its pod's, newest first."""
    clients = _clients(card)
    where = "e.card = ? OR (e.pod = ? AND e.card IS NULL)"
    args = [str(card["id"]), pod]
    if clients:
        where += f" OR e.client IN ({','.join('?' * len(clients))})"
        args += clients
    con = _db()
    try:
        rows = con.execute(f"SELECT * FROM entries e WHERE ({where}) AND e.pod IN (?, 'company') "
                           "AND e.replaced_by IS NULL ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?",
                           (*args, pod, cap)).fetchall()
    finally:
        con.close()
    lines, used = [], 0
    for r in rows:
        line = f"- {r['created_at'][:10]} {r['kind']}: {' '.join(r['text'].split())} ({r['source']})"
        used += len(line) // 4 + 1
        if used > max_tokens:
            break
        lines.append(line)
    return "\n".join(lines)
