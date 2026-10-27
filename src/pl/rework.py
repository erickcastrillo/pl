"""Auto-rework: a pull request the auto-review labeled for rework goes back to a run agent.

Each dispatcher pass (at most every REWORK_EVERY seconds) lists the open PRs labeled `rework` in the repos of the
cards in PR open, and moves each such card back to In progress with metadata.rework = {round, prs, at, active}. The
dispatcher then starts its run agent with ` rework=<round>` (start_worker), and the pl-run skill fixes the review's
findings on the PR's own branch instead of building the plan again. After [dispatch] rework_rounds rounds (0 = off)
the PRs get the `failed` label and a comment: a person decides."""
import json
import re
import shlex
import time
import urllib.parse

from pl import config as C
from pl import events, jobs, notifications
from pl.board import col_name, update
from pl.trackers import github
from pl.util import now_iso, short_id

REWORK_EVERY = 600   # seconds between two looks at the rework label (one REST call per repo)
PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
SAFE_PR = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)$")   # the repo name lands in typed text
_LAST = {"at": None}
SEEN = {}   # PR url -> frozenset of its label names, as the last rework or merge look saw it (closed PRs dropped)


def saw(url, state, names):
    if str(state).upper() == "OPEN":
        SEEN[url] = frozenset(names)
    else:
        SEEN.pop(url, None)


def labels():
    lab = C.CODE_HOST.get("labels") or {}
    return {"rework": lab.get("rework") or "pl:needs-rework", "failed": lab.get("failed") or "pl:review-failed",
            "review": lab.get("review"), "ready": lab.get("ready")}


def rounds():
    try:
        return max(0, int(C.DISPATCH.get("rework_rounds", 3)))
    except (TypeError, ValueError):
        return 3


def _prs(c):
    """[(repo, number, url)] of the pull requests recorded on the card, in the order they were recorded."""
    return [(m.group(1), int(m.group(2)), u) for u in ((c.get("metadata") or {}).get("pr_urls") or [])
            if (m := PR_URL.match(u))]


def flagged(repos, lab):
    """{(repo, number)} of the open PRs labeled rework and not failed, one REST call per repo."""
    out = set()
    for repo in sorted(repos):
        q = urllib.parse.urlencode({"state": "open", "labels": lab["rework"], "per_page": 100})
        for it in json.loads(github._gh(["api", f"repos/{repo}/issues?{q}"]) or "[]"):
            names = {x.get("name") for x in it.get("labels") or []}
            if it.get("pull_request"):
                saw(it.get("html_url") or "", "open", names)
            if it.get("pull_request") and lab["failed"] not in names:
                out.add((repo, int(it["number"])))
    return out


def look_at_ready(repos, lab):
    """Record the labels of the open PRs carrying the ready label, one REST call per repo, so SEEN has them
    without the auto-merge loop. A repo that cannot be read is skipped."""
    for repo in sorted(repos):
        q = urllib.parse.urlencode({"state": "open", "labels": lab["ready"], "per_page": 100})
        try:
            items = json.loads(github._gh(["api", f"repos/{repo}/issues?{q}"]) or "[]")
        except (SystemExit, Exception):  # noqa: BLE001 - a failed look is tried again at the next one
            continue
        for it in items:
            if it.get("pull_request"):
                saw(it.get("html_url") or "", "open", {x.get("name") for x in it.get("labels") or []})


def _give_up(c, todo, n, lab, dry):
    head = short_id(c["id"])
    print(f"{head}  PR {', '.join(f'#{num}' for _, num, _ in todo)} still fails review after {n} rework rounds; "
          f"labeled {lab['failed']} for a person")
    if dry:
        return
    note = (f"pl auto-rework: this pull request still fails review after {n} automatic rework rounds, so it is "
            f"labeled `{lab['failed']}` for a person to decide: fix it by hand, send it back once more "
            f"(remove `{lab['failed']}`), or close it.")
    for repo, num, _ in todo:
        github._gh(["api", "-X", "POST", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={lab['failed']}"])
        github._gh(["api", "-X", "POST", f"repos/{repo}/issues/{num}/comments", "-f", f"body={note}"])
    rw = (c.get("metadata") or {}).get("rework") or {}
    update(c["id"], metadata={"rework": {**rw, "active": False, "gave_up_at": now_iso()}})
    events.emit("rework_gave_up", c["id"], prs=[u for _, _, u in todo], rounds=n)


def _send_back(c, todo, n, dry):
    from pl.commands import move_to
    head = short_id(c["id"])
    print(f"{head}  PR {', '.join(f'#{num}' for _, num, _ in todo)} needs rework: back to In progress (round {n})")
    if dry:
        return
    update(c["id"], metadata={"rework": {"round": n, "prs": [u for _, _, u in todo], "at": now_iso(), "active": True}})
    move_to(c["id"], "In progress", c["id"])
    events.emit("rework", c["id"], prs=[u for _, _, u in todo], round=n)
    notifications.send("review_rework", f"Rework: {head} {', '.join(f'#{num}' for _, num, _ in todo)}",
                       f"round {n}: {c['title'][:50]}")


def sweep(all_cards, dry, force=False):
    """Send cards in PR open whose PRs are labeled rework back to In progress, or give up after rework_rounds.
    Clears metadata.rework.active on cards back in PR open (that round is over). Returns the cards sent back."""
    n_max = rounds()
    if not n_max:
        return []
    now = time.time()
    if not force and _LAST["at"] is not None and now - _LAST["at"] < REWORK_EVERY:
        return []
    lab = labels()
    open_cards = [c for c in all_cards if col_name(c["list_id"]) == "PR open"
                  and (c.get("metadata") or {}).get("pipeline_mode") == "auto" and _prs(c)]
    for c in open_cards:
        rw = (c.get("metadata") or {}).get("rework") or {}
        if rw.get("active") and not dry:
            update(c["id"], metadata={"rework": {**rw, "active": False}})
            c["metadata"] = {**c["metadata"], "rework": {**rw, "active": False}}
    if not open_cards:
        _LAST["at"] = now
        return []
    hits = flagged({repo for c in open_cards for repo, _, _ in _prs(c)}, lab)
    _LAST["at"] = now   # only a look that worked waits: a failed one is tried again next pass
    sent = []
    for c in open_cards:
        todo = [p for p in _prs(c) if p[:2] in hits]
        if not todo:
            continue
        n = int(((c.get("metadata") or {}).get("rework") or {}).get("round") or 0) + 1
        if n > n_max:
            _give_up(c, todo, n - 1, lab, dry)
        else:
            _send_back(c, todo, n, dry)
            sent.append(c["id"])
    return sent


def prompt_arg(c):
    """' rework=<round> rework_label=<label> ready=<label>' for a run agent of a card sent back by sweep, else ''.
    pl-run takes the labels off the PR when it is fixed (the review label is passed as review= already)."""
    rw = (c.get("metadata") or {}).get("rework") or {}
    if not rw.get("active"):
        return ""
    lab, q = labels(), shlex.quote
    return (f" rework={int(rw.get('round') or 1)} rework_label={q(lab['rework'])}"
            + (f" ready={q(lab['ready'])}" if lab.get("ready") else ""))


def live_message(c):
    """The one line typed into a card's live run agent for a rework round: the round and how to read the review,
    never card text."""
    rw = (c.get("metadata") or {}).get("rework") or {}
    n = int(rw.get("round") or 1)
    ms = [m for u in rw.get("prs") or [] if (m := SAFE_PR.match(str(u)))]
    reads = " ; ".join(f"gh api repos/{m.group(1)}/issues/{m.group(2)}/comments --jq '.[-1].body'" for m in ms) \
        or "read the latest review comment on the card's pull request"
    return (f"pl: the review sent this card's pull request back for rework (round {n}). Read the review with: {reads} . "
            f"Fix every finding on the PR's own branch and push, as /pl-run does with rework={n}.")


def cmd_rework(a):
    """pl rework: one sweep now, ignoring the REWORK_EVERY wait."""
    from pl.board import cards
    if not rounds():
        raise SystemExit("pl rework: auto-rework is off ([dispatch] rework_rounds = 0)")
    with jobs.track("rework"):
        jobs.progress(f"looking for pull requests labeled {labels()['rework']} on cards in PR open")
        sent = sweep(cards(), a.dry_run, force=True)
        jobs.result(f"{len(sent or [])} card(s) sent back")
