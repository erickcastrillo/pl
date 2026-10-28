"""One place that tells the owner about a named event: on the desktop (attention_cmd) and on chat connectors routed
for alerts. Only events listed in [notify] events are sent. Text carries card ids, PR numbers and short titles only."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

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


TEST_TITLE = "pl test notification"
TEST_MESSAGE = "If you can read this, pl can reach you."

_LOOKED = {"at": None}   # when review_passed last asked GitHub for the ready-labeled PRs


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


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, timeout=30)
    except OSError:
        return subprocess.CompletedProcess(argv, 127)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124)


def _attention():
    if not C.ATTENTION:
        return False, "attention_cmd: failed: not set (pl setup, or [paths] attention_cmd)"
    path = Path(C.ATTENTION)
    if not os.access(path, os.X_OK):
        return False, f"attention_cmd: failed: {path} is missing or not executable"
    result = _run([str(path), "notify", TEST_TITLE, TEST_MESSAGE])
    if result.returncode:
        return False, f"attention_cmd: failed: exit {result.returncode}"
    return True, "attention_cmd: sent"


def _chat(name):
    from pl import chat
    try:
        ad = chat.adapter(name)
        replies = [ad.send(str(chat_id), f"{TEST_TITLE}: {TEST_MESSAGE}") for chat_id in chat.load()[name].get("allow", [])]
    except Exception as e:  # noqa: BLE001 - only the class is shown: its text could hold a token
        return False, f"{name}: failed ({type(e).__name__})"
    if any(isinstance(r, dict) and not r.get("ok", True) for r in replies):
        return False, f"{name}: failed (error reply)"
    return True, f"{name}: sent"


def cmd_notify(a):
    from pl import chat
    if a.chat and a.chat not in chat.load():
        print(f"pl notify test: {a.chat} is not a configured chat connector (pl chat list)", file=sys.stderr)
        raise SystemExit(1)
    if trackers.dry_run():
        print("dry-run: nothing sent")
        raise SystemExit(0)
    print(f"sent: {TEST_TITLE} / {TEST_MESSAGE}")
    results = [_attention()] + ([_chat(a.chat)] if a.chat else [])
    for _, line in results:
        print(line)
    raise SystemExit(0 if all(ok for ok, _ in results) else 1)


def review_passed(all_cards, now_col, st):
    """now_col: card id -> column name. One review_passed per card for the PRs that newly carry the ready label (and neither rework nor failed).
    The sent record is st["notified"]["review_passed:<url>"], kept even when the event is off."""
    from pl import rework   # rework imports commands, which imports dispatch
    lab = rework.labels()
    notified = st.setdefault("notified", {})
    live = {u for c in all_cards if now_col[c["id"]] != "Done" for u in (c.get("metadata") or {}).get("pr_urls") or []}
    for key in [k for k in notified if k.startswith("review_passed:") and k[len("review_passed:"):] not in live]:
        del notified[key]
    if not lab["ready"]:
        return
    open_cards = [c for c in all_cards if now_col[c["id"]] == "PR open" and not (c.get("metadata") or {}).get("dropped_at")]
    pending = {repo for c in open_cards for repo, _, url in rework._prs(c) if f"review_passed:{url}" not in notified}
    now = time.time()
    if pending and (_LOOKED["at"] is None or now - _LOOKED["at"] >= rework.REWORK_EVERY):
        _LOOKED["at"] = now
        rework.look_at_ready(pending, lab)
    for c in open_cards:
        new = []
        for _, num, url in rework._prs(c):
            names = rework.SEEN.get(url) or frozenset()
            if lab["ready"] in names and not names & {lab["rework"], lab["failed"]} and f"review_passed:{url}" not in notified:
                new.append((num, url))
        if not new:
            continue
        send("review_passed", f"Review passed: {short_id(c['id'])} " + ", ".join(f"#{n}" for n, _ in new), c["title"][:50])
        for _, url in new:
            notified[f"review_passed:{url}"] = time.time()
