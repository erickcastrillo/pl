"""Append-only queue of local board changes waiting to be synced."""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from pl import config as C
from pl.util import now_iso


def path() -> Path | None:
    return (C.CONFIG_DIR / "outbox.jsonl") if C.CONFIG_DIR else None


def entries() -> list[dict]:
    p = path()
    if p is None or not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            if i == len(lines) - 1:
                break
    return out


def count() -> int:
    return len(entries())


@contextmanager
def _lock():
    state_dir = C.STATE_DIR or (C.CONFIG_DIR / "state" if C.CONFIG_DIR else Path("."))
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state_dir / "outbox.jsonl.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def append(card_id, op, fields) -> dict:
    with _lock():
        cur = entries()
        seq = (cur[-1]["seq"] + 1) if cur and "seq" in cur[-1] else 1
        rec = {
            "seq": seq,
            "card": str(card_id),
            "op": op,
            "fields": fields,
            "at": now_iso(),
        }
        line = json.dumps(rec, default=str) + "\n"
        p = path()
        if p is None:
            raise SystemExit("pl: outbox: no profile loaded")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.chmod(p, 0o600)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return rec


def _rewrite(keep):
    p = path()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, "".join(json.dumps(e, default=str) + "\n" for e in keep).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def drop_through(seq) -> int:
    """Remove the entries up to and including seq; returns how many are left."""
    with _lock():
        keep = [e for e in entries() if e.get("seq", 0) > seq]
        _rewrite(keep)
        return len(keep)


def strip(card, fields) -> int:
    """Take these fields out of the card's queued updates (a column move is `list_id`; None drops every update whole); returns how many entries changed."""
    gone = set(fields or ()) | ({"list_id"} if "column" in (fields or ()) else set())
    with _lock():
        keep, changed = [], 0
        for e in entries():
            if e.get("card") == str(card) and e.get("op") == "update" and (fields is None or gone & set(e["fields"])):
                changed += 1
                e = {**e, "fields": {k: v for k, v in e["fields"].items() if fields is not None and k not in gone}}
                if not e["fields"]:
                    continue
            keep.append(e)
        if changed:
            _rewrite(keep)
        return changed
