"""One place that tells the owner about a named event: on the desktop (attention_cmd) and on chat connectors routed
for alerts. Only events listed in [notify] events are sent. Text carries card ids, PR numbers and short titles only."""
import json
import os
import re
import sys
import time
from datetime import datetime

from pl import config as C
from pl import manager, trackers
from pl.util import mask, notify, short_id

QUIET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
URGENT_FOR = 2      # seconds an opened high alert lets notifications through
SUMMARY_LINES = 20
_URGENT = {"at": None}
_now = datetime.now
_mono = time.monotonic


def quiet_problem():
    v = C.NOTIFY.get("quiet")
    if v is None or v == "":
        return None
    m = QUIET_RE.match(v) if isinstance(v, str) else None
    if m and m.group(1, 2) != m.group(3, 4):
        return None
    return f'notify.quiet {v!r} must be HH:MM-HH:MM in 24-hour time, like "22:00-07:00"'


def _window():
    v = C.NOTIFY.get("quiet")
    if not v or quiet_problem():
        return None
    h1, m1, h2, m2 = map(int, QUIET_RE.match(v).groups())
    return h1 * 60 + m1, h2 * 60 + m2


def _inside(now):
    start, end = _window()
    m = now.hour * 60 + now.minute
    return start <= m < end if start < end else m >= start or m < end


def urgent(on=True):
    _URGENT["at"] = _mono() if on else None


def holding():
    if _window() is None or not _inside(_now()):
        return False
    at = _URGENT["at"]
    return at is None or _mono() - at >= URGENT_FOR


def _held_path():
    return C.STATE_DIR / "notify-held.jsonl"


def hold(title, message) -> bool:
    if not holding():
        return False
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(_held_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"at": _now().strftime("%H:%M"), "title": mask(title), "message": mask(message)}) + "\n")
    except OSError:
        return False
    return True


def release():
    """The first pass outside the window sends one summary of what was held. A failed summary never stops the pass."""
    path = _held_path()
    if holding() or not path.exists():
        return
    taken = path.with_name(f".notify-held.{os.getpid()}.tmp")
    try:
        os.replace(path, taken)
        lines = [json.loads(ln) for ln in taken.read_text().splitlines() if ln.strip()]
        taken.unlink()
        if not lines:
            return
        shown = [f"{e['at']} {e['title']}: {e['message']}" for e in lines[:SUMMARY_LINES]]
        if len(lines) > SUMMARY_LINES:
            shown.append(f"and {len(lines) - SUMMARY_LINES} more")
        title, message = f"Quiet hours: {len(lines)} held", "\n".join(shown)
        notify(title, message)
        _to_chat(f"{title}: {message}")
    except Exception as e:  # noqa: BLE001
        print(f"pl: quiet hours summary failed: {e}", file=sys.stderr)


def send(event, title, message) -> bool:
    if event not in C.NOTIFY["events"] or trackers.dry_run():
        return False
    if hold(title, message):
        return True
    notify(title, message)
    _to_chat(f"{title}: {message}")
    return True


def _to_chat(text):
    try:
        mdir = manager.machine_dir()
        mdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(mdir / "chat-outbox.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"key": "notify", "headline": text, "route": "alerts"}) + "\n")
    except Exception:  # noqa: BLE001 - a notification never stops the pass
        pass


def pr_ref(card) -> str:
    urls = (card.get("metadata") or {}).get("pr_urls") or []
    tail = str(urls[-1]).rstrip("/").rsplit("/", 1)[-1] if urls else ""
    return f"#{tail}" if tail.isdigit() else ""


def moves(cards_moved):
    """cards_moved: (card, from_column, to_column) for each card the pass saw move."""
    merged = []
    for card, _, to in cards_moved:
        meta = card.get("metadata") or {}
        sid, title = short_id(card["id"]), card["title"][:50]
        if to == "PR open":
            send("pr_opened", f"PR opened: {sid} {pr_ref(card)}".rstrip(), title)
        elif to == "Done" and not meta.get("dropped_at"):
            if meta.get("pr_urls"):
                merged.append(card)
            else:
                send("done", f"Done: {sid}", title)
    if len(merged) == 1:
        c = merged[0]
        send("merged", f"Merged: {short_id(c['id'])} {pr_ref(c)}".rstrip(), c["title"][:50])
    elif merged:
        send("merged", f"Merged: {len(merged)} PRs", ", ".join(f"{short_id(c['id'])} {pr_ref(c)}".rstrip() for c in merged))
