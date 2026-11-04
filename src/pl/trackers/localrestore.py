"""pl board restore: replace the local board's columns and cards with a backup's board.json (written by pl.backup).

Writes straight to the store in one transaction, not through the tracker, so the outbox gets no entries."""
import fcntl
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import events
from pl.trackers import localstore


def _bad(src, reason):
    return SystemExit(f"pl board restore: {src}/board.json: {reason}")


def load(src: Path) -> dict:
    try:
        data = json.loads((src / "board.json").read_text())
    except OSError as e:
        raise _bad(src, e.strerror or "cannot be read") from e
    except ValueError as e:
        raise _bad(src, f"not valid JSON ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
        raise _bad(src, "no columns list")
    if not isinstance(data.get("cards"), list):
        raise _bad(src, "no cards list")
    columns = []
    for col in data["columns"]:
        col = {"title": col} if isinstance(col, str) else col
        if not isinstance(col, dict) or not isinstance(col.get("title"), str):
            raise _bad(src, "a column has no title")
        columns.append({"id": col.get("id") or uuid.uuid4().hex, "title": col["title"]})
    by_title = {c["title"]: c["id"] for c in columns}
    ids = set(by_title.values())
    cards = []
    for card in data["cards"]:
        if not isinstance(card, dict) or not card.get("id") or not isinstance(card.get("title"), str):
            raise _bad(src, "a card has no id or title")
        list_id = card.get("list_id") if card.get("list_id") in ids else by_title.get(card.get("column"))
        if list_id is None:
            raise _bad(src, f"card {card['id']} is in a column that is not in columns")
        cards.append({**card, "list_id": list_id})
    return {"columns": columns, "cards": cards}


def db_path() -> Path:
    return C.CONFIG_DIR / (C.TRACKER.get("path") or "board.db")


def _safety_copy(conn) -> Path:
    path = db_path().with_name(f"{db_path().name}.before-restore-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path


def _replace(conn, data):
    board_id = conn.execute("SELECT id FROM boards LIMIT 1").fetchone()["id"]
    now = datetime.now(timezone.utc).isoformat()
    top = max([conn.execute("SELECT COALESCE(MAX(seq), 0) FROM cards").fetchone()[0],
               *[c["seq"] for c in data["cards"] if isinstance(c.get("seq"), int)]])
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM cards")
    conn.execute("DELETE FROM columns WHERE board_id = ?", (board_id,))
    for pos, col in enumerate(data["columns"]):
        conn.execute("INSERT INTO columns (id, board_id, title, pos) VALUES (?, ?, ?, ?)",
                     (col["id"], board_id, col["title"], pos))
    for card in data["cards"]:
        if not isinstance(card.get("seq"), int):
            top += 1
        conn.execute(
            "INSERT INTO cards (id, board_id, list_id, title, description, tags_json, metadata_json, assigned_to, "
            "updated_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card["id"], board_id, card["list_id"], card["title"], card.get("description") or "",
             json.dumps(list(card.get("tags") or [])), json.dumps(dict(card.get("metadata") or {}), ensure_ascii=False),
             card.get("assigned_to"), card.get("updated_at") or now,
             card["seq"] if isinstance(card.get("seq"), int) else top))
    conn.execute("COMMIT")


def restore(src: Path, yes: bool) -> str:
    if (C.TRACKER or {}).get("type") != "local":
        raise SystemExit('pl board restore: needs [tracker] type = "local"')
    data = load(src)
    lock = open(C.LOCK_FILE, "a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("pl board restore: stop the dispatcher for this profile first") from None
        conn = localstore.open_db(db_path())
        had = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if not yes:
            print(f"would replace {had} local cards with {len(data['cards'])} cards from {src}; re-run with --yes")
            raise SystemExit(1)
        copy = _safety_copy(conn)
        try:
            _replace(conn, data)
        except Exception as e:  # noqa: BLE001 - any failure leaves the old board
            conn.rollback()
            raise SystemExit(f"pl board restore: failed, board unchanged: {e}") from e
        finally:
            conn.close()
        events.emit("board restored", source=str(src), cards=len(data["cards"]))
        return (f"restored {len(data['cards'])} cards in {len(data['columns'])} columns from {src}\n"
                f"previous board kept at {copy}")
    finally:
        lock.close()
