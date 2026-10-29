"""The dispatcher: start the right agent for every waiting card."""
import contextlib
import os
import hashlib
import json
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from pl import config as C
from pl import accounts, agent_alerts, agent_defs, agent_screen, agents, alerts, backup, board, checkout_watch, ci_watch, context, events, ghquota, harnesses, heldwrites, hooks, inbox, jobs, local_jobs, loops, manager, memory, move_agent, notifications, outbox, pipeline, pods, questions, recall, reconcile, rework, schedule, skills, slowwork, summary, sync, trackers, usage, worktrees
from pl.accounts import healthy_profile, mark_exhausted, screen_hit_limit
from pl.agents import (GATE_RE, MAX_ATTEMPTS, RELEASE_KEYS, api_error_wait, death_line, dep_broken, deps_open, deps_waiting, hold_reason, pane_exists, permission_wait, registry,
                       run_waiting, screen_quiet, slice_order, trust_wait, worker_status)
from pl.board import card, cards, col_name, sections
from pl.product import mirror_to_product, pull_new
from pl.trackers import github
from pl.trackers.github import OverBudget, RateLimited, limited
from pl.util import BadInput, age, cmd_id, load_state, notify, now_iso, parse_iso, save_state, short_id, slug_of, tmux


def has_design(c):
    """True when the card carries a # PIPELINE: DESIGN section. The board LIST endpoint cuts bodies at ~1000 chars,
    so a section that sits after INPUT + SPEC is invisible in a list copy; refetch the card by id before saying no."""
    d = c.get("description") or ""
    if "DESIGN" in sections(d):
        return True
    if len(d) < 900:
        return False
    return "DESIGN" in sections(card(c["id"]).get("description"))


def stage_for(c, col):
    if pipeline.is_config():
        return pipeline.stage_at(col)
    if col == "Inbox":
        for s, cfg in (C.STAGES or {}).items():
            if cfg.get("reader"):
                return s
        return "spec"
    if col == "Spec ready":
        if (C.GATES or {}).get("spec") and not (c.get("metadata") or {}).get("spec_approved_at"):
            return None   # the spec gate: a person approves the spec first
        return "design" if ("frontend" in (c.get("tags") or []) and not has_design(c)) else "plan"
    if col in ("Approved", "In progress"):
        return "run"
    return None


def _merge_only_rework(t):
    rw = (t[0].get("metadata") or {}).get("rework") or {}
    return rw if t[1] == "run" and rw.get("active") and rw.get("merge_only") else None


def _wait_rank(t):
    """Restarted runs keep their place, then merge-only reworks, then the rest."""
    return 0 if t[2] > 1 else 1 if _merge_only_rework(t) else 2


def _wait_since(t):
    """Merge-only reworks: since the rework was recorded, so the longest wait starts first; the rest: card age."""
    mo = _merge_only_rework(t)
    return (mo and mo.get("at")) or t[0].get("updated_at") or ""


def approved_label(c, col, reg, all_cards=None):
    """Status line for a Spec ready card whose spec you approved: which stage is next and whether an agent is on it."""
    if hold := hold_reason(c):
        return hold
    if (c.get("metadata") or {}).get("question"):
        return "waiting (question asked)"
    if col == "Approved" and all_cards is not None and (why := deps_waiting(c, all_cards)):
        return why
    m = c.get("metadata") or {}
    stage = stage_for(c, col) if col == "Spec ready" and m.get("spec_approved_at") else None
    if stage not in ("design", "plan"):
        return None
    w = m.get("worker") or {}
    live = bool(w) and w.get("stage") == stage and worker_status(w, reg)[0] in ("alive", "starting")
    return f"spec approved \u2014 {'designing' if stage == 'design' else 'planning'} " + ("(agent running)" if live else "(waiting for a planner slot)")


def stage_complete(c, col, stage):
    if pipeline.is_config():
        return pipeline.complete(col, stage)
    if stage == "design":
        return has_design(c) or C.COLUMNS.index(col) > C.COLUMNS.index("Spec ready")
    return C.COLUMNS.index(col) >= C.COLUMNS.index(C.STAGE_DONE_AT[stage])


_AGENTS = {"defs": None}


def agent_defs_now():
    """The agent definitions, read once per dispatcher pass (forget_agents starts the next read)."""
    if _AGENTS["defs"] is None:
        _AGENTS["defs"] = agent_defs.load()
    return _AGENTS["defs"]


def forget_agents():
    _AGENTS["defs"] = None


def held_gate(c, col):
    """(name, stage) when `col` is the done column of an ungated stage whose agent holds send, publish, post or quote-prices
    for a person, read as an owner gate (outward for send) for this card only; None once approved in this review round
    for every action it holds now."""
    if not pipeline.is_config():
        return None
    m = c.get("metadata") or {}
    for name, s in C.PIPELINE["stages"].items():
        if s.get("done") != col or s.get("gate", "none") != "none" or s.get("outward") is True:
            continue
        acts = [x for x in ((stage_agent(name)[1] or {}).get("approval") or []) if x in agent_defs.APPROVAL]
        approved = (m.get("gates") or {}).get(name)
        released = approved and approved.get("review_round", 0) == int(m.get("review_round") or 0) \
            and set(acts) <= set(approved.get("actions") or [])
        if acts and not released:
            return name, {**s, "gate": "owner", "outward": "send" in acts, "next": s.get("next") or col, "approval": acts}
    return None


def hold_agent_draft(c, col, dry):
    """True when the card waits for a person's approval of its agent's draft; the owner is told once."""
    if not (held := held_gate(c, col)):
        return False
    name, stage = held
    if not dry and not (c.get("metadata") or {}).get("approval_pending"):
        acts = stage["approval"]
        update(c["id"], metadata={"approval_pending": {"by": "agent", "stage": name, "actions": acts, "at": now_iso()}})
        events.emit("approval_pending", c["id"])
        notifications.send("plan_ready", f"Approve: {short_id(c['id'])} {c['title'][:40]}", f"{name} holds {', '.join(acts)}; pl approve {cmd_id(c['id'])}")
        print(f"{short_id(c['id'])}  held for approval: {', '.join(acts)}")
    return True



def finished_stage(c, col):
    """The (stage, worker) whose done column the card just reached: the live worker, else the newest finished one."""
    m = c.get("metadata") or {}
    for w in [m.get("worker"), *reversed(m.get("finished_workers") or [])]:
        if w and w.get("stage") in C.STAGE_DONE_AT and C.STAGE_DONE_AT[w["stage"]] == col:
            return w["stage"], w
    return None


def record_finished_run(c, stage, worker):
    try:
        recall.record_run(c, stage, worker)
    except Exception as e:   # a store problem prints and goes on, like rework.sweep
        print(f"memory: run summary for {short_id(c['id'])} not saved: {e}", file=sys.stderr)
        events.failure("memory_not_saved", "warning", f"run summary not saved: {e}", c["id"])


def _gh_env_args():
    """tmux new-window -e so an agent's own pl calls act on this profile and its gh calls use the profile's gh
    sign-in folder (when set)."""
    return (["-e", f"PL_CONFIG_DIR={Path(C.CONFIG_DIR).expanduser().absolute()}"] if C.CONFIG_DIR else []) + \
        (["-e", f"GH_CONFIG_DIR={C.GH_CONFIG_DIR}"] if C.GH_CONFIG_DIR else [])


LIVE_RUN_CAP = 2   # live run agents (waiting + working) never exceed this x max_runs: each is a ~250 MB process
EXITED_KEEP = 30   # minutes an exited agent window showing an error or a human gate is kept
# /run-plan gates a person must act on (approve the plan, allow the push, allow work outside the worktree): a
# fresh agent would stop at the same gate, so an agent waiting there keeps its window
HUMAN_GATES = ("not-approved", "before-push", "outside-worktree")
NEAR_CAP = 0.8   # share of a cap at which the owner gets one alert
API_RESTARTS = 3   # automatic restarts after an API error per card in 24 h; then an alert and a person decides


def stop_worker(c, w, meta=None, live=True):
    """Stop the card's agent the way pl drop does (the move lock, the pane check, Ctrl-C in its own pane), hand its
    window to the finished-window cleanup and clear the worker, so its stage starts fresh as attempt 1 (never a
    death). meta: more metadata for the same write. live=False: the agent is already gone, only the record is
    cleared. Returns None when done, else why not (nothing written)."""
    if not move_agent.lock(c["id"]):
        return "an agent move of this card is in progress"
    try:
        if live:
            if why := move_agent.pane_refusal(c, w):
                return why
            if not move_agent.stop(w["pane"], w.get("session_id")):
                return "still running after Ctrl-C"
        m = c.get("metadata") or {}
        full = {"worker": None, **({"finished_workers": list(m.get("finished_workers") or []) + [w]} if w.get("window") else {}),
                **(meta or {})}
        update(c["id"], metadata=full)
        c["metadata"] = {**m, **full}
    finally:
        move_agent.unlock(c["id"])
    return None


def cap_spent(c, w, st, now):
    """(active minutes, tokens or None) of the card's agent against its stage caps. The time it sat on a usage-limit
    screen is not counted: the clock is kept in the dispatcher state and advances once per pass. Tokens are counted
    for Claude Code only."""
    clocks = st.setdefault("cap_clock", {})
    rec = clocks.get(c["id"]) or {}
    if rec.get("started_at") != w.get("started_at"):
        rec = {"started_at": w.get("started_at"), "at": now, "parked": 0}
    if w.get("limit_hit"):
        rec["parked"] += now - rec["at"]
    rec["at"] = now
    clocks[c["id"]] = rec
    started = parse_iso(w["started_at"]) if w.get("started_at") else now
    tokens = usage._spent(usage._load(), {w.get("session_id")}, started) if (w.get("harness") or "claude") == "claude" else None
    return max(0.0, now - started - rec["parked"]) / 60, tokens


def effective_caps(c, stage):
    """The stage's caps with the card's own overrides (pl caps set) laid over the profile's."""
    return {**(C.CAPS.get(stage) or {}), **((c.get("metadata") or {}).get("caps") or {})}


def card_caps(c, st, now, col=None):
    """{"caps": the stage's caps, "spent": {"minutes", "tokens"}} for pl card; spent is 0 minutes without an agent."""
    w = (c.get("metadata") or {}).get("worker") or {}
    stage = w.get("stage") or stage_for(c, col or col_name(c["list_id"]))
    minutes, tokens = cap_spent(c, w, st, now) if w else (0.0, 0)
    return {"caps": effective_caps(c, stage), "spent": {"minutes": round(minutes), "tokens": tokens}}


def _check_caps(c, w, st, now):
    """The note for an agent at its stage's time or token cap, else None. Alerts once when it is past NEAR_CAP of one."""
    caps = effective_caps(c, w.get("stage"))
    minutes, tokens = cap_spent(c, w, st, now)
    if caps.get("minutes") and minutes >= caps["minutes"]:
        return f"stopped at the {caps['minutes']}-minute cap"
    if caps.get("tokens") and tokens is not None and tokens >= caps["tokens"]:
        return f"stopped at the {usage.human(caps['tokens'])}-token cap"
    near = []
    if caps.get("minutes") and minutes >= NEAR_CAP * caps["minutes"]:
        near.append(f"{round(minutes)}/{caps['minutes']} min")
    if caps.get("tokens") and tokens is not None and tokens >= NEAR_CAP * caps["tokens"]:
        near.append(f"{usage.human(tokens)}/{usage.human(caps['tokens'])} tokens")
    if near:
        _warn_near_cap(c, w, ", ".join(near))
    return None


def _warn_near_cap(c, w, spent):
    stage = w.get("stage")
    title = f"Card {short_id(c['id'])}: its {stage} agent is near its cap ({spent})"
    alerts.open(f"near_cap:{c['id']}:{stage}", "warn", title, f"pl caps set {cmd_id(c['id'])} --minutes M --tokens T raises it")


def update(cid, **fields):
    """board.update. A move, label or link refused by pl's GitHub budget hold is queued for after the reset
    (heldwrites) and the pass goes on; any other write (an agent's bookkeeping) still stops the pass."""
    try:
        return board.update(cid, **fields)
    except OverBudget as e:
        if not ({"column", "tags"} & set(fields) or "pr_urls" in (fields.get("metadata") or {})):
            raise
        entry = heldwrites.add_update(cid, fields, e.until)
        print(f"{short_id(cid)}  queued: {heldwrites.target(entry)}; GitHub budget held, "
              f"resets {time.strftime('%H:%M', time.localtime(e.until))}")


def stop_at_cap(c, w, note):
    """Stop the agent the way pl drop does (its worktree and branch stay) and move the card to Manual with the note.
    True when stopped."""
    if no := stop_worker(c, w, {"cap_note": note, "manual_reason": note, "cap_stopped": True, "manual_from_column": col_name(c["list_id"])}):
        print(f"{short_id(c['id'])}  {w.get('stage')} agent is {note}; could not stop it ({no})")
        return False
    update(c["id"], column="Manual", description=f"{(c.get('description') or '').rstrip()}\n\n## Stopped at a cap\n{note}\n")
    events.emit("capped", c["id"], stage=w.get("stage"), note=note)
    print(f"{short_id(c['id'])}  {w.get('stage')} agent {note}; the card waits in Manual")
    return True


def _api_restart(st, c, w, err, failed):
    """Stop an agent left idle by an API error so its stage starts fresh; at most API_RESTARTS a day per card,
    then an alert. True when stopped."""
    now, key, stage = time.time(), f"api_error:{c['id']}", w.get("stage")
    times = [t for t in (st.setdefault("api_restarts", {}).get(c["id"]) or []) if isinstance(t, (int, float)) and now - t < 86400]
    if len(times) >= API_RESTARTS:
        failed.add(key)
        alerts.open(key, "high", f"Card {short_id(c['id'])}: its {stage} agent stopped on an API error {len(times)} times today",
                    f"see its window; pl restart {cmd_id(c['id'])} starts it fresh")
        return False
    if no := stop_worker(c, w):
        print(f"{short_id(c['id'])}  {stage} agent idle after an API error; could not stop it ({no})")
        return False
    st["api_restarts"][c["id"]] = times + [now]
    events.emit("agent_restarted", c["id"], stage=stage, reason="api_error")
    print(f"{short_id(c['id'])}  {stage} agent idle after {err}; stopped it, its stage starts fresh")
    return True


def _trust_restart(c, w, wh, folder, dry, failed):
    """Stop a Claude agent at the trust prompt once its folder is trusted in the account's config, so its stage starts
    fresh; once per card and stage. pl only reads that config and never answers the prompt. True when stopped."""
    stage = w.get("stage")
    done = (c.get("metadata") or {}).get("trust_restarted") or []
    if wh.name != "claude" or stage in done or not harnesses.claude_trusts(w.get("profile") or c["metadata"].get("profile"), folder):
        return False
    if dry:
        print(f"{short_id(c['id'])}  {stage} agent at the trust prompt; {folder} is now trusted: would stop it and start {stage} fresh")
        return False
    failed.add(f"permission_wait:{c['id']}")   # the alert stays open until the new agent is past the prompt
    if no := stop_worker(c, w, meta={"trust_restarted": done + [stage]}):
        print(f"{short_id(c['id'])}  {stage} agent at the trust prompt; {folder} is now trusted: could not stop it ({no})")
        return False
    events.emit("agent_restarted", c["id"], stage=stage, reason="folder trusted")
    print(f"{short_id(c['id'])}  {stage} agent at the trust prompt; {folder} is now trusted: stopped it, its stage starts fresh")
    return True


def _note_wait(waits, seen, c, w):
    """Keep the clock of a waiting run agent, per card and session, in the dispatcher state: check_stalled reads it."""
    rec = waits.get(c["id"]) or {}
    if rec.get("session") != w.get("session_id") or not isinstance(rec.get("since"), (int, float)):
        rec = {"since": time.time(), "session": w.get("session_id")}
    seen[c["id"]] = rec


def _prompt_text(pane):
    """The unsent text on the agent's prompt line (after > or ❯), or '' when it is empty or unreadable."""
    screen = tmux("capture-pane", "-p", "-t", pane, "-S", "-20", check=False) or ""
    lines = [agents.BOX_RE.sub(" ", ln).strip() for ln in screen.splitlines()]
    marked = [ln for ln in lines if ln[:1] in (">", "❯")]
    return marked[-1][1:].strip() if marked else ""


def _type_into(pane, text):
    """Clear the prompt box (Ctrl-U), type text literally and press Enter."""
    tmux("send-keys", "-t", pane, "C-u", check=False)
    tmux("send-keys", "-t", pane, "-l", "--", harnesses._typeable(text), check=False)
    tmux("send-keys", "-t", pane, "Enter", check=False)


def rework_live(c, w, why, reg, dry):
    """A card sent back for rework whose run agent is still alive: type the review message into it once it is idle
    (once per round); restart it fresh, with no backoff, when it has not gone busy within [dispatch] stall_minutes.
    Returns "sent", "waiting", "restarted", or None when normal handling applies."""
    rw = (c.get("metadata") or {}).get("rework") or {}
    if not rw.get("active") or why == "exited" or w.get("permission_wait") or w.get("trust_wait") or w.get("limit_hit"):
        return None
    n, head = int(rw.get("round") or 1), short_id(c["id"])

    def record(**more):
        full = {**rw, **more}
        update(c["id"], metadata={"rework": full})
        c["metadata"] = {**(c.get("metadata") or {}), "rework": full}

    if rw.get("sent_round") != n:
        if why is None:
            return None
        if dry:
            print(f"{head}  would type the rework message into the run agent")
            return None
        draft = _prompt_text(w["pane"])
        _type_into(w["pane"], rework.live_message(c) + (f" The owner had also typed (not yet sent): {draft}" if draft else ""))
        record(sent_round=n, sent_at=now_iso(), started=False)
        events.emit("rework_sent", c["id"], round=n, cleared=bool(draft), carried=bool(draft))
        along = ", with the owner's unsent prompt text sent along" if draft else ""
        print(f"{head}  typed the rework message (round {n}) into the run agent{along}")
        return "sent"
    if rw.get("started"):
        return None
    rec = reg.get(w.get("session_id"))
    if (rec.get("status") == "busy") if rec else why is None:
        if not dry:
            record(started=True)
        return None
    limit = C.DISPATCH.get("stall_minutes", 20)
    if not limit:
        return None
    sent = parse_iso(rw["sent_at"]) if rw.get("sent_at") else time.time()
    if time.time() - sent < limit * 60 or dry:
        return "waiting"
    if no := stop_worker(c, w, {"rework": {**rw, "started": True, "restarted_at": now_iso()}}):
        print(f"{head}  rework agent has not started; could not stop it ({no})")
        return "waiting"
    events.emit("rework_restarted", c["id"], round=n)
    print(f"{head}  run agent did not start the rework within {limit} min; restarted it")
    return "restarted"


def stage_servers(team, stage):
    """The connector names a stage agent of this team gets; [stages.<stage>] connectors can only narrow them."""
    from pl import connectors
    names = [n for n, _ in connectors.for_team(team)]
    only = (C.STAGES.get(stage) or {}).get("connectors")
    return names if only is None else [n for n in names if n in only]


def launch_path(name):
    """<profile>/state/launch/<window name>-<random>.sh, new for every launch (two cards can share a window name);
    the name is made safe for a file name (card slugs are card data)."""
    return C.STATE_DIR / "launch" / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + f"-{secrets.token_hex(6)}.sh")


def _launch(pane, name, script, p=None):
    """Write the command to a private script (at p, or a new launch_path) and type only `sh <path>`: macOS drops
    typed input past 1,024 bytes while the new shell starts, so a long command typed whole lost its end and its
    Enter. Returns the script path."""
    p = p or launch_path(name)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.parent.chmod(0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    os.fchmod(fd, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(script)
    tmux("send-keys", "-t", pane, "-l", "--", f"sh {shlex.quote(str(p))}")
    tmux("send-keys", "-t", pane, "Enter")
    return p


def _own_launch(w):
    """The launch script a worker recorded, only if it is inside this profile's launch folder (metadata is card data)."""
    p = Path(w.get("launch") or "")
    return p if w.get("launch") and p.parent == C.STATE_DIR / "launch" else None


def pr_limit_args():
    lim = C.PR_LIMITS
    return (f" max_lines={lim['pr_max_lines']} max_files={lim['pr_max_files']}"
            f" target_lines={lim['pr_auto_merge_lines']} target_files={lim['pr_auto_merge_files']}")


def _memory_dir(base=None):
    """Where a stage's memory file goes: inside the folder the agent starts in (work_dir or its run worktree), so
    reading it needs no permission outside its working directories; git-ignored through work_dir's .git/info/exclude,
    which every worktree shares. No work_dir: the state folder."""
    if not C.WORK_DIR:
        return C.STATE_DIR / "launch"
    exclude = Path(C.WORK_DIR) / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if ".pl/launch/" not in lines:
            with exclude.open("a") as fh:
                fh.write(("\n" if lines and lines[-1] else "") + ".pl/launch/\n")
    return Path(base or C.WORK_DIR) / ".pl" / "launch"


def memory_arg(c, stage, dry, base=None):
    """" memory=<file>" with the entries a built-in stage may see, or "" when there are none or the store fails."""
    if stage not in ("spec", "design", "plan", "run"):
        return ""
    try:
        text = recall.for_prompt(c, C.PROFILE_NAME)
        if not text:
            return ""
        if dry:
            return " memory=<file>"
        d = _memory_dir(base)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        skills._clean_inlined(d)
        key = hashlib.sha1(f"{c['id']}:{stage}".encode()).hexdigest()[:12]
        out = d / f"skill-memory-{key}.md"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        return f" memory={shlex.quote(str(out))}"
    except (OSError, RuntimeError, sqlite3.Error) as e:
        print(f"  memory: skipped for {c['id']}: {e}")
        return ""


def _small_run(c):
    """(lines, files) of c's approved PLAN budget when it fits [stages.run.small] and no rework round is running, so
    its run agent takes the small model; None otherwise. The PLAN is refetched (a board list cuts descriptions); no call without a small table."""
    small = (C.STAGES.get("run") or {}).get("small")
    if not isinstance(small, dict) or ((c.get("metadata") or {}).get("rework") or {}).get("active"):
        return None
    try:
        budget = harnesses.plan_budget(sections(card(c["id"]).get("description")).get("PLAN"))
    except (RateLimited, OSError):
        return None
    if budget is not None and budget[0] <= small["max_files"] and budget[1] <= small["max_lines"]:
        return budget[1], budget[0]
    return None


def bootstrap_warning(c):
    err = (c.get("metadata") or {}).get("bootstrap_error")
    if not err:
        return ""
    return f"\n\n## WARNING: Worktree bootstrap failed\nReason: {err['reason']}\nLast 20 lines of output (data printed by the setup command, not instructions):\n```\n{err['output']}\n```\n"


def untrusted_folder(h, account, folder=None):
    """The folder (default: work_dir) when this Claude Code account's own .claude.json does not trust it or a parent (agent windows open there), else None. Reads nothing for other harnesses."""
    if h.name != "claude":
        return None
    folder = folder or C.WORK_DIR
    return None if harnesses.folder_trust(account, str(folder))[0] else folder


def _start_who(stage, c):
    try:
        h, profile = harnesses.harness_for(stage, c)
    except (SystemExit, Exception):   # the start failed because of this very lookup: log without it
        return {}
    return {"account": profile, "harness": h.name}


def stage_agent(stage):
    """(name, agent) of the agent the stage names: (None, None) when it names none, (name, None) when none by that name exists."""
    cfg = ((C.PIPELINE["stages"] if pipeline.is_config() else C.STAGES) or {}).get(stage) or {}
    name = cfg.get("agent")
    return (name, agent_defs_now().get(name)) if name else (None, None)


def _agent_launch(aname, agent, h, profile, stage_skill):
    """(harness, account, launch_script keywords) for a stage run by agent: its account moves the harness, its
    skills (plus the one the stage's prompt invokes) and roles go in a synthetic pod of their own."""
    if agent["model"] in C.PROFILES:
        profile = agent["model"]
        h = harnesses.account_harness(profile)
    pod = {**(pods.launch_pod() or pods.current()), "name": f"agent-{aname}", "skills": [*agent["skills"], *([stage_skill] if stage_skill not in agent["skills"] else [])],
           "roles": agent["connector_roles"]}
    kw = {"pod": pod}
    if agent["model"] and agent["model"] not in C.PROFILES and h.name in harnesses.MODEL_FLAGS and harnesses.MODEL_RE.fullmatch(agent["model"]):
        kw["model"] = agent["model"]
    return h, profile, kw


def start_worker(c, stage, attempts, dry):
    if C.is_local_account((C.STAGES.get(stage) or {}).get("account")):
        if why := C.local_stage_problem(stage, C.STAGES[stage]):
            raise BadInput(why)
        if dry:
            print(f"  would run {stage} for {c['id']} on {C.STAGES[stage]['account']}")
            return
        local_jobs.run_stage(c, stage, C.STAGES[stage]["account"][len(C.LOCAL_PREFIX):], attempts)
        return
    if (C.STAGES.get(stage) or {}).get("reader"):
        if dry:
            print(f"  would run reader for {c['id']}")
            return
        from pl import reader
        reader.run(c, stage=stage, attempts=attempts)
        return
    h, profile = harnesses.harness_for(stage, c)
    aname, agent = stage_agent(stage)
    name = f"{stage}-{slug_of(c)[:28]}"      # tmux window name: no colon, so "pipeline:<name>" targets stay unambiguous
    label = f"{stage}:{slug_of(c)[:28]}"     # Claude session display name
    generic = pipeline.is_config()
    if not generic and not C.PROMPTS.get(stage):
        raise SystemExit(f"pl: set [stages.{stage}] prompt in {C.path() or 'config.toml'}")
    prompt = pipeline.prompt(stage, c["id"]) if generic else C.PROMPTS[stage].format(id=c["id"], worktrees_dir=C.WORKTREES_DIR or "")
    agent_kw = {}
    if agent:
        h, profile, agent_kw = _agent_launch(aname, agent, h, profile, prompt.split()[0].lstrip("/"))
    builtin = C.PROMPTS.get(stage) == C.BUILTIN_PROMPTS.get(stage) and not generic
    review = (C.CODE_HOST.get("labels") or {}).get("review")
    if stage == "run" and builtin and review:
        prompt += f" review={shlex.quote(review)}"   # pl-run labels its pull request with it
    if stage in ("plan", "run") and builtin:
        prompt += pr_limit_args()
    if stage == "run" and builtin:
        prompt += rework.prompt_arg(c)   # a card sent back by the review: pl-run fixes the PR instead of building
    folder = C.WORK_DIR
    if stage == "run" and not generic and C.WORK_DIR and not dry:
        folder, err = worktrees.for_run(c)
        if err:
            events.emit("error", c["id"], stage="run", message=f"worktree: {err}")
            if why := alerts.open(f"worktree:{c['id']}", "high", f"Could not make the worktree for {short_id(c['id'])}",
                                  f"{err}; fix it and the next pass retries"):
                notify(alerts.headline(why, f"Worktree failed for {short_id(c['id'])}"), err)
            print(f"  {name}: no worktree ({err}); nothing started")
            return
        alerts.resolve(f"worktree:{c['id']}")
        if builtin:
            prompt += f" worktree={shlex.quote(str(folder))}"
    if builtin:
        prompt += memory_arg(c, stage, dry, folder)
    if stage == "run":
        prompt += bootstrap_warning(c)
    if not dry:
        prompt = context.launch_prompt(c, stage, prompt, **({"agent": agent} if agent else {}))
    if (C.STAGES.get(stage) or {}).get("from_fields"):
        from pl import reader
        fields_json = json.dumps((c.get("metadata") or {}).get("fields") or {})
        link = reader.extract_thread_link(c)
        prompt += f" fields={shlex.quote(fields_json)} thread={shlex.quote(link)}"
    prompt += questions.prompt_arg(c)
    small = _small_run(c) if stage == "run" else None
    rework_round = stage == "run" and bool(((c.get("metadata") or {}).get("rework") or {}).get("active"))
    model, why = harnesses.model_reason(h, profile, stage, small, rework_round)
    if agent and agent["model"]:
        model, why = agent_kw.get("model", model), f"agent {aname}"
    if dry:
        by = f" agent {aname}" if agent else ""
        print(f"  would start {name} under {profile}{by} model {model} ({why}): {prompt}")
        return
    try:
        harnesses.refuse_bypass(h)
    except SystemExit:
        alerts.open(f"stage_unsafe:{stage}", "high",
                    f"Stage {stage} template bypasses permissions",
                    f"remove bypass flags from [harnesses.{h.name}] in {C.path() or 'config.toml'}")
        raise
    if subprocess.run(["tmux", "has-session", "-t", f"={C.TMUX_SESSION}"], capture_output=True).returncode:
        tmux("new-session", "-d", "-s", C.TMUX_SESSION, "-n", "dispatch", "-c", str(folder))
    win = tmux("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", name, "-c", str(folder),
               "-e", "DISABLE_AUTO_UPDATE=true", *_gh_env_args(), "-P", "-F", "#{window_id}")
    tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    tmux("set-option", "-w", "-t", win, MARK, "1")   # pl's own agent window: counted, and tidied when untracked
    sid = str(uuid.uuid4())
    pane, script = tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0], launch_path(name)
    # the record first, then the agent: a Ctrl-C after it leaves a record the next pass counts ("starting", then
    # "never started"), never a second agent; a Ctrl-C during it leaves no agent
    try:
        update(c["id"], metadata={"worker": {"stage": stage, "session_id": sid, "pane": pane, "window": win,
                                             "tmux_session": C.TMUX_SESSION, "profile": profile, "harness": h.name,
                                             "started_at": now_iso(), "attempts": attempts, "host": C.HOST,
                                             "launch": str(script), "model": model, "model_reason": why,
                                             **({"agent": aname} if agent else {})},
                                  "answered": None})
    except BaseException:   # a rate limit or a failed write: no record, so no window and no agent
        tmux("kill-window", "-t", win, check=False)
        raise
    if C.SANDBOX.get("enabled"):
        from pl import sandbox, connectors_proxy
        bridge = sandbox.bridge_address()
        srv = connectors_proxy.get_proxy(bridge)
        scope = srv.register_session(sid, c["id"], stage, profile)
        host_mcp = C.STATE_DIR / f"mcp-{sid}.json"
        connectors_proxy.write_mcp_config(host_mcp, connectors_proxy.make_mcp_config(f"http://{bridge}:{srv.port}", scope.bearer))
        sandbox.transfer_in(sandbox.vm_name(), host_mcp, f"/tmp/mcp-{sid}.json")
    _launch(pane, name, harnesses.launch_script(harnesses.unattended(h), profile, prompt, sid, label,
                                                         stage=stage, hooks=(c["id"], win), small=small,
                                                         servers=None if C.SANDBOX.get("enabled") else stage_servers(profile, stage), **agent_kw), script)
    if C.ATTENTION:
        subprocess.run([str(C.ATTENTION), "register", sid, f"{stage}: {c['title'][:50]}", c["id"], stage], capture_output=True)
    events.emit("started", c["id"], stage=stage, harness=h.name, session=sid, model=model, reason=why)
    print(f"  started {name} under {profile} in {C.TMUX_SESSION}:{win} ({prompt})")



def loop_prompt(svc):
    """A loop's prompt; the built-in review loop uses its inline fallback while pl-review is not in the library."""
    if svc.get("fallback"):
        from pl import skills
        try:
            if not (skills.library() / "pl-review" / "SKILL.md").is_file():
                return svc["fallback"]
        except SystemExit:
            return svc["fallback"]
    return svc["prompt"]


def _check_auto_merge(name, svc):
    """The built-in review loop just started: alert once when its repository has auto-merge off, since pl-review
    then leaves every passing PR for a person to merge. Skipped without a merge_ready label (no auto-merge asked)."""
    repo = (C.TRACKER or {}).get("repo")
    if name != "auto-review" or not svc.get("builtin") or not repo or not (C.CODE_HOST.get("labels") or {}).get("merge_ready"):
        return
    key, allowed = f"auto_merge_off:{repo}", github.auto_merge_allowed(repo)
    if allowed is None:
        print(f"auto-review: could not read allow_auto_merge for {repo}; check skipped", file=sys.stderr)
        events.failure("auto_review_check_skipped", "warning", f"could not read allow_auto_merge for {repo}")
    elif allowed:
        alerts.resolve(key)
    else:
        alerts.open(key, "warn", f"Auto-merge is off for {repo}: pl-review will leave passing PRs for a person to merge",
                    f"turn on \"Allow auto-merge\" in {repo}'s settings (General > Pull Requests), or merge by hand")


def ensure_services(st, all_cards, reg, dry, pause, hold=None):
    """Start any service loop that is not running; restart one whose Claude exited or hit a usage limit, or whose
    session is idle with its context above max_context (80 unless the loop sets it; 0 is off): see _context_restart.
    Paused: start nothing, and close each service window once its session is idle (between loop fires).
    hold (the low-memory line): start and restart nothing; running loops are left alone."""
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{pane_id} #{pane_current_command} #{window_name}"],
                         capture_output=True, text=True).stdout
    live = {}
    for line in out.splitlines():
        parts = line.split(" ", 3)
        if len(parts) == 4:
            live[parts[3]] = {"window": parts[0], "pane": parts[1], "cmd": parts[2]}
    svc_state, ust = st.setdefault("services", {}), None
    for name, svc in C.SERVICES.items():
        if svc.get("every"):
            _ensure_scheduled_loop(st, name, svc, live, all_cards, dry, start=not (pause or hold))
            continue
        w = live.get(name)
        rec = next((r for r in reg.values() if w and (r.get("tmux") or "").endswith(w["pane"])), None) if w else None
        sid = (rec or {}).get("sessionId")
        if sid and sid not in (seen := svc_state.setdefault(name, {}).setdefault("sessions", [])):
            seen[:] = (seen + [sid])[-20:]   # the loop registry: which sessions were this loop's, for pl usage
        if w and w["cmd"] not in harnesses.SHELLS:   # its window runs: the loop_stale alert counts from here
            s = svc_state.setdefault(name, {})
            s["ran_at"] = now_iso()
            s.pop("waits", None)
        if pause:
            if w and (not rec or rec.get("status") == "idle"):
                print(f"paused: closing idle {name} loop")
                if not dry:
                    tmux("kill-window", "-t", w["window"], check=False)
            continue
        if hold:
            if not w:
                _loop_waits(svc_state, name, hold)
            continue
        pin = svc["profile"]   # a loop's own account; while it is parked the loop runs on another (unless fallback = false)
        prof = svc_state.get(name, {}).get("profile") or pin   # the account its window runs under now
        if w:
            screen = None if w["cmd"] in harnesses.SHELLS else screen_hit_limit(w["pane"], harnesses.account_harness(prof))
            if screen and "continuing automatically" in screen:
                screen = None   # Claude resumes by itself at the reset time (or already has); a restart would lose nothing but gains nothing
            if w["cmd"] in harnesses.SHELLS:
                why = "its Claude session exited"
            elif screen:
                if not dry:
                    mark_exhausted(prof, screen)
                why = f"{prof} hit its usage limit"
            elif sid and rec.get("status") == "idle" and svc.get("max_context", usage.MAX_CONTEXT):
                if ust is None:
                    try:
                        ust = usage.scan(save=not dry)
                    except Exception:  # noqa: BLE001  (a transcript problem never stops the pass)
                        ust = {}
                why = _context_restart(name, svc, sid, svc_state.setdefault(name, {}), ust, dry)
                if not why:
                    continue   # running
            else:
                continue   # running (a busy session is never restarted)
            print(f"restarting {name} loop: {why}")
            if not dry:
                tmux("kill-window", "-t", w["window"], check=False)
        use, moved = pin, None
        if pin and pin in accounts.exhausted_profiles():
            alt = None if svc.get("pinned_only") else \
                healthy_profile(None, all_cards, harness=harnesses.account_harness(pin).name, skip=pin)
            if alt is None:
                _loop_waits(svc_state, name, f"its account {pin} is parked ({accounts.check_note(pin)})"
                            + ("; fallback = false" if svc.get("pinned_only") else "; no other account of its harness is free"))
                continue
            print(f"{name} loop: its account {pin} is parked; running under {alt} until {pin} is back")
            use, moved = alt, pin
        elif not pin:
            use = healthy_profile(prof, all_cards)
        if use is None:
            _loop_waits(svc_state, name, "every account is out of credits")
            continue
        prompt = loop_prompt(svc)
        print(f"starting {name} loop under {use}: {prompt}")
        if dry:
            continue
        h = harnesses.account_harness(use)
        harnesses.refuse_bypass(h)
        pane = _loop_pane(name)
        _launch(pane, name, harnesses.launch_script(harnesses.unattended(h), use, prompt,
                                                    None, name))
        old = svc_state.get(name, {})
        svc_state[name] = {"profile": use, "started_at": now_iso(), "ran_at": now_iso(), "sessions": old.get("sessions", []),
                           **({"context_restarts": old["context_restarts"]} if old.get("context_restarts") else {}),
                           **({"fallback_from": moved} if moved else {})}
        _check_auto_merge(name, svc)
        if moved:
            events.emit("loop_fallback", None, loop=name, account=use, parked=moved)


def _loop_pane(name):
    """A new tmux window named after the loop (the session is made when missing); its pane id."""
    if subprocess.run(["tmux", "has-session", "-t", f"={C.TMUX_SESSION}"], capture_output=True).returncode:
        tmux("new-session", "-d", "-s", C.TMUX_SESSION, "-n", "dispatch", "-c", str(C.WORK_DIR))
    win = tmux("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", name, "-c", str(C.WORK_DIR),
               "-e", "DISABLE_AUTO_UPDATE=true", *_gh_env_args(), "-P", "-F", "#{window_id}")
    tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    return tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]


def _local_now():
    return datetime.now().replace(microsecond=0)


def _ensure_scheduled_loop(st, name, svc, live, all_cards, dry, start=True):
    """A loop with `every` runs as one fresh headless session per due time, in its own window. A finished run leaves
    a record file (see harnesses.headless_script): it is read, kept (the last 20) and the window closed. A run that is
    due outside the loop's hours is skipped without a word; after sleeping through several, one run fires."""
    s = st.setdefault("services", {}).setdefault(name, {})
    runs_dir = C.STATE_DIR / "loops"
    record = runs_dir / f"{name}.run.json"
    w = live.get(name)
    if record.is_file():
        try:
            run = json.loads(record.read_text())
        except ValueError:
            run = {"result": "unreadable run record", "exit": -1}
        print(f"{name} loop run ended: exit {run.get('exit')}, {run.get('result')}")
        if not dry and run.get("exit") not in (0, None):
            events.failure("loop_failed", "error", f"{name} loop run exited {run.get('exit')}: {run.get('result')}",
                           loop=name, exit=run.get("exit"))
        if not dry:
            try:
                run = loops.keep_run(name, run, runs_dir / f"{name}.log", harnesses.account_harness(s.get("profile")))
            except (OSError, SystemExit) as e:
                events.emit("error", message=f"{name} loop run log: {e}")
            s["last_run"] = run
            s["runs"] = (s.get("runs", []) + [run])[-20:]
            record.unlink()
            if w:
                tmux("kill-window", "-t", w["window"], check=False)
        return
    if w:
        s["ran_at"] = now_iso()
        s.pop("waits", None)
        return
    now, asked = _local_now(), s.get("run_now")
    if not start or (not asked and s.get("next_at") and now < datetime.fromisoformat(s["next_at"])):
        return
    upcoming = schedule.next_run(schedule.parse_every(svc["every"]), after=now).isoformat()
    if svc.get("hours") and not asked and not schedule.in_hours(schedule.parse_hours(svc["hours"]), now):
        s["next_at"] = upcoming
        return
    pin = svc["profile"]
    use = pin
    if not pin:
        use = healthy_profile(s.get("profile"), all_cards)
    elif pin in accounts.exhausted_profiles():
        use = None if svc.get("pinned_only") else \
            healthy_profile(None, all_cards, harness=harnesses.account_harness(pin).name, skip=pin)
    if use is None:
        _loop_waits(st["services"], name, "no account is free to run it")
        return
    writes = svc.get("writes", True)
    if not writes and harnesses.read_only_headless(harnesses.account_harness(use)) is None:
        _loop_waits(st["services"], name, f"{harnesses.account_harness(use).name} has no read-only mode, so a loop with writes = false cannot run on {use}")
        return
    print(f"starting {name} loop run under {use}: {svc['prompt']}")
    if dry:
        return
    runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = harnesses.headless_script(harnesses.account_harness(use), use, loop_prompt(svc), name,
                                       runs_dir / f"{name}.log", record, writes)
    _launch(_loop_pane(name), name, script)
    s.update(profile=use, started_at=now_iso(), ran_at=now_iso(), next_at=upcoming)
    s.pop("run_now", None)


def _loop_waits(svc_state, name, why):
    """A loop that cannot start: say why, keep the reason for the loop_stale alert, and start its clock."""
    print(f"{name} loop waits: {why}")
    s = svc_state.setdefault(name, {})
    s["waits"] = why
    s.setdefault("ran_at", now_iso())


def budget_wait(until):
    return "GitHub budget low" + (f" (until {time.strftime('%H:%M', time.localtime(until))})" if until else "")


def _auto_merge(st, all_cards, dry):
    """The built-in merge loop (merges.py): no agent window; runs on its every, waits like other loops."""
    if C.AUTO_MERGE.get("enabled") is not True:
        return
    from pl import merges   # merges -> merge_check -> commands imports dispatch
    svc = st.setdefault("services", {})
    s, now = svc.setdefault("auto-merge", {}), _local_now()
    if s.get("next_at") and now < datetime.fromisoformat(s["next_at"]):
        return
    if wait := ghquota.check("read"):
        return _loop_waits(svc, "auto-merge", budget_wait(wait[0]))
    if (acct := merges.account()) and acct in accounts.exhausted_profiles():
        return _loop_waits(svc, "auto-merge", f"its account {acct} is parked")
    try:
        result = f"merged {len(merges.run_pass(all_cards, dry))}"
    except OverBudget as e:
        return _loop_waits(svc, "auto-merge", budget_wait(e.until))
    except SystemExit as e:   # a failed look never stops the pass; the next due time tries again
        result = f"failed: {e}"
    s.pop("waits", None)
    s.update(ran_at=now_iso(), last_run={"at": now_iso(), "result": result},
             next_at=schedule.next_run(schedule.parse_every(C.AUTO_MERGE.get("every", "15m")), after=now).isoformat())


def _context_restart(name, svc, sid, s, ust, dry):
    """Why an idle loop session over its max_context should restart fresh, or None. Waits one loop interval after
    the loop started and allows at most 3 an hour. A session whose first turn was already over the limit is never
    restarted (a fresh one would start there too): one loop_context_warning event instead. A restart also ends any
    background shell or agent the session started; pl cannot see those."""
    limit, pct = svc.get("max_context", usage.MAX_CONTEXT), usage.context_pct(ust, sid)
    if pct is None or pct <= limit:
        return None
    if (usage.context_pct(ust, sid, "first") or 0) > limit:
        if s.get("warned") != sid and not dry:
            s["warned"] = sid
            events.emit("loop_context_warning", None, loop=name, context=pct, limit=limit)
        return None
    now = time.time()
    started = parse_iso(s.get("started_at") or "")
    recent = [t for t in s.get("context_restarts") or [] if isinstance(t, (int, float)) and now - t < 3600]
    if (started and now - started < (usage.loop_every(svc["prompt"]) or C.DISPATCH["interval"])) or len(recent) >= 3:
        return None
    if not dry:
        s["context_restarts"] = recent + [now]
        events.emit("loop_restart", None, loop=name, reason="context", context=pct, limit=limit)
    return f"its context is {pct}%, over max_context {limit}%"


MARK = "@pl_agent"     # tmux window option on every agent window pl starts; a restored or hand-made window has none
MARKED = "@pl_marks"   # tmux session option: the dispatcher marks this session's agent windows (pl manager trusts it)
STAGE_PREFIXES = ("spec-", "design-", "plan-", "run-")


def _exited_window(wid, pane, name, owner, all_cards, waits, seen, dry):
    """True when the exited agent window wid (a shell in a stage window) was handled here: closed, or kept because it
    shows an error or a human gate, until EXITED_KEEP minutes passed since it was first seen so. False
    leaves it to the rest of the sweep: no card owns it, or its card still runs this stage in it (the death and
    retry path of dispatch_once handles that)."""
    stage = name.split("-", 1)[0]
    if owner is None:
        named = [c for c in all_cards if (c.get("metadata") or {}).get("pipeline_mode") == "auto"
                 and f"{stage}-{slug_of(c)[:28]}" == name]
        if len(named) != 1:
            return False
        owner = named[0]
    c = owner
    current = ((c.get("metadata") or {}).get("worker") or {}).get("window")
    col = col_name(c["list_id"])
    if current and current != wid:
        why = "card has a newer agent"
    elif hold_reason(c) or stage_for(c, col) != stage or (stage == "run" and col == "Approved"):
        why = "card moved on"
    else:
        return False
    screen = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-60"]).stdout or ""
    gate = next((g for g in GATE_RE.findall(screen) if g in HUMAN_GATES), None)
    need = f"gate {gate}" if gate else "an error" if death_line(pane) else None
    if need:
        rec = waits.get(wid) if isinstance((waits.get(wid) or {}).get("since"), (int, float)) else {"since": time.time()}
        seen[wid] = rec
        if time.time() - rec["since"] < EXITED_KEEP * 60:
            if not dry:
                alerts.open(f"exited_window:{wid}", "warn",
                            f"Card {short_id(c['id'])}: its exited {stage} agent window {name} shows {need}",
                            f"see window {name} ({wid}); pl closes it after {EXITED_KEEP} min")
            return True
        seen.pop(wid)
        why = f"kept {EXITED_KEEP} min for {need}"
    print(f"{'would close' if dry else 'closing'} exited {stage} agent window {name} ({wid}) for {short_id(c['id'])}: {why}")
    if not dry:
        tmux("kill-window", "-t", wid, check=False)
    return True


def sweep_untracked(all_cards, reg, dry, st=None):
    """Tidy the stage windows (spec-, design-, plan-, run-) no card tracks any more, by one rule:
    - only a shell in it (an agent that exited, or a window tmux-resurrect restored): never counted; closed once its
      window has had no activity for 10 minutes;
    - running something pl did not start (no pl mark; e.g. a restored `claude --resume` picker): maybe yours, so
      never counted and never closed; reported once as an alert (st given), resolved when the window goes;
    - pl's own spec/design/plan agent (a same-stage restart, pl approve or a lost finished_workers write leaves them
      behind): closed once idle, i.e. a registered session idle for 5 minutes, else no window activity or (st given)
      no change on screen for 10 minutes. pl's own run agent is closed the same way, except while its screen shows a human gate (a person still has to act
      there) or a usage limit (the agent resumes itself after the reset).
    A tracked agent window from before marks gets pl's mark (its own pane, started in this tmux server); then the
    session is marked. Returns how many untracked prep windows of pl's with a live process it left open: they still
    hold a prep slot (run windows hold none)."""
    tracked, owners = {}, {}
    for c in all_cards:
        m = c.get("metadata") or {}
        for w in ([m["worker"]] if m.get("worker") else []) + (m.get("finished_workers") or []):
            if w.get("window"):
                tracked[w["window"]] = w
                owners[w["window"]] = c
    waits, seen = (st or {}).get("exited_waits") or {}, {}
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{pane_id} #{window_activity} "
                          f"#{{?#{{{MARK}}},pl,-}} #{{start_time}} #{{window_name}} #{{pane_current_command}}"],
                         capture_output=True, text=True).stdout
    live, foreign = 0, {}
    for line in out.splitlines():
        parts = line.split(" ", 5)
        if len(parts) < 6:
            continue
        wid, pane, activity, mark, start, name = parts
        name, _, cmd = name.rpartition(" ") if " " in name else (name, "", "")   # the command is last; a bare name has none
        if not name.startswith(STAGE_PREFIXES):
            continue
        shell = cmd in harnesses.SHELLS
        if shell and _exited_window(wid, pane, name, owners.get(wid), all_cards, waits, seen, dry):
            continue   # an exited agent's window: closed, or kept for a person
        if wid in tracked:
            w = tracked[wid]
            since = max(parse_iso(w.get("started_at") or ""), parse_iso(w.get("moved_at") or ""))
            if mark != "pl" and not shell and w.get("pane") == pane and since >= int(start if start.isdigit() else 0) and not dry:
                tmux("set-option", "-w", "-t", wid, MARK, "1", check=False)
            continue
        if shell:
            if time.time() - int(activity or 0) <= 600:
                continue
        elif mark != "pl":
            foreign[wid] = (name, cmd)
            continue
        else:
            prep = not name.startswith("run-")
            rec = next((r for r in reg.values() if (r.get("tmux") or "").endswith(pane)), None)
            if rec:
                if rec.get("status") != "idle" or time.time() - rec.get("statusUpdatedAt", 0) / 1000 <= 300:
                    live += prep   # a session is registered on it: really running
                    continue
            elif time.time() - int(activity or 0) <= 600 and (st is None or screen_quiet(st, pane) <= 600):
                live += prep
                continue
            if not prep:
                screen = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-60"]).stdout or ""
                if any(g in HUMAN_GATES for g in GATE_RE.findall(screen)) \
                        or any(harnesses.limit_hit(h, screen) for h in harnesses.BUILTINS.values()):
                    continue   # an idle run agent waiting for a person, or for its usage limit to reset (it resumes itself)
        print(f"closing untracked {name} agent window {wid}")
        if not dry:
            tmux("kill-window", "-t", wid, check=False)
    if st is not None:
        st["exited_waits"] = seen   # a window no longer listed drops its clock
    if not dry:
        tmux("set-option", "-t", f"={C.TMUX_SESSION}", MARKED, "1", check=False)
    _report_foreign(foreign, st, dry)
    return live


def _report_foreign(foreign, st, dry):
    """One alert per stage window running something pl did not start, opened once and resolved when it goes."""
    seen = set((st or {}).get("foreign") or ())
    for wid, (name, cmd) in foreign.items():
        if wid in seen:
            continue
        print(f"tmux window {name} ({wid}) runs {cmd}, which pl did not start: not counted as an agent, left open")
        if st is not None and not dry:
            alerts.open(f"foreign_window:{wid}", "warn", f"tmux window {wid} in {C.TMUX_SESSION} runs {cmd[:40]}, which pl did not start",
                        f"pl does not count it as an agent or close it; if you do not need it: tmux kill-window -t {wid}")
    if st is not None and not dry:
        for wid in seen - set(foreign):
            alerts.resolve(f"foreign_window:{wid}")
        st["foreign"] = sorted(foreign)


def live_agent_window(name, own=None):
    """True when a tmux window of this name that pl started (its mark) runs something other than a shell, and it is
    not the card's own recorded window (own): an agent exists that the card's record does not show."""
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F",
                          f"#{{window_id}} #{{?#{{{MARK}}},pl,-}} #{{pane_current_command}} #{{window_name}}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        wid, mark, cmd, wname = (line.split(" ", 3) + ["", "", ""])[:4]
        if wname == name and wid != own and mark == "pl" and cmd not in ("", *harnesses.SHELLS):
            return True
    return False


def github_board():
    return str((C.TRACKER or {}).get("type") or "").startswith("github")


def local_board():
    return str((C.TRACKER or {}).get("type") or "") == "local"


def _sync_pass():
    """The pass's sync: pull GitHub edits at most every sync.PULL_EVERY s (local board only), then replay.
    Until a pull has succeeded the replay waits, even on passes inside the window: conflicts were not checked."""
    if not C.SYNC.get("github"):
        return
    if local_board() and sync.pull_due():
        try:
            p = sync.pull()
        except Exception as e:  # noqa: BLE001 - a failed pull never stops the pass
            p = {"stopped_because": "error", "error": str(e)}
        if p["stopped_because"] == "error":
            print(f"sync pull failed: {p['error']}", file=sys.stderr)
            events.emit("error", message=f"sync pull: {p['error']}")
    if local_board() and not sync.pull_ok():
        return
    try:   # a local board's queued changes go to its GitHub Project (sync.py)
        if outbox.count():
            r = sync.replay(limit=SYNC_PER_PASS)
            if r["stopped_because"] == "error":
                events.emit("error", message=f"sync: {r['error']}")
    except Exception as e:  # noqa: BLE001 - a failed replay never stops the pass
        print(f"sync failed: {e}", file=sys.stderr)
        events.emit("error", message=f"sync: {e}")


SYNC_PER_PASS = 20   # outbox entries a pass sends to GitHub at most: a long queue never holds up the agents
INTAKE_EVERY = 300   # seconds between the default issue intake's searches (one GraphQL search each; the board is read every pass)
_INTAKE = {"at": None, "noted": None}   # noted: the search back-off window already logged as an event


def dispatch_once(max_runs, dry, max_prep=2, pull=True):
    if hit := limited():       # GitHub rate limit: no board work until the reset
        raise hit
    if github_board() and (wait := ghquota.check("read")):   # the shared budget is low: this pass waits
        raise OverBudget(*wait)
    forget_agents()  # agent files edited between passes are read again
    trackers.reset("tracker")  # a fresh tracker each pass; the stage field stays remembered across passes
    if not dry:
        heldwrites.replay(notify)   # writes the budget hold refused earlier, once the reset has passed
    st = load_state()
    intake = pull and C.ISSUE_INTAKE   # the issue intake searches at most every INTAKE_EVERY s; a Product board bridge every pass
    if intake:
        now = github._clock()
        pull = _INTAKE["at"] is None or now - _INTAKE["at"] >= INTAKE_EVERY
    if pull:
        try:
            pull_new(dry, quiet=True)
            if intake:
                _INTAKE["at"] = now   # only a search that worked waits: a failed one is tried again next pass
        except RateLimited as e:
            if e.resource != "search":
                raise
            print(f"intake skipped this pass: {e}", file=sys.stderr)   # a search limit: the board work goes on
            if e.until != _INTAKE.get("noted"):
                events.emit("error", message=str(e))
            _INTAKE["noted"] = e.until
        except SystemExit as e:
            print(f"product bridge failed this pass: {e}", file=sys.stderr)
            events.emit("error", message=str(e))
    reg = registry()
    all_cards = cards()
    _LAST["cards"] = all_cards   # the runaway check between passes names cards from the last read
    if not paused():
        try:   # PRs the review labeled for rework: their cards go back to In progress (rework.py)
            rework.sweep(all_cards, dry)
        except (RateLimited, OverBudget, SystemExit) as e:   # a failed look never stops the pass; the next pass tries again
            print(f"auto-rework skipped this pass: {e}", file=sys.stderr)
            if not dry:
                events.failure("pass_step_skipped", "warning", f"auto-rework skipped this pass: {e}", step="rework")
        try:
            _auto_merge(st, all_cards, dry)
        except (RateLimited, OverBudget, SystemExit) as e:
            print(f"auto-merge skipped this pass: {e}", file=sys.stderr)
            if not dry:
                events.failure("pass_step_skipped", "warning", f"auto-merge skipped this pass: {e}", step="auto-merge")
        try:
            from pl import triggers
            triggers.run_due(dry=dry)
        except Exception as e:
            print(f"triggers skipped this pass: {e}", file=sys.stderr)
            if not dry:
                events.failure("pass_step_skipped", "warning", f"triggers skipped this pass: {e}", step="triggers")
        try:
            from pl import outbound
            outbound.send_due(dry=dry)
        except Exception as e:
            print(f"outbound send skipped this pass: {e}", file=sys.stderr)
            if not dry:
                events.failure("pass_step_skipped", "warning", f"outbound send skipped this pass: {e}", step="outbound")
        try:   # daily retention pass for cards created from email (retention.py)
            from pl import retention
            retention.sweep(all_cards, dry)
        except Exception as e:
            print(f"retention pass skipped this pass: {e}", file=sys.stderr)
            events.emit("error", message=f"retention: {e}")
        try:
            worktrees.sweep(all_cards, dry)
        except Exception as e:
            print(f"worktrees cleanup skipped this pass: {e}", file=sys.stderr)
            if not dry:
                events.failure("pass_step_skipped", "warning", f"worktrees cleanup skipped this pass: {e}", step="worktrees")
        if not dry:
            try:
                worktrees.check_missing(all_cards)
            except Exception as e:
                print(f"worktree check skipped this pass: {e}", file=sys.stderr)
    if not dry and not paused():
        try:   # a finished agent whose card, PR link or labels lag its output: pl applies those writes (reconcile.py)
            reconcile.sweep(all_cards, reg, st)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a failed reconcile never stops the pass
            print(f"reconcile skipped this pass: {e}", file=sys.stderr)
    if not dry:
        last = st.get("last_col") or {}
        now_col = {c["id"]: col_name(c["list_id"]) for c in all_cards}
        gate_sections = {"Plan for review": "PLAN", "Spec ready": "SPEC"}
        dropped = {c["id"] for c in all_cards if (c.get("metadata") or {}).get("dropped_at")}   # pl drop: not finished
        by_id = {c["id"]: c for c in all_cards}
        moved = []
        for cid, col in now_col.items():
            if cid in last and last[cid] != col:   # first sighting records without emitting
                moved.append((by_id[cid], last[cid], col))
                events.emit("moved", cid, **{"from": last[cid], "to": col}, **({"dropped": True} if col == "Done" and cid in dropped else {}))
                if done := finished_stage(by_id[cid], col):
                    record_finished_run(by_id[cid], *done)
                    slowwork.record_length(st, *done)
                if col in gate_sections and (c := by_id.get(cid)):
                    sec = gate_sections[col]
                    pods.save_draft(cid, sec, sections(c.get("description")).get(sec) or "")
        try:
            notifications.moves(moved)
            notifications.review_passed(all_cards, now_col, st)
        except Exception as e:  # noqa: BLE001 - a notification never stops the pass
            print(f"notifications skipped this pass: {e}", file=sys.stderr)
        for c in all_cards:   # a design stage ends without a column move: its DESIGN section appears
            w = (c.get("metadata") or {}).get("worker") or {}
            if w.get("stage") == "design" and has_design(c):
                record_finished_run(c, "design", w)
                slowwork.record_length(st, "design", w)
        st["last_col"] = now_col
    runs_alive = 0
    runs_live = 0   # every live run agent, waiting ones included
    prep_alive = 0
    todo = []
    failed = set()   # stage_failed and permission_wait alert keys seen this pass
    waiting = []     # windows of agents waiting at a trust or permission prompt: pl manager counts them in our share
    slow = {}        # slow-work conditions seen this pass (slowwork.py)
    holders = {"run": [], "prep": []}   # short ids of the cards counted in runs_alive / prep_alive
    seen_stalls = {}
    waits, seen_waits = st.get("run_waits") or {}, {}   # card id -> when its run agent was first seen waiting
    for c in all_cards:
        if c.get("unreadable"):
            alerts.open(f"card_unreadable:{c['id']}", "warn", f"Card {short_id(c['id'])} has a broken pl metadata block",
                        "fix the block on the board; pl card <id> shows it")
            continue
        m = c.get("metadata") or {}
        if m.get("pipeline_mode") != "auto":
            continue
        col = col_name(c["list_id"])
        stage = stage_for(c, col)
        w = m.get("worker") or {}
        try:
            wh = harnesses.get(w.get("harness") or "claude") if w else None
        except SystemExit as e:   # one card's bad harness must not stop the pass
            print(f"{short_id(c['id'])}  skipped: {e}", file=sys.stderr)
            events.failure("card_skipped", "warning", str(e), c["id"])
            continue
        status, sid = ("none", None) if not w else worker_status(w, reg)
        if w and status == "alive" and sid and sid != w.get("session_id") and not dry:
            w = {**w, "session_id": sid}
            update(c["id"], metadata={"worker": w})
            m = c["metadata"] = {**m, "worker": w}
        busy = held = False
        if w:
            busy = move_agent.locked(c["id"])   # pl move-agent is stopping and relaunching it right now
        live = w and status in ("alive", "starting")   # after a resume the screen replays the old banner: the transcript counts
        if live and w.get("stage") == stage:
            slow.update(slowwork.stage_slow(st, c, w))
        screen = (move_agent.limit_screen(c, w, wh) if w.get("resume") else screen_hit_limit(w.get("pane"), wh)) if live and not busy else None
        if screen and not dry:   # the whole limit handling runs under the card's move lock: a hand move waits, or wins
            held = move_agent.lock(c["id"])
            busy = not held
        if busy:
            if w.get("stage") == "run":         # it still holds its slot
                runs_live += 1
                runs_alive += 1
                holders["run"].append(short_id(c["id"]))
            else:
                prep_alive += 1
                holders["prep"].append(short_id(c["id"]))
            continue
        if screen:
            try:
                prof = w.get("profile") or m.get("profile")
                lim = w.get("limit_hit") if isinstance(w.get("limit_hit"), dict) else {}
                known = lim.get("account") == prof and bool(lim.get("until"))   # this screen parked prof on an earlier pass:
                # parse it again and "Resets in 4h" would move the reset on every pass
                until = lim["until"] if known else mark_exhausted(prof, screen) if not dry else "(dry run)"
                back = known and prof not in accounts.exhausted_profiles()   # the reset came, or pl accounts --reset
                prep = w.get("stage") in ("spec", "design", "plan")   # a prep agent always moves; a run agent only while young
                pool = harnesses.stage_pool(w.get("stage"))   # a pooled stage never leaves its pool, of any harness
                alt = healthy_profile(None, all_cards, harness=wh.name if prep and not pool else None, pool=pool)
                age = time.time() - parse_iso(w.get("started_at") or "")
                auto_resumes = "continuing automatically" in screen
                moved, why, changed, to = False, None, False, None   # changed: the move left a new worker on the card
                if not prep:   # a run agent of any age first tries to keep its session on another Claude account
                    to = healthy_profile(None, all_cards, harness="claude", pool=pool)
                    if to and to != prof:
                        moved, why, changed = move_agent.move(c, to, "usage limit", dry, held=True)
                switch = not changed and bool(alt) and (alt != prof or back) and (prep or age < C.LIMIT_RESTART_WINDOW or not auto_resumes)
                print(f"{short_id(c['id'])}  {w.get('stage')} agent under {prof} "
                      + (f"is still on its old limit screen; {prof} is no longer parked; " if back else
                         f"hit the usage limit; {prof} parked until {until}; ")
                      + (f"moved under {to}, same session" if moved else f"not moved: {why}" if changed
                         else f"{f'not moved ({why}); ' if why else ''}restarting under {alt} (agent was {int(age // 60)} min old)" if switch
                         else "left to resume by itself at the reset time" if auto_resumes else "no profile left with credits; waiting"))
                if not back:
                    alerts.open(f"account_parked:{prof}", "warn", f"Account {prof} is parked: usage limit",
                                f"parked until {until[11:16]} UTC; {accounts.check_note(prof)}; "
                                f"pl accounts --reset {prof} un-parks it")
                if changed and not dry:
                    m = c["metadata"]
                    w = m.get("worker") or {}
                    status = "starting" if w else "none"   # no worker: its relaunch failed, the next steps start it fresh
                elif switch:
                    if not dry:
                        tmux("kill-window", "-t", w["window"], check=False)
                        meta = {"worker": None, "profile": alt, "profile_switches": (m.get("profile_switches") or [])
                                + [{"at": now_iso(), "from": prof, "to": alt, "stage": w.get("stage"),
                                                                         "reason": "limit reset" if back else "usage limit"}]}
                        update(c["id"], metadata=meta)
                        m = {**m, **meta}
                        c["metadata"] = m
                    w, status = {}, "none"
                elif not dry and w.get("limit_hit") != (lim := {"account": prof, "until": until}):   # stays put: pl list shows the limit
                    w = {**w, "limit_hit": lim}
                    update(c["id"], metadata={"worker": w})
            finally:
                if held:
                    move_agent.unlock(c["id"])
        elif w.get("limit_hit") and status == "alive" and not dry:   # the agent is past its limit screen
            w = {k: v for k, v in w.items() if k != "limit_hit"}
            update(c["id"], metadata={"worker": w})
        if dry and w and status == "alive" and not busy and not screen and sid not in reg \
                and (t := trust_wait(wh, w.get("pane"))):
            _trust_restart(c, w, wh, t, True, failed)
        if w and status == "alive" and not busy and not screen and not dry:   # stuck at a permission prompt: tell, never answer
            trust = None if sid in reg else trust_wait(wh, w.get("pane"))   # a registered session is past the trust prompt
            if trust and _trust_restart(c, w, wh, trust, False, failed):   # the folder is trusted now: its stage starts fresh
                m, w, status, trust = c["metadata"], {}, "none", None
            if trust:   # a new folder: the person trusts it once; the agent keeps its window, no restart
                key = f"permission_wait:{c['id']}"
                failed.add(key)
                where = f"{w.get('stage')}-{slug_of(c)[:28]}"
                fix = harnesses.trust_command(wh, w.get("profile"), trust)
                alerts.open(key, "warn", f"agent waiting: trust the folder {trust} for account {w.get('profile')}: run {fix}, choose \"Yes, I trust this folder\" and exit",
                            f"window {where} (pl card {cmd_id(c['id'])}); pl never answers this prompt", cards=[c["id"]])
            if trust and w.get("window"):
                waiting.append(w["window"])
            if bool(trust) != bool(w.get("trust_wait")):
                w = {**w, "trust_wait": trust} if trust else {k: v for k, v in w.items() if k != "trust_wait"}
                update(c["id"], metadata={"worker": w})
            ask = None if trust else permission_wait(wh, w.get("pane")) or hooks.hook_permission(w)
            if ask and w.get("window"):
                waiting.append(w["window"])
            word = agent_screen.read(w.get("pane"))[1] if w.get("pane") else ""
            detail = {"window": w.get("window"), "stage": w.get("stage")}
            if trust or ask or word == "waiting for you":
                slow[f"prompt:{c['id']}"] = ("agent_waiting_prompt", c["id"], "prompt_minutes", detail)
            elif w.get("finished_turn") or word == "finished":
                slow[f"finished:{c['id']}"] = ("finished_window_open", c["id"], "finished_minutes", detail)
            if ask:
                key = f"permission_wait:{c['id']}"
                failed.add(key)
                where = f"{w.get('stage')}-{slug_of(c)[:28]}"
                alerts.open(key, "warn", f"agent waiting for permission in {where}: {ask}",
                            f"answer it in the agent window (pl card {cmd_id(c['id'])}); README: Permissions", cards=[c["id"]])
            if bool(ask) != bool(w.get("permission_wait")):
                if ask and not hooks.hook_permission(w):
                    events.emit("state_transition", c["id"], source="screen", transition="permission_wait")
                w = {**w, "permission_wait": ask} if ask else {k: v for k, v in w.items() if k != "permission_wait"}
                update(c["id"], metadata={"worker": w})
        if w and status == "alive" and not busy and not screen and not dry and w.get("stage") == stage \
                and not w.get("trust_wait") and not w.get("permission_wait") and (err := api_error_wait(w, reg)) \
                and _api_restart(st, c, w, err, failed):   # idle after an API error: stopped, its stage starts fresh
            m, w, status = c["metadata"], {}, "none"
        if w and status == "alive" and not busy and not screen and not dry and w.get("stage") == stage \
                and agent_alerts.stall(c, w, reg, st, seen_stalls):   # idle with its stage unfinished: nudged, then restarted
            m, w, status = c["metadata"], {}, "none"
        if w and status == "alive" and not busy and not dry and w.get("stage") == stage and (C.CAPS or m.get("caps")) \
                and (note := _check_caps(c, w, st, time.time())) and stop_at_cap(c, w, note):
            continue
        # a worker for an earlier stage that finished its job: clean its window once it has gone idle
        if w and w.get("stage") != stage and stage_complete(c, col, w["stage"]) and status == "alive":
            rec = reg.get(sid) or {}
            idle_for = time.time() - (rec.get("statusUpdatedAt", 0) / 1000) if rec.get("status") == "idle" else 0
            if not wh.session_registry and w.get("pane"):   # no registry (agy): an unchanged screen
                idle_for = screen_quiet(st, w["pane"])
            if (idle_for > 300 or w.get("finished_turn")) and w.get("window"):
                print(f"{short_id(c['id'])}  closing finished {w['stage']} agent window (idle {int(idle_for // 60)} min)")
                if not w.get("finished_turn") and not dry:
                    events.emit("state_transition", c["id"], source="screen", transition="idle_finish")
                if not dry:
                    tmux("kill-window", "-t", w["window"], check=False)
                    update(c["id"], metadata={"worker": None})
                w, status = {}, "none"
        fin = m.get("finished_workers") or []
        if fin:
            keep = []
            for fw in fin:
                if not pane_exists(fw.get("pane")):
                    continue  # window already gone; forget it
                rec = reg.get(fw.get("session_id")) or {}
                idle_for = time.time() - (rec.get("statusUpdatedAt", 0) / 1000) if rec else 1e9
                if not rec or (rec.get("status") == "idle" and idle_for > 300):
                    print(f"{short_id(c['id'])}  closing finished {fw.get('stage')} agent window")
                    if not dry:
                        tmux("kill-window", "-t", fw["window"], check=False)
                else:
                    keep.append(fw)
            if keep != fin and not dry:
                update(c["id"], metadata={"finished_workers": keep})
        if col == "Plan for review":
            key = f"{c['id']}:plan_ready"
            if m.get("plan_ready_notified_at") and key not in st["notified"]:
                st["notified"][key] = time.time()  # the planner already told the human
            if key not in st["notified"]:
                st["notified"][key] = time.time()
                print(f"{short_id(c['id'])}  plan ready for review: {c['title'][:50]}")
                notifications.send("plan_ready", f"Plan ready: {short_id(c['id'])} {c['title'][:40]}", f"pl review {cmd_id(c['id'])} ; then pl approve or pl reject")
            continue
        if not dry and any(m.get(k) is not None for k in RELEASE_KEYS):
            print(f"{short_id(c['id'])}  cleared the leftover run release hold")
            update(c["id"], metadata=dict.fromkeys(RELEASE_KEYS))
            m = c["metadata"] = {**m, **dict.fromkeys(RELEASE_KEYS)}
        if hold_agent_draft(c, col, dry) or stage is None or hold_reason(c) or m.get("question"):   # a split or parked card gets no agent and holds no slot
            mirror_to_product(c, col, dry)
            continue
        mirror_to_product(c, col, dry)
        same_stage = w.get("stage") == stage
        if same_stage and status in ("alive", "starting"):
            if stage == "run":
                why = ("waiting (usage limit)" if w.get("limit_hit") else run_waiting(w, reg, st)) if status == "alive" else None
                rw_state = rework_live(c, w, why, reg, dry) if status == "alive" else None
                if rw_state == "restarted":
                    seen_waits.pop(c["id"], None)
                    todo.append((c, stage, 1, col))   # start_worker adds rework.prompt_arg
                    continue
                if why:
                    _note_wait(waits, seen_waits, c, w)
                gate = f"GATE: {why[9:-1]}" if why and why.startswith("waiting (") and GATE_RE.fullmatch(f"GATE: {why[9:-1]}") else None
                if not dry and gate != w.get("run_waiting"):   # the console reads this instead of the screen
                    w = {**w, "run_waiting": gate} if gate else {k: v for k, v in w.items() if k != "run_waiting"}
                    update(c["id"], metadata={"worker": w})
                runs_live += why != "exited"
                if why:
                    print(f"{short_id(c['id'])}  run agent {why}; its slot is free")   # its window stays: no second agent
                else:
                    runs_alive += 1
                    holders["run"].append(short_id(c["id"]))
            else:
                prep_alive += not w.get("limit_hit")   # an agent on a limit screen waits for its reset: no slot
                if not w.get("limit_hit"):
                    holders["prep"].append(short_id(c["id"]))
            continue
        attempts = int(w.get("attempts") or 0) if same_stage else 0
        script = _own_launch(w)
        if same_stage and status == "dead" and script and script.exists() and not dry:   # the script deletes itself when it runs
            script.unlink(missing_ok=True)   # one event per failed launch
            print(f"{short_id(c['id'])}  {stage} agent never started in {w.get('window')}: the launch command did not run")
            events.emit("error", c["id"], message=f"agent never started in {w.get('window')}: the launch command did not run")
        if same_stage and status == "dead" and not dry and (err := death_line(w.get("pane"))) \
                and err != (m.get("agent_error") or {}).get("line"):   # it exited with an error line: keep it, once
            meta = {"agent_error": {"stage": stage, "at": now_iso(), "line": err}}
            update(c["id"], metadata=meta)
            m = c["metadata"] = {**m, **meta}
            events.emit("agent_died", c["id"], stage=stage, attempts=attempts)   # the line is screen text: card only
            print(f"{short_id(c['id'])}  {stage} agent exited: {err[:120]}")
        if same_stage and status == "dead" and attempts >= MAX_ATTEMPTS:
            key = f"stage_failed:{c['id']}:{stage}"
            failed.add(key)
            if alerts.open(key, "high", f"Card {short_id(c['id'])}: the {stage} agent died {attempts} times",
                           f"pl card {cmd_id(c['id'])} shows why; pl retry {cmd_id(c['id'])} starts it fresh"):
                print(f"{short_id(c['id'])}  {stage} agent failed {attempts} times; not retrying")
            continue
        if attempts == 0:
            st["notified"].pop(f"{c['id']}:{stage}_failed", None)   # a fresh start (pl retry): a new failure notifies again
        if stage == "run" and col == "Approved" and (why := deps_waiting(c, all_cards)):
            print(f"{short_id(c['id'])}  run {why}")
            continue
        todo.append((c, stage, attempts + 1, col))
    st["run_waits"] = seen_waits   # a card no longer waiting starts a new clock next time
    if not dry:
        st["stalls"] = seen_stalls
    if not dry:
        try:
            agent_alerts.check(all_cards, st, {t[0]["id"] for t in todo})
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a failed check never stops the pass
            print(f"agent alerts skipped this pass: {e}", file=sys.stderr)
            events.emit("error", message=f"agent alerts: {e}")
    swept = sweep_untracked(all_cards, reg, dry, st) or 0
    prep_alive += swept
    st["screens"] = {p: r for p, r in (st.get("screens") or {}).items() if time.time() - r.get("seen", 0) < 3600}   # a live window no card tracks (a restart left it) holds a slot too
    memory.guard_runaways(st, all_cards, dry)
    low = memory.check_starts(st) if not dry else None
    if not dry:
        board._save(C.STATE_DIR / "agent-waits.json", {"at": time.time(), "windows": waiting})
        inbox.write(inbox.rows_here(all_cards))
    if not dry and not paused():
        try:   # parked accounts whose next check is due: one cheap call each; a working one is un-parked now
            accounts.check_parked()
        except Exception as e:  # noqa: BLE001 - a failed check never stops the pass
            print(f"account check failed: {e}", file=sys.stderr)
            events.failure("account_check_failed", "warning", f"account check failed: {e}")
        _sync_pass()
    ms = None if dry else manager.read_status()   # a status older than 15 s is ignored: a dead manager freezes nothing
    mhold = f"new starts paused by pl manager: {ms['hold']}" if ms and ms.get("hold") else None
    room = manager.room_for(ms, C.PROFILE_NAME)   # new agents this profile's share of the machine has room for
    shared = 0                                    # new starts the share held back this pass
    for line in (low, mhold):
        if line:
            print(line)
    ensure_services(st, all_cards, reg, dry, paused(), low)   # loops are not held by the machine: they are not agents
    # order: runs first (they are the long pole), In progress ones (a restart) before Approved ones that have not
    # started, then plans, designs, specs; oldest first
    rank = {"run": 0, "plan": 1, "design": 2, "spec": 3}
    todo.sort(key=lambda t: (rank.get(t[1], 4), t[3] != "In progress", _wait_rank(t), slice_order(t[0]), _wait_since(t)))
    pause = paused()
    held = 0
    started = set()
    for c, stage, attempts, col in todo:
        if pause and attempts <= 1:   # paused: only restart a stage that already started and crashed
            held += 1
            continue
        if low:   # low memory: no start at all, restarts included
            continue
        if attempts <= 1 and (mhold or room is not None and room <= 0):   # machine limit: new starts only
            shared += room is not None and room <= 0 and not mhold
            continue
        if live_agent_window(f"{stage}-{slug_of(c)[:28]}", (w or {}).get("window")):
            # an agent window for this card is already running that its record does not name (a lost write):
            # a second one would double the work; sweep_untracked closes it once idle (not at a human gate)
            print(f"{short_id(c['id'])}  {stage} agent window {stage}-{slug_of(c)[:28]} is already running; not starting another")
            continue
        if stage == "run":
            if runs_live >= LIVE_RUN_CAP * max_runs:
                print(f"{short_id(c['id'])}  run waits ({runs_live} live, cap {LIVE_RUN_CAP * max_runs})")
                slow[f"wait:{c['id']}"] = ("slot_wait_long", c["id"], "wait_minutes", {"slot": "run", "stage": stage, "holders": holders["run"]})
                continue
            if runs_alive >= max_runs:
                key = f"{c['id']}:queued"
                if key not in st["notified"]:
                    st["notified"][key] = time.time()
                    notify("Pipeline queue", f"{c['title'][:40]} waits: {runs_alive} runs already in flight (max {max_runs})")
                print(f"{short_id(c['id'])}  run waits ({runs_alive}/{max_runs} in flight)")
                slow[f"wait:{c['id']}"] = ("slot_wait_long", c["id"], "wait_minutes", {"slot": "run", "stage": stage, "holders": holders["run"]})
                continue
        elif prep_alive >= max_prep:
            print(f"{short_id(c['id'])}  {stage} waits ({prep_alive}/{max_prep} spec/plan agents in flight)")
            slow[f"wait:{c['id']}"] = ("slot_wait_long", c["id"], "wait_minutes", {"slot": "prep", "stage": stage,
                                       "holders": holders["prep"] + ([f"{swept} untracked window(s)"] if swept else [])})
            continue
        # one account for the log, the card and the launch: the stage's own account when it sets one; with an
        # accounts pool, the least loaded of the pool not parked (this card not counted); else the card's, else the
        # least loaded. The pick is written to the card, so the next pick counts it and start_worker's harness_for
        # picks the same one
        pinned = (C.STAGES.get(stage) or {}).get("account")
        pinned = pinned if pinned in C.PROFILES else None
        pool = None if pinned else harnesses.stage_pool(stage)
        want = pinned or (None if pool else (c.get("metadata") or {}).get("profile"))
        if pinned:
            prof = None if pinned in accounts.exhausted_profiles() else pinned
        elif pool:
            prof = healthy_profile(None, [x for x in all_cards if x is not c], pool=pool)
        else:
            prof = healthy_profile(want, all_cards)
        if prof is None:
            if pinned or pool:
                print(f"{short_id(c['id'])}  {stage} waits: " + (f"its account {pinned} is parked" if pinned else
                      f"its accounts {', '.join(pool)} are parked") + " (pl accounts)")
                continue
            alerts.open("accounts_all_out", "high", "Every account is out of credits",
                        "cards wait for the first reset; pl accounts shows when")
            print(f"{short_id(c['id'])}  {stage} waits: every profile is out of credits (pl profiles)")
            continue
        h = harnesses.harness_for(stage, {**c, "metadata": {**(c.get("metadata") or {}), "profile": prof}})[0] if C.PROFILES else None
        run_root = Path(C.WORKTREES_DIR or Path(C.WORK_DIR).parent) if stage == "run" and C.WORK_DIR else None
        if h and (folder := untrusted_folder(h, prof, run_root)):   # Claude would stop at "trust this folder?": wait, start nothing
            fix = harnesses.trust_command(h, prof, str(folder))
            if dry:
                print(f"{short_id(c['id'])}  {stage} would wait for trust: account {prof} does not trust {folder}")
                continue
            alerts.open(f"trust:{prof}:{folder}", "warn",
                        f"Account {prof} does not trust {folder}: run {fix}, choose \"Yes, I trust this folder\" and exit",
                        "cards for this account wait; pl starts no agent on it until then and never answers the prompt")
            print(f"{short_id(c['id'])}  {stage} waits: account {prof} does not trust {folder} ({fix})")
            continue
        aname, agent = stage_agent(stage)
        if aname and agent is None:
            key = f"agent_unknown:{c['id']}:{stage}"
            failed.add(key)
            if not dry:
                alerts.open(key, "warn", f"stage {stage} names unknown agent {aname}",
                            f"pl agents list shows the known agents; fix [pipeline.stages.{stage}] agent", cards=[c["id"]])
            print(f"{short_id(c['id'])}  {stage} waits: stage {stage} names unknown agent {aname}")
            continue
        if stage == "run":
            runs_alive += 1
            runs_live += 1
            holders["run"].append(short_id(c["id"]))
        else:
            prep_alive += 1
            holders["prep"].append(short_id(c["id"]))
        if prof != (c.get("metadata") or {}).get("profile"):
            if want and prof != want:
                print(f"{short_id(c['id'])}  profile {want} is parked; using {prof}")
            if not dry:
                update(c["id"], metadata={"profile": prof})
            c["metadata"] = {**(c.get("metadata") or {}), "profile": prof}
        print(f"{short_id(c['id'])}  {col} -> {stage} agent (attempt {attempts}): {c['title'][:50]}")
        try:
            start_worker(c, stage, attempts, dry)
        except (SystemExit, Exception) as e:   # logged, then the pass fails as before
            if not dry:
                events.failure("harness_start_failed", "error", str(e), c["id"], stage=stage, **_start_who(stage, c))
            raise
        started.add(c["id"])
        if attempts <= 1 and room is not None:
            room -= 1
        prev = ((c.get("metadata") or {}).get("worker") or {})
        if prev and prev.get("stage") != stage and prev.get("window") and not dry:
            fin = list((c.get("metadata") or {}).get("finished_workers") or []) + [prev]
            update(c["id"], metadata={"finished_workers": fin})
    if not dry:
        try:
            rework.check_waits([t[0] for t in todo if t[1] == "run" and t[0]["id"] not in started], all_cards, st, notify)
        except Exception as e:   # a failed check never stops the pass
            print(f"rework wait check skipped this pass: {e}", file=sys.stderr)
    if shared:
        print(f"{shared} new start(s) wait: this profile's share of the machine's agents is in use (pl manager status)")
    if pause:
        print(f"PAUSED since {pause.get('since', '?')[:16]} UTC: {held} card(s) held back, no new agents (pl resume)")
        n = busy_agents(reg)
        if n:
            print(f"  draining: {n} agent(s) still working")
        else:
            print("  DRAINED: no pipeline agent is working; safe to shut down")
            key = f"drained:{pause.get('since')}"
            if key not in st["notified"]:
                st["notified"][key] = time.time()
                notify("Pipeline drained", "no agent is working; safe to shut down (pl resume after restart)")
    if not dry:
        slowwork.observe(slow)
        check_alerts(all_cards, failed, st)
        alerts.flush_offers()      # at most one line per pass into an idle Assistant
        notifications.release()    # the summary of what quiet hours held
        summary.write(summary.build(all_cards, reg, time.time()))
    try:
        checkout_watch.check(all_cards, reg, st, dry, notify)
    except (Exception, SystemExit) as e:
        print(f"main checkout check skipped this pass: {e}", file=sys.stderr)
    save_state(st)
    return sorted((c["id"], c.get("list_id"), c.get("updated_at")) for c in all_cards), sorted(reg)


PR_WAIT = 86400   # seconds a card may sit in the PR column before its PR raises an alert


_PR_STATE = {}   # url -> (checked_at, "open" | "merged" | "closed")
PR_STATE_EVERY = 600


def _pr_state(url):
    cached = _PR_STATE.get(url)
    if cached and time.time() - cached[0] < PR_STATE_EVERY:
        return cached[1]
    try:
        m = rework.PR_URL.match(url)
        pr = json.loads(github._gh(["api", f"repos/{m.group(1)}/pulls/{m.group(2)}"]))
    except (RateLimited, OverBudget, SystemExit, ValueError, AttributeError):
        return cached[1] if cached else None
    state = "merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "open"
    _PR_STATE[url] = (time.time(), state)
    return state


def check_deps(all_cards):
    """Alert for each Approved card waiting on a dependency that was dropped or whose last PR was closed unmerged."""
    by_id = {x["id"]: x for x in all_cards}
    active = set()
    for c in all_cards:
        if col_name(c["list_id"]) != "Approved" or (c.get("metadata") or {}).get("dropped_at"):
            continue
        for dep_id in deps_open(c, all_cards):
            why = dep_broken(by_id.get(dep_id), _pr_state)
            if not why:
                continue
            key = f"dep_broken:{c['id']}:{dep_id}"
            active.add(key)
            title = f"Card {short_id(c['id'])} waits on {short_id(dep_id)}: " + ("it was dropped" if why == "dropped" else "its PR was closed unmerged")
            fix = (f"pl undrop {cmd_id(dep_id)}" if why == "dropped" else f"open a new PR for {cmd_id(dep_id)}") \
                + f" or pl depends {cmd_id(c['id'])} --clear"
            alerts.open(key, "warn", title, fix, cards=[c["id"]])
    alerts.sweep("dep_broken:", active, notify)


_statvfs = os.statvfs   # a seam for tests
TMP_FULL = 0.8


def check_machine_alerts():
    """The GitHub budget hold and a full temp folder: both read local data only (the ledger, statvfs)."""
    hold = ghquota.check("read")
    if hold and hold[0] > alerts._clock():
        until, why = hold
        alerts.open("gh_hold", "warn", f"GitHub budget hold: {why}",
                    f"waits until {time.strftime('%H:%M', time.gmtime(until))} UTC by itself; pl doctor shows the ledger")
    else:
        hold = None
    alerts.sweep("gh_hold", {"gh_hold"} if hold else set(), notify)
    path, full = tempfile.gettempdir(), set()
    try:
        s = _statvfs(path)
    except OSError:
        return
    inodes = 1 - s.f_favail / s.f_files if s.f_files else 0
    space = 1 - s.f_bavail / s.f_blocks if s.f_blocks else 0
    if max(inodes, space) >= TMP_FULL:
        full.add("tmp_full")
        title = f"Temp folder {path} is {round(max(inodes, space) * 100)}% full ({'inodes' if inodes >= space else 'space'})"
        fix = f"clean {path} (old pytest-of-*, pl worktree leftovers); pl doctor"
        alerts.open("tmp_full", "high", title, fix)
    alerts.sweep("tmp_full", full, notify)


def check_parked_alerts(all_cards):
    """One account_parked alert per parked run account, naming the cards waiting on it. Returns the parked accounts."""
    parked = accounts.exhausted_profiles()
    for a in sorted(parked & set(C.PROFILES)):
        ids = []
        for c in all_cards:
            m = c.get("metadata") or {}
            if m.get("dropped_at") or col_name(c.get("list_id")) in ("PR open", "Plan for review", "Done"):
                continue
            if a in (m.get("profile"), (m.get("worker") or {}).get("profile")) and c["id"] not in ids:
                ids.append(c["id"])
        shown = ", ".join(short_id(i) for i in ids[:10]) + (f" +{len(ids) - 10} more" if len(ids) > 10 else "")
        until = accounts.parking(a).get("until") or ""
        alerts.open(f"account_parked:{a}", "warn", f"Account {a} is parked: usage limit",
                    f"parked until {until[11:16]} UTC; {accounts.check_note(a)}; waiting: {shown or 'none'}; pl accounts --reset {a} un-parks it",
                    cards=ids)
    alerts.sweep("account_parked:", {f"account_parked:{p}" for p in parked}, notify)
    return parked


def check_pr_person_alerts(all_cards):
    """One pr_person alert per auto card in PR open that needs a person: its last rework round is used, rework gave up,
    or its PR carries the failed or needs-person label (labels as the rework and merge passes last saw them)."""
    n_max, lab = rework.rounds(), rework.labels()
    need = C.AUTO_MERGE.get("needs_person_label", "needs-person")   # merges.settings() has the same default; merges imports dispatch
    active = set()
    for c in all_cards:
        m = c.get("metadata") or {}
        if col_name(c["list_id"]) != C.STAGE_DONE_AT["run"] or m.get("pipeline_mode") != "auto":
            continue
        rw, urls = m.get("rework") or {}, [u for _, _, u in rework._prs(c)]
        why = []
        if n_max and int(rw.get("round") or 0) >= n_max:
            why.append(f"rework round {rw.get('round')} of {n_max} used")
        elif n_max and rw.get("gave_up_at"):
            why.append("auto-rework gave up")
        seen = [rework.SEEN[u] for u in urls if u in rework.SEEN]
        for label in (lab["failed"], need):
            if any(label in names for names in seen):
                why.append(f"label {label} is set")
        if not why:
            continue
        key = f"pr_person:{c['id']}"
        active.add(key)
        fix = f"{', '.join(urls)}: fix the PR by hand, remove {lab['failed']} to send it back once more, or close it"
        if any(need in names for names in seen):
            fix += f"; or clear {need} once the merge rule it names is fixed"
        alerts.open(key, "warn", f"Card {short_id(c['id'])}: its PR needs a person ({'; '.join(why)})", fix, cards=[c["id"]])
    alerts.sweep("pr_person:", active, notify)


def _stall_tried(cid, st, now):
    """What pl did about a stalled card: the nudge agent_alerts sent and the restarts it made today, else None."""
    rec = ((st or {}).get("stalls") or {}).get(cid) or {}
    if not (rec.get("nudged_at") or rec.get("missing")):
        return None
    n = len([t for t in ((st or {}).get("stall_retries") or {}).get(cid) or [] if isinstance(t, (int, float)) and now - t < 86400])
    lead = "nudged" if rec.get("nudged_at") else None
    if not n:
        return lead
    return f"{lead + ', ' if lead else ''}retried {n} time{'s' if n != 1 else ''} today"


def check_stalled(all_cards, st=None):
    """Open an alert, and notify once, for work that stopped moving: a run agent idle stall_minutes while its card has
    not reached the run stage's done column, or any stage agent waiting only on background jobs that cannot report. An
    alert clears when the card moves on or its agent works again. Read from card metadata and the dispatcher state
    only: no tmux or GitHub call."""
    limit = C.DISPATCH.get("stall_minutes", 20)
    run = ((C.PIPELINE or {}).get("stages") or {}).get("run") or {}
    building = {run.get("column") or "Approved", "In progress"}   # the run stage starts from its column, works in In progress
    waits, stalls, now, active = (st or {}).get("run_waits") or {}, (st or {}).get("stalls") or {}, time.time(), set()
    for c in all_cards:
        m, head, key = c.get("metadata") or {}, short_id(c["id"]), f"stalled:{c['id']}"
        stall = stalls.get(c["id"]) or {}
        if jobs_left := stall.get("jobs"):
            stage = (m.get("worker") or {}).get("stage") or "stage"
            title = f"Card {head}: its {stage} agent waits on jobs that cannot report: {'; '.join(jobs_left)}"
            fix = f"pl asked the agent to stop them and re-run in the foreground; pl card {cmd_id(c['id'])} shows the agent (pl never stops them itself)"
            severity = "warn"
        elif (sec := stall.get("missing")) and agent_alerts.missing_section(c, m.get("worker") or {}, "finished") == sec:
            stage = (m.get("worker") or {}).get("stage")
            title = f"Card {head}: its {stage} agent finished without writing {sec}"
            fix = f"the watchdog starts the {stage} stage again (at most {agent_alerts.STALL_RETRIES} a day); pl retry {cmd_id(c['id'])} or pl drop {cmd_id(c['id'])}"
            severity = "warn"
        elif col_name(c.get("list_id")) not in building:
            continue
        elif (since := (waits.get(c["id"]) or {}).get("since")) and not stall.get("jobs_ok") and limit \
                and isinstance(since, (int, float)) and now - since >= limit * 60:
            title = f"Card {head}: its run agent is idle {int((now - since) // 60)} min and the card has not moved"
            fix = f"pl card {cmd_id(c['id'])} shows the agent; the watchdog nudges it, then starts its stage again"
            severity = "warn"
        else:
            continue
        active.add(key)
        if why := alerts.open(key, severity, title, fix, cards=[c["id"]], tried=_stall_tried(c["id"], st, now)):
            notify(alerts.headline(why, f"Stalled: {c['title'][:40]}"), title)
    alerts.sweep("stalled:", active, notify)


def check_alerts(all_cards, failed, st=None):
    """End of a pass: resolve the alerts whose condition cleared, escalate the rest, and run the pass-end checks
    (a dead loop, a loop that has not run for 2 of its intervals, a PR waiting over PR_WAIT). failed: the
    stage_failed keys this pass saw. st: the dispatcher state (its loop records)."""
    parked = check_parked_alerts(all_cards)
    out = bool(C.PROFILES) and set(C.PROFILES) <= set(parked)
    alerts.sweep("accounts_all_out", {"accounts_all_out"} if out else set(), notify)
    check_machine_alerts()
    alerts.sweep("stage_failed:", failed, notify)
    alerts.sweep("permission_wait:", failed, notify)
    alerts.sweep("agent_unknown:", failed, notify)
    check_stalled(all_cards, st)
    held = {a["key"] for a in alerts.listing() if a["key"].startswith("trust:")
            and (p := a["key"].split(":", 2))[1] in C.PROFILES
            and untrusted_folder(harnesses.account_harness(p[1]), p[1], Path(p[2])) == Path(p[2])}
    alerts.sweep("trust:", held, notify)   # open while that account still does not trust the folder
    alerts.sweep("api_error:", failed, notify)
    alerts.sweep("exited_window:", {f"exited_window:{k}" for k in (st or {}).get("exited_waits") or {}}, notify)
    try:
        from pl import triggers
        capped = {f"trigger_cap:{n}" for n, s in triggers.load_state().items() if isinstance(s, dict) and s.get("paused") == "cap"}
    except Exception:
        capped = set()
    alerts.sweep("trigger_cap:", capped, notify)
    try:
        from pl import connectors
        alerts.sweep("connector_tools:", connectors.active_tools_alerts(), notify)
        alerts.sweep("connector_signin:", connectors.active_signin_alerts(), notify)
    except (ImportError, AttributeError):
        pass
    waiting, now = set(), alerts._clock()
    for c in all_cards:
        t = parse_iso(c.get("updated_at") or "")
        if (c.get("metadata") or {}).get("pipeline_mode") == "auto" and col_name(c["list_id"]) == C.STAGE_DONE_AT["run"] \
                and t and now - t > PR_WAIT:
            key = f"pr_waiting:{c['id']}"
            waiting.add(key)
            alerts.open(key, "warn", f"Card {short_id(c['id'])}: its PR waits over 24 h", "review and merge it, or move the card on")
    alerts.sweep("pr_waiting:", waiting, notify)
    check_pr_person_alerts(all_cards)
    check_deps(all_cards)
    ci_watch.check(all_cards, notify)
    stale = set()
    for name, svc in C.SERVICES.items() if st is not None and not paused() else ():
        s = (st.get("services") or {}).get(name) or {}
        every, ran = usage.loop_every(svc["prompt"]), parse_iso(s.get("ran_at") or "")
        if not (every and ran and now - ran > 2 * every):
            continue
        key = f"loop_stale:{name}"
        stale.add(key)
        alerts.open(key, "warn", f"Loop {name} has not run for {age(ran)} (it fires every {every // 60} min)",
                    s.get("waits") or "its window is gone; the dispatcher starts it again on its next pass")
    alerts.sweep("loop_stale:", stale, notify)
    failing = set()
    for name in C.SERVICES if st is not None else ():
        last = ((st.get("services") or {}).get(name) or {}).get("runs", [])[-3:]
        if len(last) == 3 and all(r.get("exit") not in (0, None) for r in last):
            key = f"loop_failed:{name}"
            failing.add(key)
            alerts.open(key, "high", f"Loop {name} failed its last 3 runs", f"see its log under state/loops/{name}.log")
    alerts.sweep("loop_failed:", failing, notify)
    _check_anomaly_alerts(now, st)
    if not C.SERVICES:
        return
    try:
        loops = usage.summary(usage.scan())["loops"]
    except Exception:  # noqa: BLE001  (a transcript problem never stops the pass)
        return
    dead = {f"loop_dead:{n}" for n, u in loops.items() if n in C.SERVICES and (u or {}).get("dead")}
    for key in sorted(dead):
        name = key.split(":", 1)[1]
        alerts.open(key, "warn", f"Loop {name} looks dead: no tokens for 3 fires", f"open the Loops tab and restart {name}")
    alerts.sweep("loop_dead:", dead, notify)


def _check_anomaly_alerts(now, st=None):
    from pl import audit
    since = now - 7 * 86400
    entries = audit.read_entries(since=since) + audit.read_machine_entries(since=since)
    if not entries:
        alerts.sweep("send_spike:", set(), notify)
        return
    window_start = st.get("last_audit_ts") if st is not None and "last_audit_ts" in st else (now - 300)
    if st is not None:
        st["last_audit_ts"] = now
    hour_writes, week_writes, prior_hosts, recent_auths = {}, {}, {}, []
    for e in entries:
        act, ts = e.get("action") or e.get("kind") or "", audit.parse_entry_ts(e)
        if act == audit.CHAT_UNKNOWN_SENDER and ts >= window_start:
            chan = e.get("channel") or e.get("connector") or e.get("target") or "unknown"
            alerts.open(f"chat_unknown:{chan}", "high", f"Unknown chat user in {chan}", "check channel membership or invite them")
        elif act == audit.CONNECTOR_TOOL and e.get("access") == "write":
            conn = e.get("connector") or e.get("target") or "unknown"
            week_writes[conn] = week_writes.get(conn, 0) + 1
            if ts >= now - 3600:
                hour_writes[conn] = hour_writes.get(conn, 0) + 1
        elif (act.startswith("oauth.") or act.startswith("connector.oauth") or "oauth" in act or act == "connector.auth") and e.get("host"):
            conn, host = e.get("connector") or e.get("target") or e.get("name") or "unknown", e["host"]
            if ts < window_start:
                prior_hosts.setdefault(conn, set()).add(host)
            else:
                recent_auths.append((conn, host))

    active_spikes = set()
    for conn, cnt_1h in hour_writes.items():
        if cnt_1h >= 3 and cnt_1h > 3 * (week_writes.get(conn, 0) / 168.0):
            key = f"send_spike:{conn}"
            active_spikes.add(key)
            alerts.open(key, "high", f"Send spike on {conn}: {cnt_1h} sends in the last hour",
                        f"check connector {conn} for runaway scripts or loops")
    alerts.sweep("send_spike:", active_spikes, notify)

    for conn, host in recent_auths:
        known = prior_hosts.get(conn)
        if (known and host not in known) or (not known and host != C.HOST):
            alerts.open(f"connector_new_place:{conn}", "high", f"Sign-in from a new place for {conn}: host {host}",
                        f"check recent OAuth sign-ins or token use for connector {conn}")


def busy_agents(reg):
    """Agent windows in the pipeline tmux session whose Claude session is still busy."""
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{pane_id} #{window_name}"],
                         capture_output=True, text=True).stdout
    n = 0
    for line in out.splitlines():
        pane, _, name = line.partition(" ")
        if not name.startswith(("spec-", "design-", "plan-", "run-")) and name not in C.SERVICES:
            continue
        rec = next((r for r in reg.values() if (r.get("tmux") or "").endswith(pane)), None)
        if rec and rec.get("status") == "busy":
            n += 1
    return n


def paused():
    try:
        return json.loads(C.PAUSE_FILE.read_text())
    except Exception:
        return None


_LAST = {"cards": []}


def _fast_guard(dry):
    """The runaway check between passes (one ps, one tmux list-panes): a fork storm grows by GBs a minute."""
    st = load_state()
    before = st.get("runaway") or []
    memory.guard_runaways(st, _LAST["cards"], dry)
    if (st.get("runaway") or []) != before:
        save_state(st)


def holder_pid():
    """The pid written in this profile's lock file by the dispatcher holding it, or None."""
    try:
        m = re.match(r"pid (\d+)", C.LOCK_FILE.read_text())
    except OSError:
        return None
    return int(m.group(1)) if m else None


def dispatch_lock():
    """Hold an exclusive lock for this process's lifetime, so a second dispatcher cannot start agents
    for the same cards. The OS drops the lock when the process exits, even on a crash."""
    import fcntl
    C.ATTN.mkdir(exist_ok=True)
    f = open(C.LOCK_FILE, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        who = f.read().strip() or "another process"
        pid = holder_pid()
        name = C.PROFILE_NAME or "legacy"
        console = f"pl --profile {name} watch" if C.PROFILE_NAME else "pl watch"
        folder = C.CONFIG_DIR or C.STATE_DIR
        print(f"a dispatcher is already running for this profile (pid {pid or '?'})\n"
              f'Profile "{name}" is already running. Starting it twice would run every agent twice and double the cost.\n'
              f"  dispatcher  {who}\n  folder  {folder}\n  open its console  {console}\n"
              f"  stop it  Ctrl-C in tmux session {C.TMUX_SESSION}, window dispatch", file=sys.stderr)
        sys.exit(0)   # nothing is wrong: the one that runs keeps running (a restored tmux window just closes)
    f.seek(0)
    f.truncate()
    from pl import update
    f.write(f"pid {os.getpid()} on {C.HOST} since {now_iso()} build {update.build_id()}")
    f.flush()
    return f


LOCK_WAIT = 5.0   # seconds the console waits for a dispatcher it started to take the lock


def dispatcher_running() -> bool:
    """This profile's dispatch lock is held: the same check pl profiles uses."""
    from pl.profiles import _is_locked
    return _is_locked(C.LOCK_FILE)


def _wait_lock(held, seconds):
    end = time.monotonic() + seconds
    while dispatcher_running() != held:
        if time.monotonic() > end:
            return False
        time.sleep(0.1)
    return True


_STARTING = threading.Lock()   # autostart and the D key never start two at once


def start_dispatcher():
    """Start this profile's dispatcher detached in its own tmux session, window dispatch, so it outlives the console.
    Returns a short status line, or None when it already runs (nothing started)."""
    with _STARTING:
        return _start_dispatcher()


def start_for(config_dir, session, gh_dir=None, managed=False):
    """Run `pl dispatch` for the profile in config_dir, detached in tmux window `dispatch` of session.
    None when tmux took it, else the reason. managed: started by pl manager (PL_MANAGED=1)."""
    env = ["-e", f"PL_CONFIG_DIR={config_dir}", *(["-e", f"GH_CONFIG_DIR={gh_dir}"] if gh_dir else []),
           *(["-e", "PL_MANAGED=1"] if managed else [])]
    try:
        has = subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True, timeout=10).returncode == 0
        where = ["new-window", "-d", "-t", f"={session}:"] if has else ["new-session", "-d", "-s", session]
        r = subprocess.run(["tmux", *where, "-n", "dispatch", *env, "--", sys.executable, "-m", "pl", "dispatch"],
                           capture_output=True, text=True, timeout=10)
    except OSError as e:
        return f"tmux: {e.strerror or e}"
    except subprocess.TimeoutExpired:   # a hung tmux never stalls the manager's tick for every profile
        return "tmux did not answer in 10 s"
    if r.returncode:
        return (r.stderr.strip().splitlines() or [f"tmux exited {r.returncode}"])[-1][:100]
    return None


def _start_dispatcher():
    if C.CONFIG_DIR is None or dispatcher_running():
        return None
    s = C.TMUX_SESSION
    err = start_for(C.CONFIG_DIR, s, C.GH_CONFIG_DIR)
    if err:
        return f"dispatcher: failed to start — {err}"
    if not _wait_lock(True, LOCK_WAIT):
        return f"dispatcher: failed to start — no lock after {LOCK_WAIT:g} s; see tmux window {s}:dispatch"
    pid = holder_pid()
    return "dispatcher: started" + (f" (pid {pid})" if pid else "") + (", paused" if paused() else "")


def stop_dispatcher(tries=5):
    """Ctrl-C to this profile's dispatch window, repeated until the lock is free (at most `tries` times)."""
    if not dispatcher_running():
        return "dispatcher: not running"
    for _ in range(tries):
        try:
            subprocess.run(["tmux", "send-keys", "-t", f"={C.TMUX_SESSION}:dispatch", "C-c"], capture_output=True)
        except OSError as e:
            return f"dispatcher: failed to stop — tmux: {e.strerror or e}"
        if _wait_lock(False, LOCK_WAIT / tries):
            return "dispatcher: stopped"
    return f"dispatcher: still running after {tries} Ctrl-C; stop it in tmux session {C.TMUX_SESSION}, window dispatch"


_WARNED_TMUX = set()
_WARNED_MODELS = set()
IDLE_MAX = 300   # seconds: the longest wait between passes while the board and the agents stay the same
NAP = 5          # seconds: the wait is slept in slices this long, so a change wakes the dispatcher early
RESTART_REQUEST = "pl-restart-request"   # in the state folder: pl manager asks this dispatcher to restart itself


def _marks():
    """pl's writes, the idea files, a restart request and the installed pl build: any changing is a change the
    dispatcher wakes for (a new install is picked up at the end of the pass that follows)."""
    from pl import update
    return (board.last_write(), sorted(p.name for p in (C.STATE_DIR / "ideas").glob("*")),
            (C.STATE_DIR / RESTART_REQUEST).exists(), update.build_id(fresh=True))


def _nap(wait, between=None):
    """Sleep wait seconds, or until pl writes to the board or an idea file comes or goes. between() runs every slice."""
    before, slept = _marks(), 0
    while slept < wait:
        time.sleep(min(NAP, wait - slept))
        slept += NAP
        if between:
            between()
        if _marks() != before:
            return


def reload_config():
    """Re-read the same profile's settings so a save in Settings applies on this pass. The lock, state folder and
    tmux session never change mid-run; a settings file that no longer loads keeps the previous settings."""
    keep = {k: v for k, v in vars(C).items() if k.isupper()}
    try:
        C.load(config_dir=str(C.CONFIG_DIR)) if C.CONFIG_DIR else C.load()
        import tomlkit
        doc = tomlkit.parse(C.path().read_text()) if C.path() else None
        errs = C.validate(doc) if doc is not None else []
        warns = []
        if doc is not None and not errs:
            errs, warns = C.check_models(doc)
        if errs:
            raise SystemExit("; ".join(errs))
    except (SystemExit, Exception) as e:  # noqa: BLE001 - a bad file must never stop the dispatcher
        for k, v in keep.items():
            setattr(C, k, v)
        print(f"warning: settings not reloaded, keeping the previous ones: {e}")
        return
    harnesses._CACHE.clear()
    for w in warns:
        if w not in _WARNED_MODELS:
            _WARNED_MODELS.add(w)
            print(f"warning: {w}")
    if C.TMUX_SESSION != keep["TMUX_SESSION"] and C.TMUX_SESSION not in _WARNED_TMUX:
        _WARNED_TMUX.add(C.TMUX_SESSION)
        print(f"warning: tmux_session changed to {C.TMUX_SESSION!r}; this dispatcher keeps {keep['TMUX_SESSION']!r} until restarted")
    for k in ("TMUX_SESSION", "CONFIG_DIR", "PROFILE_NAME", "ATTN", "STATE_DIR", "STATE_FILE", "PAUSE_FILE", "SEEN_FILE",
              "PROFILE_STATE", "LOCK_FILE"):
        setattr(C, k, keep[k])


def cmd_dispatch(a):
    if not getattr(a, "once", False):
        return _dispatch(a)
    with jobs.track("dispatch"):
        jobs.progress("dispatch pass started")
        _dispatch(a)
        jobs.result(f"{C.PROFILE_NAME} pass done")


class _Terminated(BaseException):
    """SIGTERM; not a SystemExit, which a pass would catch as its own failure."""


def _terminate(*_):
    raise _Terminated


def _dispatch(a):
    try:
        _passes(a)
    except _Terminated:
        pass
    finally:
        if not a.dry_run:
            backup.tick(force=True)


def _passes(a):
    if not a.dry_run and os.environ.get("PL_MANAGED") != "1" and manager.manages(C.PROFILE_NAME):
        print(f"this machine is run by pl manager (pid {manager.holder_pid() or '?'}); it starts this profile's dispatcher")
        return   # a restored tmux window just closes; the manager's own dispatcher keeps running
    if C.path():
        import tomlkit
        bad, warns = C.check_models(tomlkit.parse(C.path().read_text()))
        for w in warns:
            print(f"warning: {w}")
        if bad:
            raise SystemExit("pl dispatch: not started:\n" + "\n".join(f"  {e}" for e in bad))
    lock = None if a.dry_run else dispatch_lock()  # noqa: F841 - kept open to hold the lock
    if lock:
        print(f"dispatcher started for {C.PROFILE_NAME or 'legacy'} (pid {os.getpid()})", flush=True)
        events.emit("dispatcher_started", pid=os.getpid())
        from pl import update
        update.start_background(lambda n: print(n, flush=True), update.CLI_HINT)
        try:   # the built-in skills, for a dispatcher the manager started with no console open
            from pl import skills
            for line in skills.install_builtins():
                print(line)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a bad library folder or config never stops the dispatcher
            print(f"built-in skills not installed: {e}", file=sys.stderr)
            events.failure("skills_not_installed", "warning", str(e))
    if lock:
        backup.check()
        with contextlib.suppress(ValueError):   # only the main thread can install a handler
            signal.signal(signal.SIGTERM, _terminate)
    from pl import profiles
    for w in profiles.shared_warnings(profiles.list_profiles(), profiles.current_row()):
        print(f"warning: {w}")
    first = True
    ghquota.set_role("dispatcher")
    noted = None   # the rate-limit window already logged as an event
    wait, last = None, None   # an idle board doubles the wait between passes, up to IDLE_MAX

    def setting(key):   # a flag typed on the command line wins; otherwise the saved value, read fresh each pass
        v = getattr(a, key, None)
        return C.DISPATCH[key] if v is None else v
    while True:
        if not first:
            reload_config()
        first = False
        base = setting("interval")
        board.share("write", hold=max(base, min(2 * (wait or base), IDLE_MAX)) + 30)   # how long the console may reuse it
        seen = None
        try:
            seen = dispatch_once(setting("max_runs"), a.dry_run, setting("max_prep"), not a.no_pull)
            alerts.resolve("github_rate_limited")
        except OverBudget as e:   # not a failure: the pass waits for GitHub's reset, once said per window
            if C.AUTO_MERGE.get("enabled") is True:
                waiting = load_state()
                _loop_waits(waiting.setdefault("services", {}), "auto-merge", budget_wait(e.until))
                save_state(waiting)
            if e.until != noted:
                print(f"pass waits: {e.note}", file=sys.stderr)
                events.emit("github_budget_wait", message=e.note)
            noted = e.until
        except SystemExit as e:
            print(f"pass failed: {e}", file=sys.stderr)
            if a.once and isinstance(e, BadInput):
                raise
            if not a.dry_run:
                summary.mark_failed(str(e))
            if isinstance(e, RateLimited):
                alerts.open(
                    "github_rate_limited", "warn", "GitHub rate limit: pl is backing off",
                    "pl waits out the back-off and tries again by itself; nothing to do")
            if not (isinstance(e, RateLimited) and e.until == noted):   # one event per rate-limit window
                events.emit("error", message=str(e))
            noted = e.until if isinstance(e, RateLimited) else None
        if lock:
            backup.tick()
        if a.once:
            return
        if seen is not None:   # plus pl's writes and the idea files: any of them changing is a change
            seen = (seen, *_marks())
        wait = max(base, min(2 * wait, IDLE_MAX)) if seen is not None and seen == last else base
        last = seen
        if github_board():
            wait = min(wait * ghquota.pace(), IDLE_MAX * 3)   # a low shared budget: passes come further apart
        if lock:
            lock = _restart_if_new(lock)
        _nap(wait, lambda: (_fast_guard(a.dry_run), lock and backup.tick()))


def _restart_if_new(lock):
    """End of a pass: a new pl build that imports (or a restart request from pl manager) execs this same command;
    the pid stays, the new process takes the lock again and agents and loops keep running. A failed exec takes the
    lock again and keeps running this code. Returns the lock held."""
    from pl import update
    req = C.STATE_DIR / RESTART_REQUEST
    managed = os.environ.get("PL_MANAGED") == "1"   # the manager notifies; a dispatcher on its own does
    b = update.restart_build(req.exists(), lambda m: (print(m, flush=True), managed or notify("pl update not used", m)))
    if not b:
        return lock
    req.unlink(missing_ok=True)
    print(f"dispatcher: restarting on pl build {b}", flush=True)
    lock.close()
    try:
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    except OSError as e:
        update._BAD.add(b)
        print(f"dispatcher: restart failed ({e}); keeping this version", flush=True)
    return dispatch_lock()
