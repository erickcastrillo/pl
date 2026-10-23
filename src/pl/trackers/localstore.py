import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C

SCHEMA_VERSION = 1
ALLOWED_CARD_FIELDS = {"title", "description", "tags", "metadata", "assigned_to", "list_id", "column_id"}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


def _board(conn, board_id=None):
    if board_id:
        row = conn.execute("SELECT id FROM boards WHERE id = ?", (board_id,)).fetchone()
        if not row:
            raise ProtocolError("not_found", f"unknown board {board_id}")
        return row["id"]
    row = conn.execute("SELECT id FROM boards LIMIT 1").fetchone()
    if not row:
        raise ProtocolError("not_found", "no boards configured")
    return row["id"]


def _card_dict(row):
    return {
        "id": row["id"],
        "board_id": row["board_id"],
        "list_id": row["list_id"],
        "title": row["title"],
        "description": row["description"],
        "tags": json.loads(row["tags_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "assigned_to": row["assigned_to"],
        "updated_at": row["updated_at"],
        "seq": row["seq"],
    }


def open_db(path, profile=None):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(str(p), 0o600)

    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v == 0:
        conn.execute("CREATE TABLE boards (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        conn.execute("CREATE TABLE columns (id TEXT PRIMARY KEY, board_id TEXT NOT NULL, title TEXT NOT NULL, pos INTEGER NOT NULL)")
        conn.execute(
            "CREATE TABLE cards ("
            "id TEXT PRIMARY KEY, "
            "board_id TEXT NOT NULL, "
            "list_id TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "tags_json TEXT NOT NULL DEFAULT '[]', "
            "metadata_json TEXT NOT NULL DEFAULT '{}', "
            "assigned_to TEXT, "
            "updated_at TEXT NOT NULL, "
            "seq INTEGER"
            ")"
        )
        board_title = (
            profile
            or getattr(C, "PROFILE_NAME", None)
            or (p.parent.name.removeprefix(".pl-") if p.parent.name.startswith(".pl-") else p.parent.name)
            or "pl"
        )
        bid = uuid.uuid4().hex
        conn.execute("INSERT INTO boards (id, title) VALUES (?, ?)", (bid, board_title))
        for pos, col_name in enumerate(C.COLUMNS):
            conn.execute("INSERT INTO columns (id, board_id, title, pos) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, bid, col_name, pos))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


def protocol(conn, board_id=None):
    rows = conn.execute("SELECT id, title FROM boards ORDER BY rowid").fetchall()
    return {"version": SCHEMA_VERSION, "boards": [{"id": r["id"], "title": r["title"]} for r in rows]}


def columns(conn, board_id=None):
    bid = _board(conn, board_id)
    rows = conn.execute("SELECT id, title FROM columns WHERE board_id = ? ORDER BY pos ASC", (bid,)).fetchall()
    return {"columns": [{"id": r["id"], "title": r["title"]} for r in rows]}


def ensure_column(conn, title, description="", board_id=None):
    bid = _board(conn, board_id)
    row = conn.execute("SELECT id, title FROM columns WHERE board_id = ? AND title = ?", (bid, title)).fetchone()
    if row:
        return {"column": {"id": row["id"], "title": row["title"]}}
    cid = uuid.uuid4().hex
    pos = conn.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM columns WHERE board_id = ?", (bid,)).fetchone()[0]
    conn.execute("INSERT INTO columns (id, board_id, title, pos) VALUES (?, ?, ?, ?)", (cid, bid, title, pos))
    conn.commit()
    return {"column": {"id": cid, "title": title}}


def cards(conn, cursor=None, limit=100, assigned_to=None, updated_since=None, board_id=None):
    bid = _board(conn, board_id)
    clauses = ["board_id = ?"]
    params = [bid]
    if cursor is not None:
        try:
            cur_seq = int(cursor)
        except ValueError:
            raise ProtocolError("invalid", f"bad cursor {cursor}")
        clauses.append("seq > ?")
        params.append(cur_seq)
    if assigned_to is not None:
        clauses.append("assigned_to = ?")
        params.append(assigned_to)
    if updated_since is not None:
        clauses.append("updated_at >= ?")
        params.append(updated_since)

    where = " AND ".join(clauses)
    lim = int(limit)
    rows = conn.execute(f"SELECT * FROM cards WHERE {where} ORDER BY seq ASC LIMIT ?", (*params, lim + 1)).fetchall()
    if len(rows) > lim:
        page = rows[:lim]
        nxt = str(page[-1]["seq"])
    else:
        page = rows
        nxt = None
    return {"cards": [_card_dict(r) for r in page], "next_cursor": nxt}


def card(conn, id, board_id=None):
    bid = _board(conn, board_id)
    row = conn.execute("SELECT * FROM cards WHERE board_id = ? AND id = ?", (bid, id)).fetchone()
    if not row:
        raise ProtocolError("not_found", f"no card {id}")
    return {"card": _card_dict(row)}


def create(conn, column_id, title, description="", tags=None, metadata=None, assigned_to=None, board_id=None, **kwargs):
    bid = _board(conn, board_id)
    if kwargs:
        raise ProtocolError("invalid", f"unknown field {next(iter(kwargs))}")
    col = conn.execute("SELECT id FROM columns WHERE board_id = ? AND id = ?", (bid, column_id)).fetchone()
    if not col:
        raise ProtocolError("invalid", f"unknown column {column_id}")
    cid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM cards").fetchone()[0]
    tags_json = json.dumps(list(tags or []))
    meta_json = json.dumps(dict(metadata or {}), ensure_ascii=False)
    conn.execute(
        "INSERT INTO cards (id, board_id, list_id, title, description, tags_json, metadata_json, assigned_to, updated_at, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, bid, column_id, title, description, tags_json, meta_json, assigned_to, now, seq),
    )
    conn.commit()
    return card(conn, cid, board_id=bid)


def update(conn, id, fields=None, board_id=None, **kwargs):
    bid = _board(conn, board_id)
    cur = conn.execute("SELECT * FROM cards WHERE board_id = ? AND id = ?", (bid, id)).fetchone()
    if not cur:
        raise ProtocolError("not_found", f"no card {id}")
    f = dict(fields or {})
    f.update(kwargs)
    for k in f:
        if k not in ALLOWED_CARD_FIELDS:
            raise ProtocolError("invalid", f"unknown field {k}")
    col_id = f.get("column_id") or f.get("list_id")
    if col_id is not None:
        col = conn.execute("SELECT id FROM columns WHERE board_id = ? AND id = ?", (bid, col_id)).fetchone()
        if not col:
            raise ProtocolError("invalid", f"unknown column {col_id}")
    new_title = f.get("title", cur["title"])
    new_desc = f.get("description", cur["description"])
    new_tags = json.dumps(list(f["tags"])) if "tags" in f else cur["tags_json"]
    new_meta = json.dumps(dict(f["metadata"]), ensure_ascii=False) if "metadata" in f else cur["metadata_json"]
    new_assigned = f["assigned_to"] if "assigned_to" in f else cur["assigned_to"]
    new_list = col_id if col_id is not None else cur["list_id"]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE cards SET title = ?, description = ?, tags_json = ?, metadata_json = ?, assigned_to = ?, list_id = ?, updated_at = ? "
        "WHERE id = ? AND board_id = ?",
        (new_title, new_desc, new_tags, new_meta, new_assigned, new_list, now, id, bid),
    )
    conn.commit()
    return card(conn, id, board_id=bid)


def move(conn, id, column_id, board_id=None):
    bid = _board(conn, board_id)
    cur = conn.execute("SELECT * FROM cards WHERE board_id = ? AND id = ?", (bid, id)).fetchone()
    if not cur:
        raise ProtocolError("not_found", f"no card {id}")
    col = conn.execute("SELECT id FROM columns WHERE board_id = ? AND id = ?", (bid, column_id)).fetchone()
    if not col:
        raise ProtocolError("invalid", f"unknown column {column_id}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE cards SET list_id = ?, updated_at = ? WHERE id = ? AND board_id = ?",
        (column_id, now, id, bid),
    )
    conn.commit()
    return card(conn, id, board_id=bid)


def delete(conn, id, board_id=None):
    bid = _board(conn, board_id)
    cur = conn.execute("SELECT id FROM cards WHERE board_id = ? AND id = ?", (bid, id)).fetchone()
    if not cur:
        raise ProtocolError("not_found", f"no card {id}")
    conn.execute("DELETE FROM cards WHERE id = ? AND board_id = ?", (id, bid))
    conn.commit()
    return {"deleted": True}
