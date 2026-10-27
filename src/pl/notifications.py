"""One place that tells the owner about a named event: on the desktop (attention_cmd) and on chat connectors routed
for alerts. Only events listed in [notify] events are sent. Text carries card ids, PR numbers and short titles only."""
import json
import os
import time

from pl import config as C
from pl import manager, trackers
from pl.util import notify, short_id


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
    for c in all_cards:
        if now_col[c["id"]] != "PR open" or (c.get("metadata") or {}).get("dropped_at"):
            continue
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
