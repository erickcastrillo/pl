"""A card whose plan has `### Slice k:` headings closes only when each slice has a merged pull request titled
"(slice k of n)". A plan with no slices makes no GitHub call here.

Pull request titles and slice headings are text from outside: shown cut and stripped of control characters, never run."""
import hashlib
import json
import re

from pl import alerts, board, rework
from pl.trackers import github
from pl.util import cmd_id, notify, short_id

KEY = "slices_missing:"
HEADING = re.compile(r"^### Slice (\d+):[ \t]*(.*)$", re.M)
TITLE = re.compile(r"\(slice (\d+) of \d+\)", re.I)
RANK = {"merged": 3, "open": 2, "closed": 1}


def _clean(text, n=70):
    return "".join(ch for ch in text if ch.isprintable())[:n].strip()


def planned(c):
    """[(k, name)] for the slices in the card's PLAN."""
    plan = board.sections(c.get("description")).get("PLAN") or ""
    return [(int(k), _clean(name)) for k, name in HEADING.findall(plan)]


def pr_state(url, seen):
    """{"url", "repo", "number", "state": merged|open|closed, "at", "sha", "title", "body"}, or None when it cannot be read."""
    if url in seen:
        return seen[url]
    m = rework.PR_URL.match(url)
    got = None
    if m:
        try:
            pr = json.loads(github._gh(["api", f"repos/{m.group(1)}/pulls/{m.group(2)}"]))
            got = {"url": url, "repo": m.group(1), "number": int(m.group(2)),
                   "state": "merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "open",
                   "at": pr.get("merged_at") or "", "sha": pr.get("merge_commit_sha") or "", "title": pr.get("title") or "", "body": pr.get("body") or ""}
        except (SystemExit, ValueError, AttributeError):   # RateLimited and OverBudget are SystemExits
            got = None
    seen[url] = got
    return got


def _ref(p):
    return f"{p['repo']}#{p['number']}"


def view(c, seen=None):
    """{"slices": [{"k", "name", "state", "pr"}], "strays": [refs], "unread": bool}. State: merged, open, closed,
    none or dropped, from the card's recorded pull requests titled for the slice; the best state wins."""
    seen = {} if seen is None else seen
    prs = [pr_state(u, seen) for u in (c.get("metadata") or {}).get("pr_urls") or []]
    best, strays = {}, []
    for p in filter(None, prs):
        m = TITLE.search(p["title"])
        if not m:
            strays.append(_ref(p))
        elif RANK[p["state"]] > RANK.get(best.get(int(m.group(1)), {}).get("state"), 0):
            best[int(m.group(1))] = {"state": p["state"], "pr": _ref(p)}
    dropped = set(((c.get("metadata") or {}).get("dropped_slices") or {}).get("numbers") or [])
    rows = [{"k": k, "name": name, "pr": best.get(k, {}).get("pr"),
             "state": "dropped" if k in dropped else best.get(k, {}).get("state", "none")} for k, name in planned(c)]
    return {"slices": rows, "strays": strays, "unread": None in prs}


def unmerged(v):
    return [s for s in v["slices"] if s["state"] not in ("merged", "dropped")]


def unbuilt(v):
    return [s for s in v["slices"] if s["state"] in ("none", "closed")]


def signature(c):
    plan = board.sections(c.get("description")).get("PLAN") or ""
    return hashlib.sha1(json.dumps([(c.get("metadata") or {}).get("pr_urls") or [], plan]).encode()).hexdigest()


def held(c):
    """The recorded hold while the card's pull requests and plan are the ones it was made for."""
    h = (c.get("metadata") or {}).get("slice_hold")
    return h if isinstance(h, dict) and h.get("sig") == signature(c) else None


def hold(c, v):
    h = {"sig": signature(c), "missing": [s["k"] for s in unmerged(v)], "strays": v["strays"]}
    board.update(c["id"], metadata={"slice_hold": h})
    c["metadata"] = {**(c.get("metadata") or {}), "slice_hold": h}
    return h


def alert(c, h, active):
    """Keep the card's alert open, telling the owner once: it is acked as it opens so no reminder follows."""
    key = f"{KEY}{c['id']}"
    active.add(key)
    if (a := alerts.get(key)) and not a.get("resolved_at"):
        return
    nums = ", ".join(str(k) for k in h["missing"])
    title = f"Card {short_id(c['id'])}: slices {nums} have no merged pull request" + "".join(
        f"; {r} names no slice" for r in h["strays"])
    fix = f'pl done {cmd_id(c["id"])} --drop-slices "<reason>"'
    alerts.open(key, "warn", title, fix, cards=[c["id"]])
    alerts.ack(key)


def sweep_alerts(active):
    alerts.sweep(KEY, active, notify)


def keep_alert(c, active):
    """A pass that could not read the card's pull requests neither opens nor clears its alert."""
    active.add(f"{KEY}{c['id']}")
