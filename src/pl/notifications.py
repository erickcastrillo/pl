"""One place that tells the owner about a named event: on the desktop (attention_cmd) and on chat connectors routed
for alerts. Only events listed in [notify] events are sent. Text carries card ids, PR numbers and short titles only."""
import json
import os
import subprocess
import sys
from pathlib import Path

from pl import config as C
from pl import manager, trackers
from pl.util import notify, short_id


TEST_TITLE = "pl test notification"
TEST_MESSAGE = "If you can read this, pl can reach you."


def send(event, title, message) -> bool:
    if event not in C.NOTIFY["events"] or trackers.dry_run():
        return False
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
