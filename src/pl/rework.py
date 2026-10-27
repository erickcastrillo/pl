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
from pl import alerts, events, heldwrites, jobs, notifications
from pl.board import card, col_name, update
from pl.trackers import github
from pl.util import now_iso, parse_iso, short_id

REWORK_EVERY = 600   # seconds between two looks at the rework label (one REST call per repo)
PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
MERGE_RE = re.compile(r"\b(merg\w*|rebas\w*|conflict\w*|behind main|out of date|up to date with main)\b", re.I)
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


def merge_only(text):
    """True when every finding in the review text asks only for a merge, a rebase or a conflict fix."""
    pieces = (re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", p).strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or ""))
    findings = [p for p in pieces if p and not p.startswith("#")]
    return bool(findings) and all(MERGE_RE.search(p) for p in findings)


def _review_text(repo, num):
    """The body of the newest comment on the pull request (where the auto-review posts its findings), or ''."""
    try:
        comments = json.loads(github._gh(["api", f"repos/{repo}/issues/{num}/comments?per_page=100"]) or "[]")
        return (comments[-1].get("body") or "") if comments else ""
    except (Exception, SystemExit):   # no review text is a normal rework, never a stopped sweep
        return ""


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


def _queued(cid, op, args, until):
    heldwrites.add(cid, op, args, until)
    print(f"{short_id(cid)}  queued: {heldwrites.target({'op': op, 'args': args})}; GitHub budget held, resets "
          f"{time.strftime('%H:%M', time.localtime(until))} (pl applies it after the reset)")


def _give_up(c, todo, n, lab, dry):
    head = short_id(c["id"])
    print(f"{head}  PR {', '.join(f'#{num}' for _, num, _ in todo)} still fails review after {n} rework rounds; "
          f"labeled {lab['failed']} for a person")
    if dry:
        return
    urls = [u for _, _, u in todo]
    try:
        _do_give_up(c, urls, n, lab)
    except github.OverBudget as e:
        _queued(c["id"], "give_up", {"prs": urls, "rounds": n}, e.until)


def _do_give_up(c, urls, n, lab):
    note = (f"pl auto-rework: this pull request still fails review after {n} automatic rework rounds, so it is "
            f"labeled `{lab['failed']}` for a person to decide: fix it by hand, send it back once more "
            f"(remove `{lab['failed']}`), or close it.")
    for repo, num in (m.groups() for m in map(PR_URL.match, urls)):
        github._gh(["api", "-X", "POST", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={lab['failed']}"])
        github._gh(["api", "-X", "POST", f"repos/{repo}/issues/{num}/comments", "-f", f"body={note}"])
    rw = (c.get("metadata") or {}).get("rework") or {}
    update(c["id"], metadata={"rework": {**rw, "active": False, "gave_up_at": now_iso()}})
    events.emit("rework_gave_up", c["id"], prs=urls, rounds=n)


def _send_back(c, todo, n, dry, mo=False):
    """True when the card went back (or would, dry); False when pl's budget hold queued the writes."""
    print(f"{short_id(c['id'])}  PR {', '.join(f'#{num}' for _, num, _ in todo)} needs rework: back to In progress (round {n})"
          + (", merge-only" if mo else ""))
    if dry:
        return True
    urls = [u for _, _, u in todo]
    try:
        _do_send_back(c, n, urls, mo)
    except github.OverBudget as e:
        _queued(c["id"], "rework", {"round": n, "prs": urls, "merge_only": mo}, e.until)
        return False
    return True


def _do_send_back(c, n, urls, mo=False):
    from pl.commands import move_to
    update(c["id"], metadata={"rework": {"round": n, "prs": urls, "at": now_iso(), "active": True, "merge_only": mo}})
    move_to(c["id"], "In progress", c["id"])
    events.emit("rework", c["id"], prs=urls, round=n)
    notifications.send("review_rework", f"Rework: {short_id(c['id'])} {', '.join('#' + u.rsplit('/', 1)[-1] for u in urls)}",
                       f"round {n}: {c['title'][:50]}")


def replay_send_back(entry):
    """heldwrites op rework: the reason to skip when the card left PR open or the review no longer asks for rework, else None."""
    c, urls = card(entry["card"]), entry["args"]["prs"]
    if col_name(c["list_id"]) != "PR open":
        return "the card is no longer in PR open"
    mine = [m.groups() for m in map(PR_URL.match, urls) if m]
    if not any((repo, int(num)) in flagged({r for r, _ in mine}, labels()) for repo, num in mine):
        return "the pull request is no longer labeled for rework"
    _do_send_back(c, entry["args"]["round"], urls, entry["args"].get("merge_only", False))


def replay_give_up(entry):
    _do_give_up(card(entry["card"]), entry["args"]["prs"], entry["args"]["rounds"], labels())


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
            try:
                update(c["id"], metadata={"rework": {**rw, "active": False}})
            except github.OverBudget:
                continue   # a send-back sets the whole rework value anyway
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
            if _send_back(c, todo, n, dry, all(merge_only(_review_text(r, num)) for r, num, _ in todo)):
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


def check_waits(waiting, all_cards, st, send):
    """A rework that has waited over [dispatch] rework_wait_minutes for a run slot: one Activity warning per wait and
    an open rework_waiting alert, resolved once the card is no longer waiting. waiting: run cards still unstarted."""
    limit = int(C.DISPATCH.get("rework_wait_minutes", 30)) * 60
    seen, active = st.setdefault("rework_waits", {}), set()
    for c in waiting:
        rw = (c.get("metadata") or {}).get("rework") or {}
        at = parse_iso(rw["at"]) if rw.get("active") and rw.get("at") else 0
        if not at or time.time() - at <= limit:
            continue
        cid, mins = c["id"], int((time.time() - at) // 60)
        active.add(key := f"rework_waiting:{cid}")
        if why := alerts.open(key, "warn", f"Card {short_id(cid)}: its rework has waited {mins} min for a run slot",
                              "pl list; free a run slot or raise [dispatch] max_runs", [cid]):
            send(alerts.headline(why, f"Card {short_id(cid)}: its rework has waited {mins} min for a run slot"),
                 "pl list; free a run slot or raise [dispatch] max_runs")
        if seen.get(cid) != rw["at"]:
            events.emit("rework_waiting", cid, minutes=mins)
            seen[cid] = rw["at"]
    live = {c["id"]: ((c.get("metadata") or {}).get("rework") or {}).get("at") for c in all_cards}
    st["rework_waits"] = {cid: at for cid, at in seen.items() if live.get(cid) == at}
    alerts.sweep("rework_waiting:", active, send)
