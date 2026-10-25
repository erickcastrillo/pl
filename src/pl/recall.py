"""Agent memory: entries about runs, approvals and decisions, in a SQLite store with full-text search."""
import hashlib
import json
import os
import re
import math
import secrets
import sqlite3
import struct
import sys
from datetime import datetime, timezone

from pl import config as C
from pl import events, jsonout, local_jobs, local_model, util
from pl.board import sections

SEMANTIC_MIN = 0.55
KINDS = ("run", "approval", "rejection", "decision", "fact", "preference", "commitment")
UNTRUSTED_SOURCES = ("email:", "connector:", "trigger:")


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


def add(text, kind, source, card=None, pipeline=None, pod=None, client=None, contact=None, author=None,
        replaces=None, link=None):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if os.environ.get("PL_UNTRUSTED") == "1" or source.startswith(UNTRUSTED_SOURCES):   # PL_UNTRUSTED: an agent reading outside data
        raise Refused("memory takes no text from emails or connector results")
    text = _clean(text)
    pipeline = pipeline or C.PROFILE_NAME
    pod = pod or pipeline
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
    _store_vectors([(entry_id, text)])
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


def _embed(texts):
    """(model, vectors) from the local embed model, or None when none is set, it fails or it answers oddly."""
    model = local_model.job_settings()["models"].get("embed")
    if not model:
        return None
    try:
        vectors = local_jobs.embed(texts)
    except Exception:  # noqa: BLE001 - a missing vector never blocks a save or a search
        return None
    ok = isinstance(vectors, list) and len(vectors) == len(texts) and all(isinstance(v, list) and v for v in vectors)
    return (model, vectors) if ok else None


def _store_vectors(pairs):
    got = _embed([t for _, t in pairs]) if pairs else None
    if not got:
        return 0
    model, vectors = got
    con = _db()
    try:
        with con:
            con.executemany("INSERT OR REPLACE INTO vectors(id, model, dim, blob) VALUES (?,?,?,?)",
                            [(i, model, len(v), struct.pack(f"{len(v)}f", *v)) for (i, _), v in zip(pairs, vectors)])
    finally:
        con.close()
    return len(pairs)


def reindex() -> int:
    """Give every entry without a vector one; returns how many were filled in."""
    con = _db()
    try:
        pairs = [(r["id"], r["text"]) for r in con.execute(
            "SELECT id, text FROM entries WHERE id NOT IN (SELECT id FROM vectors)")]
    finally:
        con.close()
    return _store_vectors(pairs)


def _cosine(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _semantic(con, query, model, pod, card=None):
    """{id: cosine} of the live entries whose vector, made by the same model, is close to the query vector."""
    sql = ("SELECT v.id, v.dim, v.blob FROM vectors v JOIN entries e ON e.id = v.id "
           "WHERE v.model = ? AND e.replaced_by IS NULL" + (" AND e.pod = ?" if pod else "") + (" AND e.card = ?" if card else ""))
    scores = {}
    for r in con.execute(sql, (model, *([pod] if pod else []), *([str(card)] if card else []))):
        if r["dim"] == len(query):
            c = _cosine(query, struct.unpack(f"{r['dim']}f", r["blob"]))
            if c > SEMANTIC_MIN:
                scores[r["id"]] = c
    return scores


def _full_text(con, q, pod, card=None):
    match = _match(q)
    if not match:
        return []
    sql = ("SELECT e.*, bm25(entries_fts) AS rank FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
           "WHERE entries_fts MATCH ? AND e.replaced_by IS NULL")
    args = [match]
    if pod:
        sql += " AND e.pod = ?"
        args.append(pod)
    if card:
        sql += " AND e.card = ?"
        args.append(str(card))
    return con.execute(sql + " ORDER BY rank", args).fetchall()


def search_with_mode(q, limit=10, pod=None, card=None):
    """(entries, mode): mode is "full-text+semantic" when the embed model answered for the query, else "full-text"."""
    got = _embed([q]) if q.strip() else None
    con = _db()
    try:
        hits = _full_text(con, q, pod, card)
        best, worst = (hits[0]["rank"], hits[-1]["rank"]) if hits else (0, 0)
        scores = {r["id"]: ((worst - r["rank"]) / (worst - best) if worst != best else 1.0) for r in hits}
        rows = {r["id"]: dict(r) for r in hits}
        if got:
            for i, c in _semantic(con, got[1][0], got[0], pod, card).items():
                scores[i] = max(scores.get(i, 0.0), c)
                if i not in rows:
                    rows[i] = dict(con.execute("SELECT * FROM entries WHERE id = ?", (i,)).fetchone())
    finally:
        con.close()
    ranked = sorted(rows, key=lambda i: -scores[i])[:limit]
    return [{**{k: v for k, v in rows[i].items() if k != "rank"}, "score": round(scores[i], 3)} for i in ranked], \
        "full-text+semantic" if got else "full-text"


def search(q, limit=10, pod=None, card=None):
    return search_with_mode(q, limit, pod, card)[0]


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
    from pl import trackers
    if trackers.dry_run():
        print(f"would remember ({kind}) on {card}")
        return None
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
                con.execute("DELETE FROM vectors WHERE id = ?", (entry_id,))
                return con.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


_OF_CONTACT = "contact = ? OR replaced_by IN (SELECT id FROM entries WHERE contact = ?)"


def count_contact(contact) -> int:
    con = _db()
    try:
        return con.execute(f"SELECT count(*) FROM entries WHERE {_OF_CONTACT}", (contact, contact)).fetchone()[0]
    finally:
        con.close()


def forget_contact(contact) -> int:
    """Delete every entry linked to the contact, the older entries they replaced, and their vectors."""
    con = _db()
    try:
        with con:
            con.execute(f"DELETE FROM vectors WHERE id IN (SELECT id FROM entries WHERE {_OF_CONTACT})", (contact, contact))
            n = con.execute(f"DELETE FROM entries WHERE {_OF_CONTACT}", (contact, contact)).rowcount
            con.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
        con.execute("VACUUM")
    finally:
        con.close()
    return n


def _cmd_forget(a):
    if a.dry_run:
        n = count_contact(a.contact)
        print(json.dumps({"would_delete": n}) if a.json else f"would delete {n} entries")
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit(2)
        try:
            ok = input(f"Forget every memory entry linked to contact {a.contact}? [y/N]: ")
        except EOFError:
            ok = ""
        if ok.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 0
    n = forget_contact(a.contact)
    events.emit("memory_forgot", contact_hash=hashlib.sha256(a.contact.encode("utf-8")).hexdigest()[:12], count=n)
    print(json.dumps({"deleted": n}) if a.json else f"deleted {n} entries")
    return 0


def _link(e):
    return f"pl card {e['card']}" if e["card"] else f"pl memory show {e['id']}"


def _memory_search(a):
    found, mode = search_with_mode(a.query, a.limit, a.pod, a.card)
    entries = [{**e, "link": _link(e)} for e in found]
    if a.json:
        jsonout.emit({"mode": mode, "entries": entries})
        return 0
    if mode == "full-text":
        print("full-text search only (no local embeddings)")
    for e in entries:
        print(f"{e['id']}  {e['created_at'][:10]} {e['kind']}: {' '.join(e['text'].split())}  ({e['source']}, {e['link']})")
    return 0


def _memory_add(a):
    try:
        entry_id = add(a.text, a.kind, f"owner:{C.USER_EMAIL or 'cli'}", card=a.card, client=a.client,
                       contact=a.contact, author=C.USER_EMAIL or None, replaces=a.replaces)
    except (Refused, ValueError) as e:
        print(f"pl: {e}", file=sys.stderr)
        return 2
    if a.json:
        jsonout.emit({"id": entry_id})
    else:
        print(entry_id)
    return 0


def _memory_show(a):
    entry = get(a.id)
    if not entry:
        print(f"pl: no memory entry {a.id}", file=sys.stderr)
        return 1
    entry = {**entry, "link": _link(entry)}
    if a.json:
        jsonout.emit(entry)
        return 0
    for key in ("id", "kind", "source", "card", "client", "contact", "pipeline", "pod", "author", "created_at", "link"):
        if entry.get(key):
            print(f"{key}: {entry[key]}")
    if entry["replaced_by"]:
        print(f"replaced by {entry['replaced_by']}")
    print(f"\n{entry['text']}")
    return 0


def cmd_memory(a):
    return {"search": _memory_search, "add": _memory_add, "show": _memory_show, "forget": _cmd_forget}[a.action](a)
