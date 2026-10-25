"""Agent memory: entries about runs, approvals and decisions, in a SQLite store with full-text search."""
import json
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

from pl import config as C
from pl import util
from pl.board import sections

KINDS = ("run", "approval", "rejection", "decision", "fact", "preference", "commitment")


class Refused(Exception):
    pass


def _luhn(s: str) -> bool:
    d = [int(c) for c in s if c.isdigit()]
    return 13 <= len(d) <= 19 and sum((x * 2 - 9 if x * 2 > 9 else x * 2) if i % 2 else x for i, x in enumerate(reversed(d))) % 10 == 0


def _clean(text: str) -> str:
    if util.mask(text) != text:
        raise Refused("memory takes no text holding secrets")
    return re.sub(r"\b(?:\d[ -]*?){13,19}\b", lambda m: ("****" + re.sub(r"\D", "", m.group(0))[-4:]) if _luhn(m.group(0)) else m.group(0), text)


_SCHEMA = """
CREATE TABLE entries(id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
  card TEXT, pipeline TEXT, pod TEXT, client TEXT, contact TEXT, author TEXT, created_at TEXT NOT NULL,
  replaced_by TEXT, link TEXT);
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
    cols = {r[1] for r in con.execute("PRAGMA table_info(entries)")}
    if "link" not in cols:   # a store made before entries could carry a link
        con.execute("ALTER TABLE entries ADD COLUMN link TEXT")
        con.commit()
    return con


class McpMemory:
    """The memory tools of an MCP server (a connector with role "memory"), returning what the local store returns."""

    def __init__(self, server, timeout=30):
        self.server, self.timeout = server, timeout

    def _call(self, tool, args):
        from pl.trackers import mcp as M
        key, s = M._session(self.server, self.timeout)
        res = s.run(lambda session: session.call_tool(tool, args))
        text = next((c.text for c in res.content if getattr(c, "type", None) == "text"), "")
        if res.isError:
            raise RuntimeError(f"pl: memory tool {tool} failed: {util.mask(text)[:200]}")
        return json.loads(text) if text.strip() else None

    def search(self, q, limit, pod, card):
        args = {"query": q, "limit": limit, **{k: v for k, v in (("pod", pod), ("card", card)) if v}}
        found = self._call("memory_search", args)
        return found.get("results", []) if isinstance(found, dict) else found or []

    def add(self, entry):
        return self._call("memory_add", entry)["id"]

    def get(self, entry_id):
        return self._call("memory_get", {"id": entry_id})

    def forget_contact(self, contact):
        return int(self._call("memory_forget", {"contact": contact}).get("deleted", 0))


def backend():
    """The MCP memory server when a connector has role "memory"; None means the local store."""
    from pl import connectors
    for _, c in connectors.load().items():
        if c["enabled"] and "memory" in c.get("role", []):
            cmd = [c["command"]] if isinstance(c.get("command"), str) else list(c.get("command") or [])
            server = {"url": c["url"], "headers": c.get("headers") or {}} if c.get("url") else \
                {"command": cmd[0], "args": [*cmd[1:], *(c.get("args") or [])], "env": c.get("env") or {}}
            return McpMemory(server)
    return None


def add(text, kind, source, card=None, pipeline=None, pod=None, client=None, contact=None, author=None,
        replaces=None, link=None):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if os.environ.get("PL_UNTRUSTED") == "1":   # an agent reading untrusted content may not write memory
        raise Refused("untrusted source")
    text = _clean(text)
    pipeline = pipeline or C.PROFILE_NAME
    pod = pod or pipeline
    if (remote := backend()):
        return remote.add({"text": text, "kind": kind, "source": source, "card": card, "pipeline": pipeline, "pod": pod,
                           "client": client, "contact": contact, "author": author, "replaces": replaces, "link": link})
    entry_id = "m-" + secrets.token_hex(4)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = _db()
    try:
        with con:
            con.execute("INSERT INTO entries(id, kind, text, source, card, pipeline, pod, client, contact, author, created_at, link) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, kind, text, source, card, pipeline, pod, client, contact, author, now, link))
            if replaces:
                con.execute("UPDATE entries SET replaced_by=? WHERE id=?", (entry_id, replaces))
    finally:
        con.close()
    return entry_id


def get(entry_id):
    if (remote := backend()):
        return remote.get(entry_id)
    con = _db()
    try:
        row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def _match(q):
    return " ".join('"' + w.replace('"', '""') + '"' for w in q.split())


def search(q, limit=10, pod=None, card=None):
    match = _match(q)
    if not match:
        return []
    if (remote := backend()):
        return remote.search(q, limit, pod, card)
    sql = ("SELECT e.* FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
           "WHERE entries_fts MATCH ? AND e.replaced_by IS NULL")
    args = [match]
    if pod:
        sql += " AND e.pod = ?"
        args.append(pod)
    if card:
        sql += " AND e.card = ?"
        args.append(card)
    con = _db()
    try:
        rows = con.execute(sql + " ORDER BY bm25(entries_fts) LIMIT ?", (*args, limit)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _already(source, card, author):
    con = _db()
    try:
        return con.execute("SELECT 1 FROM entries WHERE source=? AND card=? AND author=?",
                           (source, card, author)).fetchone() is not None
    finally:
        con.close()


def _first_paragraph(text):
    return next((p.strip() for p in (text or "").split("\n\n") if p.strip() and not p.startswith("#")), "")


def _section_body(text, heading):
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lstrip("#").strip().lower() == heading.lower() and line.startswith("#"):
            return _first_paragraph("\n".join(lines[i + 1:]).lstrip("\n"))
    return ""


def run_text(c):
    parts = sections(c.get("description"))
    pieces = [_section_body(parts.get("PLAN"), "Summary"),
              _section_body(parts.get("SPEC"), "Outcome") or _first_paragraph(parts.get("SPEC")),
              *((c.get("metadata") or {}).get("pr_urls") or [])[-1:]]
    return " ".join(p for p in pieces if p)[:400] or c.get("title", "")


def record_run(c, stage, worker):
    """One `run` entry for a stage that finished, once per (card, stage, session)."""
    source = f"run:{worker.get('session_id')}"
    author = f"{stage} agent ({worker.get('harness') or 'claude'}/{worker.get('profile')})"
    if _already(source, c["id"], author):
        return None
    return add(run_text(c), "run", source, card=c["id"], author=author)


def note(text, kind, source, card):
    """add() for callers that must carry on when the store fails: the failure is printed, never raised."""
    try:
        return add(text, kind, source, card=card, author=C.USER_EMAIL or None)
    except Exception as e:
        print(f"pl: memory not saved: {e}", file=sys.stderr)
        return None


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


def forget(entry_id) -> bool:
    """Delete one entry (the Brain tab's undo of what a conversation saved)."""
    try:
        con = _db()
        try:
            with con:
                return con.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def forget_contact(contact) -> int:
    """Delete every entry linked to this contact; the count."""
    if (remote := backend()):
        return remote.forget_contact(contact)
    con = _db()
    try:
        with con:
            return con.execute("DELETE FROM entries WHERE contact = ?", (contact,)).rowcount
    finally:
        con.close()
