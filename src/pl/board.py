"""Board access through the configured tracker, and the card-section contract."""
import hashlib
import json
import os
import re
import threading
import time

from pl import config as C
from pl import ghquota, trackers
from pl.util import BadInput


# ---------- board access (thin wrappers over the configured tracker; names and signatures are a compat contract) ----------

def _t():
    return trackers.get("tracker")


def lists():
    return {t: {"id": i, "title": t} for t, i in _t().columns().items()}


def col_id(name):
    if name not in lists():
        raise BadInput(f"pl: the board has no column {name!r} (for Inbox run: pl board init)")
    return lists()[name]["id"]


def col_name(list_id):
    return next((t for t, l in lists().items() if l["id"] == list_id), "?")


_clock = time.time   # tests fake the clock
REUSE_MAX = 330      # seconds a reader reuses a save at most: an idle dispatcher's longest wait (300) + 30
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
    if not C.STATE_DIR:
        return None
    if "mode" not in _SHARE:
        return "read"
    return _SHARE["mode"] if _SHARE.get("dir") == C.STATE_DIR else None


def share_seconds():
    return int((C.TRACKER or {}).get("share_seconds") or 120)


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
        pace = ghquota.pace() if str((C.TRACKER or {}).get("type") or "").startswith("github") else 1
        at, floor = got.get("at"), share_seconds()
        hold = floor
        if (got.get("hold") or 0) > floor:
            from pl.dispatch import dispatcher_running   # a stopped dispatcher's long hold no longer applies
            if dispatcher_running():
                hold = got["hold"]
        hold = min(hold, REUSE_MAX) * pace   # low budget: longer
        stamp = max(at, got.get("patched") or 0) if isinstance(at, (int, float)) else 0
        if (got.get("key") == _key() and isinstance(at, (int, float)) and 0 <= _clock() - at < hold
                and stamp > (last_write() or 0) and isinstance(got.get("cards"), list) and not _fresh()):
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


def patch(name, args, kw, res, before):
    """After a successful write: put that one card into the shared save and keep the save fresh. Anything that cannot
    be patched in place leaves the save stale, so the next reader reads the board."""
    if not C.STATE_DIR or name == "delete":
        return
    import fcntl
    from datetime import datetime, timezone
    path = C.STATE_DIR / "board-cache.json"
    try:
        lock = open(C.STATE_DIR / "board-cache.lock", "a")
    except OSError:
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        got = _load(path)
        at = got.get("at")
        if (got.get("key") != _key() or not isinstance(got.get("cards"), list) or not isinstance(at, (int, float))
                or max(at, got.get("patched") or 0) <= before):
            return
        ref = res if isinstance(res, str) else None
        res = res if isinstance(res, dict) else {}
        item_id = res.get("id") or ref or (args[0] if args else kw.get("item_id"))
        column = kw.get("column") or (args[1] if name in ("move", "adopt") and len(args) > 1 else None)
        old = next((c for c in got["cards"] if c.get("id") == item_id), None)
        if name == "create":
            new = res
        elif old is None:
            return
        elif "list_id" in res:
            new = res
        else:
            new = dict(old)
            for k in ("title", "description", "tags", "assigned_to", "list_id"):
                if k in kw:
                    new[k] = kw[k]
            if column:
                new["list_id"] = col_id(column)
            if kw.get("metadata") is not None:
                new["metadata"] = {**(old.get("metadata") or {}), **kw["metadata"]}
        if not isinstance(new, dict) or "list_id" not in new:
            return
        new = {**new, "updated_at": datetime.now(timezone.utc).isoformat()}
        got["cards"] = ([*got["cards"], new] if name == "create" or old is None
                        else [new if c is old else c for c in got["cards"]])
        got["patched"] = _clock()
        _save(path, got)


def card(item_id):
    return _t().card(item_id)


def matches(token, cs, min_prefix=1):
    """The cards token names: the full id; for a GitHub id "owner/repo#43" also "repo#43" (any case) or "#43";
    else the cards whose id starts with token (min_prefix+ chars)."""
    token = str(token).strip()
    if hits := [c for c in cs if c["id"] == token]:
        return hits
    if "#" in token and "/" not in token:
        name, _, num = token.rpartition("#")
        if num.isdigit():
            return [c for c in cs if "#" in c["id"] and c["id"].rpartition("#")[2] == num
                    and (not name or c["id"].rpartition("#")[0].rsplit("/", 1)[-1].lower() == name.lower())]
    return [c for c in cs if len(token) >= min_prefix and c["id"].startswith(token)]


def pick(token, cs, who="pl", min_prefix=1):
    """The one card token names (see matches), or SystemExit naming every match: never a guess."""
    hits = matches(token, cs, min_prefix)
    if len(hits) != 1:
        some = f": {', '.join(c['id'] for c in hits[:10])}; give the full id" if hits else ""
        raise BadInput(f"{who}: {len(hits)} cards match {token!r}{some}")
    return hits[0]


def find_card(token):
    """Full id, a unique id prefix, or for a GitHub card "repo#N" or "#N" (see matches). A full GitHub id
    "owner/repo#N" (what agents' pl card and pl section pass) is one issue read, never the whole board."""
    from pl import sync
    if (C.TRACKER or {}).get("type") == "local" and (lid := sync.local_id(token)):
        return card(lid)
    if len(token) >= 32 or ((C.TRACKER or {}).get("type") == "github-project"
                            and re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", token.strip())):
        return card(token.strip())
    return card(pick(token, cards())["id"])


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


def _whole(v):
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return int(v.strip()) if isinstance(v, str) and re.fullmatch(r"[0-9]+", v.strip()) else None


def plan_slice(c):
    """(plan, slice) of a card's metadata as whole numbers; None for what is missing or not a whole number."""
    m = c.get("metadata") or {}
    return _whole(m.get("plan")), _whole(m.get("slice"))


def plan_order(c):
    """Sort key: the one rule `pl list --plan` and the console's plan filter share (slice, then card id)."""
    s = plan_slice(c)[1]
    head, _, num = str(c.get("id") or "").rpartition("#")
    return (s is None, s or 0, head, int(num) if num.isdigit() else 0, str(c.get("id") or ""))
