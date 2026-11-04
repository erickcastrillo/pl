"""A card whose pull requests are all merged moves to Done, on every tracker; one closed unmerged goes to a person.

Each dispatcher pass reads the pull requests (one REST call each) of the cards in PR open, and of those in In progress
whose run agent has ended. Merged everywhere: Done, with a "## Closed" section naming each merge. No open one and some
closed unmerged: an open pr_closed alert. A pull request that cannot be read changes nothing; the next pass tries again."""
import json
import re

from pl import agents, alerts, board, rework
from pl.trackers import github
from pl.util import cmd_id, notify, short_id

WATCHED = ("PR open", "In progress")
REASON = "all PRs merged"
KEY = "pr_closed:"


def _state(url, seen):
    """{"url", "repo", "number", "state": merged|open|closed, "at", "sha"}, or None when it cannot be read."""
    if url in seen:
        return seen[url]
    m = rework.PR_URL.match(url)
    got = None
    if m:
        try:
            pr = json.loads(github._gh(["api", f"repos/{m.group(1)}/pulls/{m.group(2)}"]))
            got = {"url": url, "repo": m.group(1), "number": int(m.group(2)),
                   "state": "merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "open",
                   "at": pr.get("merged_at") or "", "sha": pr.get("merge_commit_sha") or ""}
        except (SystemExit, ValueError, AttributeError):   # RateLimited and OverBudget are SystemExits
            got = None
    seen[url] = got
    return got


def _note(prs):
    lines = [f"- {p['repo']}#{p['number']} merged {p['at']} ({p['sha'][:9] or 'commit unknown'})" for p in prs]
    return "All pull requests merged:\n" + "\n".join(lines)


def _built(c, n):
    """False for an In progress stacked card with more slices in its plan than pull requests: the rest is not built."""
    if board.col_name(c["list_id"]) != "In progress":
        return True
    plan = board.sections(c.get("description")).get("PLAN") or ""
    return len(re.findall(r"^### Slice \d+", plan, re.M)) <= n


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
        for line in commands.done(c["id"], [c], note=note, reason=REASON):
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


def sweep(all_cards, reg, dry):
    active = set()
    seen = {}
    for c in all_cards:
        urls = (c.get("metadata") or {}).get("pr_urls") or []
        col = board.col_name(c.get("list_id"))
        if c.get("unreadable") or col not in WATCHED or not urls:
            continue
        if col == "In progress" and not _ended(c, reg):
            continue
        states = [_state(u, seen) for u in urls]
        if None in states:
            active.add(f"{KEY}{c['id']}")   # a failed read neither opens nor clears an alert
            continue
        if all(s["state"] == "merged" for s in states):
            if _built(c, len(states)):
                _close(c, _note(states), dry)
        elif not any(s["state"] == "open" for s in states) and not dry:
            _closed_alert(c, [s for s in states if s["state"] == "closed"], active)
    if not dry:
        alerts.sweep(KEY, active, notify)
