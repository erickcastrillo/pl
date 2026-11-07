"""A card whose pull requests are all merged moves to Done, on every tracker; one closed unmerged goes to a person.

Each dispatcher pass reads the pull requests (one REST call each) of the cards in PR open, and of those in In progress
whose run agent has ended. Merged everywhere: Done, with a "## Closed" section naming each merge. No open one and some
closed unmerged: an open pr_closed alert. A pull request that cannot be read changes nothing; the next pass tries again."""

from pl import agents, alerts, board, slices
from pl.util import cmd_id, notify, short_id

WATCHED = ("PR open", "In progress")
REASON = "all PRs merged"
KEY = "pr_closed:"


def _note(prs):
    lines = [f"- {p['repo']}#{p['number']} merged {p['at']} ({p['sha'][:9] or 'commit unknown'})" for p in prs]
    return "All pull requests merged:\n" + "\n".join(lines)


def _ended(c, reg):
    w = (c.get("metadata") or {}).get("worker")
    return not w or agents.worker_status(w, reg)[0] == "dead"


def _close(c, note, dry):
    from pl import commands   # commands imports dispatch, which imports this module
    if dry:
        print(f"would move {short_id(c['id'])} to Done")
        return
    now = board.card(c["id"])
    if board.col_name(now["list_id"]) not in WATCHED:
        c["list_id"] = now["list_id"]
        return
    try:
        for line in commands.done(c["id"], [c], note=note, reason=REASON, checked=True):
            print(line)
    except SystemExit as e:
        print(f"{short_id(c['id'])}: merged, but not moved to Done yet ({e}); the next pass retries")
        return
    c["list_id"] = board.col_id("Done")   # the rest of this pass reads these same dicts: no agent starts on it
    c["metadata"] = {**(c.get("metadata") or {}), "worker": None}


def _closed_alert(c, closed, active):
    cid = c["id"]
    active.add(f"{KEY}{cid}")
    refs = ", ".join(f"{p['repo']}#{p['number']}" for p in closed)
    title = f"Card {short_id(cid)}: {refs} closed without merging"
    fix = f"open a new pull request or run pl done {cmd_id(cid)}"
    if why := alerts.open(f"{KEY}{cid}", "warn", title, fix, cards=[cid]):
        notify(alerts.headline(why, title), fix)


def _close_or_hold(c, states, seen, active, dry):
    if not slices.planned(c):
        return _close(c, _note(states), dry)
    v = slices.view(c, seen)
    if v["unread"]:
        return slices.keep_alert(c, active)
    if not slices.unmerged(v):
        return _close(c, _note(states), dry)
    if dry:
        print(f"would hold {short_id(c['id'])}: slices {', '.join(str(s['k']) for s in slices.unmerged(v))} not merged")
        return
    slices.alert(c, slices.hold(c, v), active)


def sweep(all_cards, reg, dry):
    active, held = set(), set()
    seen = {}
    for c in all_cards:
        urls = (c.get("metadata") or {}).get("pr_urls") or []
        col = board.col_name(c.get("list_id"))
        if c.get("unreadable") or col not in WATCHED or not urls:
            continue
        if col == "In progress" and not _ended(c, reg):
            continue
        if slices.planned(c) and slices.held(c):
            held.add(f"{slices.KEY}{c['id']}")   # held until its pull requests or plan change: no read
            continue
        states = [slices.pr_state(u, seen) for u in urls]
        if None in states:
            active.add(f"{KEY}{c['id']}")   # a failed read neither opens nor clears an alert
            continue
        if all(s["state"] == "merged" for s in states):
            _close_or_hold(c, states, seen, held, dry)
        elif not any(s["state"] == "open" for s in states) and not dry:
            _closed_alert(c, [s for s in states if s["state"] == "closed"], active)
    if not dry:
        alerts.sweep(KEY, active, notify)
        slices.sweep_alerts(held)
