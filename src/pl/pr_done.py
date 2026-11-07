"""A card whose pull requests are all merged moves to Done, on every tracker; one closed unmerged goes to a person.

Each dispatcher pass reads the pull requests (one REST call each) of the cards in PR open, and of those in In progress
whose run agent has ended. Merged everywhere: Done, with a "## Closed" section naming each merge. No open one and some
closed unmerged: an open pr_closed alert. A pull request that cannot be read changes nothing; the next pass tries again."""
import json
import re

from pl import agents, alerts, board, rework
from pl.trackers import github
from pl.util import cmd_id, mask, notify, now_iso, short_id

WATCHED = ("PR open", "In progress")
REASON = "all PRs merged"
KEY = "pr_closed:"
SECTION = re.compile(r"^#{1,6}[ \t]+manual steps remaining[ \t]*$", re.I)
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*)$")
STEP_CHARS, MAX_STEPS = 300, 20
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def parse_steps(text):
    """None when the text has no "## Manual steps remaining" section, else its steps (one line each; [] when it is empty
    or says None). Headings inside code fences or HTML comments do not count."""
    lines, fenced, inside, steps = HTML_COMMENT.sub("", text or "").splitlines(), False, False, []
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if inside and re.match(r"^#{1,2}[ \t]", line):
            break
        if SECTION.match(line):
            inside = True
        elif inside and line.strip():
            item = ITEM.match(line)
            if item:
                steps.append(item.group(1).strip())
            elif steps:
                steps[-1] += " " + line.strip()
            else:
                steps.append(line.strip())
    if not inside:
        return None
    steps = [t for t in steps if t]
    if len(steps) == 1 and re.fullmatch(r"none\.?", steps[0], re.I):
        return []
    steps = [mask(t)[:STEP_CHARS] for t in steps]
    return steps[:MAX_STEPS] + ([f"and {len(steps) - MAX_STEPS} more, see the PR"] if len(steps) > MAX_STEPS else [])


def steps_of(states):
    """[{"pr": "o/r#1", "text": ...}] across the pull requests that were not closed unmerged."""
    return [{"pr": f"{p['repo']}#{p['number']}", "text": t} for p in states if p["state"] != "closed" for t in parse_steps(p["body"]) or []]


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
                   "at": pr.get("merged_at") or "", "sha": pr.get("merge_commit_sha") or "", "body": pr.get("body") or ""}
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


def _hold(c, steps, dry):
    """Keep a merged card in its column with the steps a person must finish; `pl done` is the release."""
    if dry:
        print(f"would hold {short_id(c['id'])}: {len(steps)} manual step(s) remaining")
        return
    now = board.card(c["id"])
    col = board.col_name(now["list_id"])
    if col not in WATCHED:
        c["list_id"] = now["list_id"]
        return
    held = {"column": col, "at": now_iso(), "steps": steps}
    try:
        board.update(c["id"], metadata={"manual_steps": held, "worker": None})
    except SystemExit as e:
        print(f"{short_id(c['id'])}: merged with manual steps, but not held yet ({e}); the next pass retries")
        return
    c["metadata"] = {**(c.get("metadata") or {}), "manual_steps": held, "worker": None}
    print(f"{short_id(c['id'])}: merged, {len(steps)} manual step(s) remaining; held for pl done")


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
        if c.get("unreadable"):
            continue
        held = (c.get("metadata") or {}).get("manual_steps")
        if held and held.get("column") != col and col != "Done" and not dry:
            board.update(c["id"], metadata={"manual_steps": None})
            c["metadata"] = {**c["metadata"], "manual_steps": None}
        elif held and held.get("column") == col:
            continue
        if col not in WATCHED or not urls:
            continue
        if col == "In progress" and not _ended(c, reg):
            continue
        states = [_state(u, seen) for u in urls]
        if None in states:
            active.add(f"{KEY}{c['id']}")   # a failed read neither opens nor clears an alert
            continue
        if all(s["state"] == "merged" for s in states):
            if _built(c, len(states)):
                steps = steps_of(states)
                _hold(c, steps, dry) if steps else _close(c, _note(states), dry)
        elif not any(s["state"] == "open" for s in states) and not dry:
            _closed_alert(c, [s for s in states if s["state"] == "closed"], active)
    if not dry:
        alerts.sweep(KEY, active, notify)
