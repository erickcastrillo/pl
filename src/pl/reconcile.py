"""When a stage agent finished but its card, PR link or PR labels do not show it, pl applies those writes itself (plan 54 slice 2)."""
import json
import sys
from pathlib import Path

from pl import agent_screen, agents, alerts, board, events, move_agent, pipeline, rework, worktrees
from pl import config as C
from pl.trackers import github
from pl.util import cmd_id, parse_iso, short_id

PREFIX = "reconcile_failed:"


def _dispatch():
    from pl import dispatch   # imported here: dispatch imports this module
    return dispatch


def _finished(w, reg):
    """The agent's window is gone, its turn ended, or its screen says finished. The screen is read last: it costs a tmux call."""
    return (agents.worker_status(w, reg)[0] == "dead" or bool(w.get("finished_turn"))
            or (bool(w.get("pane")) and agent_screen.read(w["pane"])[1] == "finished"))


def _branch_pr(c):
    """The open PR from the card's worktree branch, or None. At most one GitHub call."""
    repo = c["id"].rpartition("#")[0]
    if "/" not in repo or not C.WORK_DIR:
        return None
    for tree in worktrees._list_worktrees(Path(C.WORK_DIR)):
        branch = tree.get("branch") or ""
        if branch.startswith("pl/") and worktrees._card_for(branch.removeprefix("pl/"), [c]):
            found = json.loads(github._gh(["pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                                           "--json", "url", "--limit", "1"]))
            return found[0]["url"] if found else None
    return None


def _labels(url, m):
    """(label to add, label to remove) for the PR; None for each that is already right. A PR that is not open, or a
    rework run with no commit since the review, needs nothing."""
    pr = json.loads(github._gh(["pr", "view", url, "--json", "state,labels,commits"]))
    if pr.get("state") != "OPEN":
        return None, None
    have = {x["name"] for x in pr.get("labels") or []}
    wanted = rework.labels()
    add = wanted["review"] if wanted["review"] and wanted["review"] not in have else None
    drop = wanted["rework"] if wanted["rework"] in have else None
    since = parse_iso((m.get("rework") or {}).get("at") or "")
    if (add or drop) and since and max((parse_iso(x.get("committedDate") or "") for x in pr.get("commits") or []), default=0) < since:
        return None, None
    return add, drop


def _write(c, name, fn, active):
    """Run one write. True when it landed. A failure opens the card's reconcile_failed alert; a held GitHub budget raises."""
    try:
        fn()
        return True
    except github.RateLimited:
        raise
    except (Exception, SystemExit):  # noqa: BLE001 - one card's failed write never stops the others
        cid = c["id"]
        active.add(f"{PREFIX}{cid}")
        title, fix = f"Card {short_id(cid)}: pl could not {name}", f"do it by hand, then the alert clears; pl card {cmd_id(cid)}"
        if why := alerts.open(f"{PREFIX}{cid}", "warn", title, fix, [cid]):
            _dispatch().notify(alerts.headline(why, title), fix)
        return False


def _move(c, col, changes, active):
    from pl import commands
    if not _write(c, f"move to {col}", lambda: commands.move_to(c["id"], col, c["id"]), active):
        return False
    c["list_id"] = board.col_id(col)   # the rest of this pass sees the card where it now is
    changes.append(f"moved to {col}")
    return True


def _link(c, url, changes, active):
    urls = (c["metadata"].get("pr_urls") or [])
    if url in urls:
        return True
    if not _write(c, f"link {url}", lambda: board.update(c["id"], metadata={"pr_urls": [*urls, url]}), active):
        return False
    c["metadata"]["pr_urls"] = [*urls, url]
    changes.append(f"linked PR {url}")
    return True


def _fix_labels(c, url, changes, active):
    add, drop = _labels(url, c["metadata"])
    if not (add or drop):
        return True
    todo = [*([f"add label {add} to {url}"] if add else []), *([f"remove label {drop} from {url}"] if drop else [])]
    argv = ["pr", "edit", url, *(["--add-label", add] if add else []), *(["--remove-label", drop] if drop else [])]
    if not _write(c, " and ".join(todo), lambda: github._gh(argv), active):
        return False
    changes.extend(f"added label {add}" if x.startswith("add") else f"removed label {drop}" for x in todo)
    return True


def _card(c, reg, done, active):
    """Apply the missing writes of one finished stage agent. True when the card reached its done state."""
    d, m = _dispatch(), c.get("metadata") or {}
    w, cid = m.get("worker") or {}, c["id"]
    if (c.get("unreadable") or m.get("pipeline_mode") != "auto" or m.get("dropped_at") or m.get("question") or not w
            or done.get(cid) == w.get("session_id") or move_agent.locked(cid)):
        return
    stage, col, changes = w.get("stage"), board.col_name(c["list_id"]), []
    if stage in ("spec", "plan"):
        if d.stage_for(c, col) != stage or d.stage_complete(c, col, stage) or stage.upper() not in board.sections(c.get("description")):
            return
        ok = _finished(w, reg) and _move(c, C.STAGE_DONE_AT[stage], changes, active)
    elif stage == "run" and col in ("Approved", "In progress", C.STAGE_DONE_AT["run"]):
        if not _finished(w, reg):
            return
        url = (m.get("pr_urls") or [None])[-1] or (_branch_pr(c) if col != C.STAGE_DONE_AT["run"] else None)
        if not url:
            return
        ok = _link(c, url, changes, active)
        if ok and col != C.STAGE_DONE_AT["run"]:
            ok = _move(c, C.STAGE_DONE_AT["run"], changes, active)
        ok = ok and _fix_labels(c, url, changes, active)
    else:
        return
    if changes:
        events.emit("reconciled", cid, stage=stage, changes=changes)
    if ok:
        done[cid] = w.get("session_id")


def sweep(all_cards, reg, st):
    """One pass over the cards with a finished stage agent: make the board and the PR match the stage's done state."""
    if pipeline.is_config():
        return
    done, active = st.get("reconciled") or {}, set()
    try:
        for c in all_cards:
            _card(c, reg, done, active)
    except github.RateLimited as e:
        print(f"reconcile: waiting for the GitHub budget ({e})", file=sys.stderr)
        active.update(a["key"] for a in alerts.listing() if a["key"].startswith(PREFIX))   # a hold resolves nothing
    ids = {c["id"] for c in all_cards}
    st["reconciled"] = {k: v for k, v in done.items() if k in ids}
    alerts.sweep(PREFIX, active, _dispatch().notify)
