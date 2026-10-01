"""The dispatcher: start the right agent for every waiting card."""
import os
import json
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from pl import config as C
from pl import accounts, alerts, board, events, harnesses, manager, memory, move_agent, trackers, usage
from pl.accounts import healthy_profile, mark_exhausted, screen_hit_limit
from pl.agents import hold_reason, pane_exists, permission_wait, registry, run_waiting, worker_status
from pl.board import card, cards, col_name, sections, update
from pl.product import mirror_to_product, pull_new
from pl.trackers import github
from pl.trackers.github import RateLimited, limited
from pl.util import load_state, notify, now_iso, parse_iso, save_state, slug_of, tmux


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
    if col == "Inbox":
        return "spec"
    if col == "Spec ready":
        if (C.GATES or {}).get("spec") and not (c.get("metadata") or {}).get("spec_approved_at"):
            return None   # the spec gate: a person approves the spec first
        return "design" if ("frontend" in (c.get("tags") or []) and not has_design(c)) else "plan"
    if col in ("Approved", "In progress"):
        return "run"
    return None


def approved_label(c, col, reg):
    """Status line for a Spec ready card whose spec you approved: which stage is next and whether an agent is on it."""
    if hold := hold_reason(c):
        return hold
    m = c.get("metadata") or {}
    stage = stage_for(c, col) if col == "Spec ready" and m.get("spec_approved_at") else None
    if stage not in ("design", "plan"):
        return None
    w = m.get("worker") or {}
    live = bool(w) and w.get("stage") == stage and worker_status(w, reg)[0] in ("alive", "starting")
    return f"spec approved \u2014 {'designing' if stage == 'design' else 'planning'} " + ("(agent running)" if live else "(waiting for a planner slot)")


def stage_complete(c, col, stage):
    if stage == "design":
        return has_design(c) or C.COLUMNS.index(col) > C.COLUMNS.index("Spec ready")
    return C.COLUMNS.index(col) >= C.COLUMNS.index(C.STAGE_DONE_AT[stage])



def _gh_env_args():
    """tmux new-window -e so an agent's own pl calls act on this profile and its gh calls use the profile's gh
    sign-in folder (when set)."""
    return (["-e", f"PL_CONFIG_DIR={Path(C.CONFIG_DIR).expanduser().absolute()}"] if C.CONFIG_DIR else []) + \
        (["-e", f"GH_CONFIG_DIR={C.GH_CONFIG_DIR}"] if C.GH_CONFIG_DIR else [])


MAX_ATTEMPTS = 3   # a stage whose agent died this many times waits for a person (pl retry)
LIVE_RUN_CAP = 2   # live run agents (waiting + working) never exceed this x max_runs: each is a ~250 MB process


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


def start_worker(c, stage, attempts, dry):
    h, profile = harnesses.harness_for(stage, c)
    name = f"{stage}-{slug_of(c)[:28]}"      # tmux window name: no colon, so "pipeline:<name>" targets stay unambiguous
    label = f"{stage}:{slug_of(c)[:28]}"     # Claude session display name
    if not C.PROMPTS.get(stage):
        raise SystemExit(f"pl: set [stages.{stage}] prompt in {C.path() or 'config.toml'}")
    prompt = C.PROMPTS[stage].format(id=c["id"])
    review = (C.CODE_HOST.get("labels") or {}).get("review")
    if stage == "run" and C.PROMPTS[stage] == C.BUILTIN_PROMPTS["run"] and review:
        prompt += f" review={shlex.quote(review)}"   # pl-run labels its pull request with it
    if dry:
        print(f"  would start {name} under {profile}: {prompt}")
        return
    if subprocess.run(["tmux", "has-session", "-t", f"={C.TMUX_SESSION}"], capture_output=True).returncode:
        tmux("new-session", "-d", "-s", C.TMUX_SESSION, "-n", "dispatch", "-c", str(C.WORK_DIR))
    win = tmux("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", name, "-c", str(C.WORK_DIR),
               "-e", "DISABLE_AUTO_UPDATE=true", *_gh_env_args(), "-P", "-F", "#{window_id}")
    tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    sid = str(uuid.uuid4())
    pane, script = tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0], launch_path(name)
    # the record first, then the agent: a Ctrl-C after it leaves a record the next pass counts ("starting", then
    # "never started"), never a second agent; a Ctrl-C during it leaves no agent
    update(c["id"], metadata={"worker": {"stage": stage, "session_id": sid, "pane": pane, "window": win,
                                         "tmux_session": C.TMUX_SESSION, "profile": profile, "harness": h.name,
                                         "started_at": now_iso(), "attempts": attempts, "host": C.HOST,
                                         "launch": str(script)}})
    _launch(pane, name, harnesses.launch_script(harnesses.unattended(h), profile, prompt, sid, label), script)
    if C.ATTENTION:
        subprocess.run([str(C.ATTENTION), "register", sid, f"{stage}: {c['title'][:50]}", c["id"], stage], capture_output=True)
    events.emit("started", c["id"], stage=stage, harness=h.name, session=sid)
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
        w = live.get(name)
        rec = next((r for r in reg.values() if w and (r.get("tmux") or "").endswith(w["pane"])), None) if w else None
        sid = (rec or {}).get("sessionId")
        if sid and sid not in (seen := svc_state.setdefault(name, {}).setdefault("sessions", [])):
            seen[:] = (seen + [sid])[-20:]   # the loop registry: which sessions were this loop's, for pl usage
        if pause:
            if w and (not rec or rec.get("status") == "idle"):
                print(f"paused: closing idle {name} loop")
                if not dry:
                    tmux("kill-window", "-t", w["window"], check=False)
            continue
        if hold:
            if not w:
                print(f"{name} loop waits: {hold}")
            continue
        pin = svc["profile"]   # a loop with an explicit account never runs under another one
        prof = pin or svc_state.get(name, {}).get("profile")
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
        if pin and pin in accounts.exhausted_profiles():
            print(f"{name} loop waits: its account {pin} is parked")
            continue
        use = pin or healthy_profile(prof, all_cards)
        if use is None:
            print(f"{name} loop waits: every profile is out of credits")
            continue
        prompt = loop_prompt(svc)
        print(f"starting {name} loop under {use}: {prompt}")
        if dry:
            continue
        if subprocess.run(["tmux", "has-session", "-t", f"={C.TMUX_SESSION}"], capture_output=True).returncode:
            tmux("new-session", "-d", "-s", C.TMUX_SESSION, "-n", "dispatch", "-c", str(C.WORK_DIR))
        win = tmux("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", name, "-c", str(C.WORK_DIR),
                   "-e", "DISABLE_AUTO_UPDATE=true", *_gh_env_args(), "-P", "-F", "#{window_id}")
        tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
        pane = tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]
        _launch(pane, name, harnesses.launch_script(harnesses.unattended(harnesses.account_harness(use)), use, prompt,
                                                    None, name))
        old = svc_state.get(name, {})
        svc_state[name] = {"profile": use, "started_at": now_iso(), "sessions": old.get("sessions", []),
                           **({"context_restarts": old["context_restarts"]} if old.get("context_restarts") else {})}


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


def sweep_untracked(all_cards, reg, dry):
    """Close spec/design/plan agent windows that no card tracks any more (a same-stage restart or a lost
    finished_workers write leaves them behind), once idle for 5 minutes. Run windows are never touched:
    an idle run agent may be waiting at a human gate."""
    tracked = set()
    for c in all_cards:
        m = c.get("metadata") or {}
        for w in ([m["worker"]] if m.get("worker") else []) + (m.get("finished_workers") or []):
            if w.get("window"):
                tracked.add(w["window"])
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{pane_id} #{window_activity} #{window_name}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4:
            continue
        wid, pane, activity, name = parts
        if wid in tracked or not name.startswith(("spec-", "design-", "plan-")):
            continue
        rec = next((r for r in reg.values() if (r.get("tmux") or "").endswith(pane)), None)
        if rec:
            if rec.get("status") != "idle" or time.time() - rec.get("statusUpdatedAt", 0) / 1000 <= 300:
                continue
        elif time.time() - int(activity or 0) <= 600:
            continue
        print(f"closing untracked {name} agent window {wid}")
        if not dry:
            tmux("kill-window", "-t", wid, check=False)


INTAKE_EVERY = 300   # seconds between the default issue intake's searches (one GraphQL search each; the board is read every pass)
_INTAKE = {"at": None, "noted": None}   # noted: the search back-off window already logged as an event


def dispatch_once(max_runs, dry, max_prep=2, pull=True):
    if hit := limited():       # GitHub rate limit: no board work until the reset
        raise hit
    trackers.reset("tracker")  # a fresh tracker each pass; the stage field stays remembered across passes
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
    if not dry:
        last = st.get("last_col") or {}
        now_col = {c["id"]: col_name(c["list_id"]) for c in all_cards}
        for cid, col in now_col.items():
            if cid in last and last[cid] != col:   # first sighting records without emitting
                events.emit("moved", cid, **{"from": last[cid], "to": col})
        st["last_col"] = now_col
    runs_alive = 0
    runs_live = 0   # every live run agent, waiting ones included
    prep_alive = 0
    todo = []
    failed = set()   # stage_failed and permission_wait alert keys seen this pass
    for c in all_cards:
        m = c.get("metadata") or {}
        if m.get("pipeline_mode") != "auto":
            continue
        col = col_name(c["list_id"])
        stage = stage_for(c, col)
        w = m.get("worker") or {}
        try:
            wh = harnesses.get(w.get("harness") or "claude") if w else None
        except SystemExit as e:   # one card's bad harness must not stop the pass
            print(f"{c['id'][:8]}  skipped: {e}", file=sys.stderr)
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
        screen = (move_agent.limit_screen(c, w, wh) if w.get("resume") else screen_hit_limit(w.get("pane"), wh)) if live and not busy else None
        if screen and not dry:   # the whole limit handling runs under the card's move lock: a hand move waits, or wins
            held = move_agent.lock(c["id"])
            busy = not held
        if busy:
            if w.get("stage") == "run":         # it still holds its slot
                runs_live += 1
                runs_alive += 1
            else:
                prep_alive += 1
            continue
        if screen:
            try:
                prof = w.get("profile") or m.get("profile")
                until = mark_exhausted(prof, screen) if not dry else "(dry run)"
                prep = w.get("stage") in ("spec", "design", "plan")   # a prep agent always moves; a run agent only while young
                alt = healthy_profile(None, all_cards, harness=wh.name) if prep else healthy_profile(None, all_cards)
                age = time.time() - parse_iso(w.get("started_at") or "")
                auto_resumes = "continuing automatically" in screen
                moved, why, changed, to = False, None, False, None   # changed: the move left a new worker on the card
                if not prep:   # a run agent of any age first tries to keep its session on another Claude account
                    to = healthy_profile(None, all_cards, harness="claude")
                    if to and to != prof:
                        moved, why, changed = move_agent.move(c, to, "usage limit", dry, held=True)
                switch = not changed and bool(alt) and alt != prof and (prep or age < C.LIMIT_RESTART_WINDOW or not auto_resumes)
                print(f"{c['id'][:8]}  {w.get('stage')} agent under {prof} hit the usage limit; {prof} parked until {until}; "
                      + (f"moved under {to}, same session" if moved else f"not moved: {why}" if changed
                         else f"{f'not moved ({why}); ' if why else ''}restarting under {alt} (agent was {int(age // 60)} min old)" if switch
                         else "left to resume by itself at the reset time" if auto_resumes else "no profile left with credits; waiting"))
                if why := alerts.open(f"account_parked:{prof}", "warn", f"Account {prof} is parked: usage limit",
                                      f"parked until {until[11:16]} UTC; pl accounts --reset {prof} un-parks it"):
                    notify(alerts.headline(why, f"Profile {prof} hit its usage limit"), f"parked until {until[11:16]} UTC; "
                           + (f"moved and resumed its agent under {to}" if moved
                              else f"young agents move to {alt}" if alt else "no other profile has credits"))
                if changed and not dry:
                    m = c["metadata"]
                    w = m.get("worker") or {}
                    status = "starting" if w else "none"   # no worker: its relaunch failed, the next steps start it fresh
                elif switch:
                    if not dry:
                        tmux("kill-window", "-t", w["window"], check=False)
                        meta = {"worker": None, "profile": alt, "profile_switches": (m.get("profile_switches") or [])
                                + [{"at": now_iso(), "from": prof, "to": alt, "stage": w.get("stage"), "reason": "usage limit"}]}
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
        if w and status == "alive" and not busy and not screen and not dry:   # stuck at a permission prompt: tell, never answer
            ask = permission_wait(wh, w.get("pane"))
            if ask:
                key = f"permission_wait:{c['id']}"
                failed.add(key)
                where = f"{w.get('stage')}-{slug_of(c)[:28]}"
                if why := alerts.open(key, "warn", f"agent waiting for permission in {where}: {ask}",
                                      f"answer it in the agent window (pl card {c['id'][:8]}); README: Permissions"):
                    notify(alerts.headline(why, f"Agent waiting for permission: {c['title'][:40]}"), ask)
            if bool(ask) != bool(w.get("permission_wait")):
                w = {**w, "permission_wait": ask} if ask else {k: v for k, v in w.items() if k != "permission_wait"}
                update(c["id"], metadata={"worker": w})
        # a worker for an earlier stage that finished its job: clean its window once it has gone idle
        if w and w.get("stage") != stage and stage_complete(c, col, w["stage"]) and status == "alive":
            rec = reg.get(sid) or {}
            idle_for = time.time() - (rec.get("statusUpdatedAt", 0) / 1000)
            if rec.get("status") == "idle" and idle_for > 300 and w.get("window"):
                print(f"{c['id'][:8]}  closing finished {w['stage']} agent window (idle {int(idle_for // 60)} min)")
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
                    print(f"{c['id'][:8]}  closing finished {fw.get('stage')} agent window")
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
                print(f"{c['id'][:8]}  plan ready for review: {c['title'][:50]}")
                notify(f"Plan ready: {c['title'][:50]}", f"pl review {c['id'][:8]} ; then pl approve or pl reject")
            continue
        if stage is None or hold_reason(c):   # a split or parked card gets no agent and holds no slot
            mirror_to_product(c, col, dry)
            continue
        mirror_to_product(c, col, dry)
        same_stage = w.get("stage") == stage
        if same_stage and status in ("alive", "starting"):
            if stage == "run":
                runs_live += 1
                why = run_waiting(w, reg) if status == "alive" else None
                if why:
                    print(f"{c['id'][:8]}  run agent {why}; its slot is free")   # its window stays: no second agent
                else:
                    runs_alive += 1
            else:
                prep_alive += 1
            continue
        attempts = int(w.get("attempts") or 0) if same_stage else 0
        script = _own_launch(w)
        if same_stage and status == "dead" and script and script.exists() and not dry:   # the script deletes itself when it runs
            script.unlink(missing_ok=True)   # one event per failed launch
            print(f"{c['id'][:8]}  {stage} agent never started in {w.get('window')}: the launch command did not run")
            events.emit("error", c["id"], message=f"agent never started in {w.get('window')}: the launch command did not run")
        if same_stage and status == "dead" and attempts >= MAX_ATTEMPTS:
            key = f"stage_failed:{c['id']}:{stage}"
            failed.add(key)
            if why := alerts.open(key, "high", f"Card {c['id'][:8]}: the {stage} agent died {attempts} times",
                                  f"pl card {c['id'][:8]} shows why; pl retry {c['id'][:8]} starts it fresh"):
                notify(alerts.headline(why, f"Needs you: {c['title'][:40]}"), f"the {stage} agent died {attempts} times; see pl card {c['id'][:8]}")
                print(f"{c['id'][:8]}  {stage} agent failed {attempts} times; not retrying")
            continue
        if attempts == 0:
            st["notified"].pop(f"{c['id']}:{stage}_failed", None)   # a fresh start (pl retry): a new failure notifies again
        todo.append((c, stage, attempts + 1, col))
    sweep_untracked(all_cards, reg, dry)
    memory.guard_runaways(st, all_cards, dry)
    low = memory.check_starts(st) if not dry else None
    ms = None if dry else manager.read_status()   # a status older than 15 s is ignored: a dead manager freezes nothing
    mhold = f"new starts paused by pl manager: {ms['hold']}" if ms and ms.get("hold") else None
    room = ms.get("room") if ms and isinstance(ms.get("room"), int) else None   # new agents the machine has room for
    for line in (low, mhold):
        if line:
            print(line)
    ensure_services(st, all_cards, reg, dry, paused(), low)   # loops are not held by the machine: they are not agents
    # order: runs first (they are the long pole), then plans, designs, specs; oldest first
    rank = {"run": 0, "plan": 1, "design": 2, "spec": 3}
    todo.sort(key=lambda t: (rank[t[1]], t[0].get("updated_at") or ""))
    pause = paused()
    held = 0
    for c, stage, attempts, col in todo:
        if pause and attempts <= 1:   # paused: only restart a stage that already started and crashed
            held += 1
            continue
        if low:   # low memory: no start at all, restarts included
            continue
        if attempts <= 1 and (mhold or room is not None and room <= 0):   # machine limit: new starts only
            continue
        if stage == "run":
            if runs_live >= LIVE_RUN_CAP * max_runs:
                print(f"{c['id'][:8]}  run waits ({runs_live} live, cap {LIVE_RUN_CAP * max_runs})")
                continue
            if runs_alive >= max_runs:
                key = f"{c['id']}:queued"
                if key not in st["notified"]:
                    st["notified"][key] = time.time()
                    notify("Pipeline queue", f"{c['title'][:40]} waits: {runs_alive} runs already in flight (max {max_runs})")
                print(f"{c['id'][:8]}  run waits ({runs_alive}/{max_runs} in flight)")
                continue
            runs_alive += 1
            runs_live += 1
        else:
            if prep_alive >= max_prep:
                print(f"{c['id'][:8]}  {stage} waits ({prep_alive}/{max_prep} spec/plan agents in flight)")
                continue
            prep_alive += 1
        want = (c.get("metadata") or {}).get("profile")
        prof = healthy_profile(want, all_cards)
        if prof is None:
            if why := alerts.open("accounts_all_out", "high", "Every account is out of credits",
                                  "cards wait for the first reset; pl accounts shows when"):
                notify(alerts.headline(why, "Every Claude profile is out of credits"), f"{c['title'][:40]} waits; pl profiles")
            print(f"{c['id'][:8]}  {stage} waits: every profile is out of credits (pl profiles)")
            continue
        if prof != want:
            print(f"{c['id'][:8]}  profile {want} is parked; using {prof}")
            if not dry:
                update(c["id"], metadata={"profile": prof})
            c["metadata"] = {**(c.get("metadata") or {}), "profile": prof}
        print(f"{c['id'][:8]}  {col} -> {stage} agent (attempt {attempts}): {c['title'][:50]}")
        start_worker(c, stage, attempts, dry)
        if attempts <= 1 and room is not None:
            room -= 1
        prev = ((c.get("metadata") or {}).get("worker") or {})
        if prev and prev.get("stage") != stage and prev.get("window") and not dry:
            fin = list((c.get("metadata") or {}).get("finished_workers") or []) + [prev]
            update(c["id"], metadata={"finished_workers": fin})
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
        check_alerts(all_cards, failed)
        alerts.flush_offers()      # at most one line per pass into an idle Assistant
    save_state(st)
    return sorted((c["id"], c.get("list_id"), c.get("updated_at")) for c in all_cards), sorted(reg)


PR_WAIT = 86400   # seconds a card may sit in the PR column before its PR raises an alert


def check_alerts(all_cards, failed):
    """End of a pass: resolve the alerts whose condition cleared, escalate the rest, and run the pass-end checks
    (a dead loop, a PR waiting over PR_WAIT). failed: the stage_failed keys this pass saw."""
    parked = accounts.exhausted_profiles()
    alerts.sweep("account_parked:", {f"account_parked:{p}" for p in parked}, notify)
    out = bool(C.PROFILES) and set(C.PROFILES) <= set(parked)
    alerts.sweep("accounts_all_out", {"accounts_all_out"} if out else set(), notify)
    alerts.sweep("stage_failed:", failed, notify)
    alerts.sweep("permission_wait:", failed, notify)
    waiting, now = set(), time.time()
    for c in all_cards:
        t = parse_iso(c.get("updated_at") or "")
        if (c.get("metadata") or {}).get("pipeline_mode") == "auto" and col_name(c["list_id"]) == C.STAGE_DONE_AT["run"] \
                and t and now - t > PR_WAIT:
            key = f"pr_waiting:{c['id']}"
            waiting.add(key)
            if why := alerts.open(key, "warn", f"Card {c['id'][:8]}: its PR waits over 24 h", "review and merge it, or move the card on"):
                notify(alerts.headline(why, f"PR waiting over 24 h: {c['title'][:40]}"), f"pl card {c['id'][:8]}")
    alerts.sweep("pr_waiting:", waiting, notify)
    if not C.SERVICES:
        return
    try:
        loops = usage.summary(usage.scan())["loops"]
    except Exception:  # noqa: BLE001  (a transcript problem never stops the pass)
        return
    dead = {f"loop_dead:{n}" for n, u in loops.items() if n in C.SERVICES and (u or {}).get("dead")}
    for key in sorted(dead):
        name = key.split(":", 1)[1]
        if why := alerts.open(key, "warn", f"Loop {name} looks dead: no tokens for 3 fires", f"open the Loops tab and restart {name}"):
            notify(alerts.headline(why, f"Loop {name} looks dead"), "no tokens spent for 3 of its fires; see the Loops tab")
    alerts.sweep("loop_dead:", dead, notify)


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
        has = subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True).returncode == 0
        where = ["new-window", "-d", "-t", f"={session}:"] if has else ["new-session", "-d", "-s", session]
        r = subprocess.run(["tmux", *where, "-n", "dispatch", *env, "--", sys.executable, "-m", "pl", "dispatch"],
                           capture_output=True, text=True)
    except OSError as e:
        return f"tmux: {e.strerror or e}"
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
        errs = C.validate(tomlkit.parse(C.path().read_text())) if C.path() else []
        if errs:
            raise SystemExit("; ".join(errs))
    except (SystemExit, Exception) as e:  # noqa: BLE001 - a bad file must never stop the dispatcher
        for k, v in keep.items():
            setattr(C, k, v)
        print(f"warning: settings not reloaded, keeping the previous ones: {e}")
        return
    harnesses._CACHE.clear()
    if C.TMUX_SESSION != keep["TMUX_SESSION"] and C.TMUX_SESSION not in _WARNED_TMUX:
        _WARNED_TMUX.add(C.TMUX_SESSION)
        print(f"warning: tmux_session changed to {C.TMUX_SESSION!r}; this dispatcher keeps {keep['TMUX_SESSION']!r} until restarted")
    for k in ("TMUX_SESSION", "CONFIG_DIR", "PROFILE_NAME", "ATTN", "STATE_DIR", "STATE_FILE", "PAUSE_FILE", "SEEN_FILE",
              "PROFILE_STATE", "LOCK_FILE"):
        setattr(C, k, keep[k])


def cmd_dispatch(a):
    if not a.dry_run and os.environ.get("PL_MANAGED") != "1" and manager.manages(C.PROFILE_NAME):
        print(f"this machine is run by pl manager (pid {manager.holder_pid() or '?'}); it starts this profile's dispatcher")
        return   # a restored tmux window just closes; the manager's own dispatcher keeps running
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
    from pl import profiles
    for w in profiles.shared_warnings(profiles.list_profiles(), profiles.current_row()):
        print(f"warning: {w}")
    first = True
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
        except SystemExit as e:
            print(f"pass failed: {e}", file=sys.stderr)
            if isinstance(e, RateLimited) and (why := alerts.open(
                    "github_rate_limited", "warn", "GitHub rate limit: pl is backing off",
                    "pl waits out the back-off and tries again by itself; nothing to do")):
                notify(alerts.headline(why, "GitHub rate limit"), e.note)
            if not (isinstance(e, RateLimited) and e.until == noted):   # one event per rate-limit window
                events.emit("error", message=str(e))
            noted = e.until if isinstance(e, RateLimited) else None
        if a.once:
            return
        if seen is not None:   # plus pl's writes and the idea files: any of them changing is a change
            seen = (seen, *_marks())
        wait = max(base, min(2 * wait, IDLE_MAX)) if seen is not None and seen == last else base
        last = seen
        if lock:
            lock = _restart_if_new(lock)
        _nap(wait, lambda: _fast_guard(a.dry_run))


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
