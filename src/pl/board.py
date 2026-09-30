"""Board access through the configured tracker, and the card-section contract."""
import hashlib
import json
import os
import re
import threading
import time

from pl import config as C
from pl import trackers


# ---------- board access (thin wrappers over the configured tracker; names and signatures are a compat contract) ----------

def _t():
    return trackers.get("tracker")


def lists():
    return {t: {"id": i, "title": t} for t, i in _t().columns().items()}


def col_id(name):
    if name not in lists():
        raise SystemExit(f"pl: the board has no column {name!r} (for Inbox run: pl board init)")
    return lists()[name]["id"]


def col_name(list_id):
    return next((t for t, l in lists().items() if l["id"] == list_id), "?")


_clock = time.time   # tests fake the clock
REUSE_MAX = 120      # seconds a reader reuses a save at most, however long the dispatcher waits
FRESH_FOR = 5        # seconds after fresh_next that no thread reuses or seeds from the save
_SHARE = {}          # this process's part in the shared board read: mode, hold, fresh until, and the state folder it is for


def share(mode, hold=None):
    """"write": save each board read to <profile>/state/board-cache.json (the dispatcher; hold = seconds the save stays
    good, its next wait + 30). "read": reuse a save while it is good instead of reading the board (the console, pl list)."""
    _SHARE.update(mode=mode, hold=hold, dir=C.STATE_DIR)


def fresh_next():
    """The next board read asks the board, not the shared save nor the tracker's copy (r in the console; a write
    that edits what it read). For FRESH_FOR seconds no thread reuses the save or seeds the tracker from it, so a console
    refresh running meanwhile cannot hand the write an old copy."""
    _SHARE["fresh_until"] = _clock() + FRESH_FOR
    trackers.reset("tracker")


def _fresh():
    return _clock() < _SHARE.get("fresh_until", 0)


def _mode():
    return _SHARE.get("mode") if _SHARE.get("dir") == C.STATE_DIR and C.STATE_DIR else None


def _save(path, data):
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:   # it holds card bodies
            f.write(json.dumps(data, default=str))
        os.replace(tmp, path)
    except OSError:
        pass


def _load(path):
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def dirty():
    """A write through pl: no process reuses a board read that started before now."""
    if C.STATE_DIR:
        _save(C.STATE_DIR / "board-dirty.json", {"at": _clock()})


def last_write():
    """When pl last wrote to the board from any process of the profile, or None."""
    return _load(C.STATE_DIR / "board-dirty.json").get("at") if C.STATE_DIR else None


def _key():
    return hashlib.sha256(json.dumps(C.TRACKER, sort_keys=True, default=str).encode()).hexdigest()[:16]


def cards():
    mode = _mode()
    if mode == "read" and not _fresh():
        got = _load(C.STATE_DIR / "board-cache.json")
        at, hold = got.get("at"), min(got.get("hold") or C.DISPATCH["interval"] + 30, REUSE_MAX)
        if (got.get("key") == _key() and isinstance(at, (int, float)) and 0 <= _clock() - at < hold
                and at > (last_write() or 0) and isinstance(got.get("cards"), list) and not _fresh()):
            if hasattr(_t(), "seed"):   # a tracker whose card(id) reads from its own listing takes this one, until the save's end
                _t().seed(got["cards"], at, at + hold)
            return got["cards"]
    t = _t()
    if (r := getattr(t, "_read", None)) and r[2] <= (last_write() or 0):
        t._read = None   # the tracker's own copy is from before another process wrote: read again
    start = _clock()
    out = t.cards()
    if mode:   # stamped with when the board was really read: the tracker may have answered from its copy
        at = min(start, r[2]) if (r := getattr(t, "_read", None)) else start
        _save(C.STATE_DIR / "board-cache.json", {"at": at, "hold": _SHARE.get("hold"), "key": _key(), "cards": out})
    return out


def card(item_id):
    return _t().card(item_id)


def find_card(token):
    """Full id, or a unique id prefix (8+ chars)."""
    if len(token) >= 32:
        return card(token)
    hits = [c for c in cards() if c["id"].startswith(token)]
    if len(hits) != 1:
        raise SystemExit(f"pl: {len(hits)} cards match {token!r}")
    return card(hits[0]["id"])


def update(item_id, **fields):
    """PATCH with a client-side metadata merge, then assert the reply echoes what we sent."""
    return _t().update(item_id, **fields)


# ---------- card size (the board server cuts replies it thinks too large) ----------

MAX_CARD_CHARS = 40_000


def card_size(description):
    """Characters the description takes in the server's JSON reply."""
    return len(json.dumps(description or ""))


def max_card_chars():
    return int((C.TRACKER or {}).get("max_card_chars") or MAX_CARD_CHARS)


def check_size(description):
    """description, or SystemExit when the card would be over the limit. Call it before any tracker write."""
    n, limit = card_size(description), max_card_chars()
    if n > limit:
        raise SystemExit(f"pl: this card would be {n:,} characters, over the limit of {limit:,} (the board server "
                         "cannot return larger cards); split the feature into smaller cards")
    return description


# ---------- card sections ----------

MARK = re.compile(r"^# PIPELINE: ([A-Z][A-Z ]*?)\s*$", re.M)


def sections(desc):
    desc = desc or ""
    ms = list(MARK.finditer(desc))
    if not ms:
        return {"": desc} if desc.strip() else {}
    parts = {}
    if desc[:ms[0].start()].strip():
        parts[""] = desc[:ms[0].start()]
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(desc)
        parts[m.group(1)] = desc[m.end():end].strip("\n")
    return parts


def render(parts):
    out = []
    if parts.get(""):
        out.append(parts[""].rstrip("\n") + "\n")
    for k, v in parts.items():
        if k:
            out.append(f"# PIPELINE: {k}\n{v.rstrip()}\n")
    return "\n".join(out).rstrip() + "\n"
