"""pl watch: the data behind the console, the plain-text frame, and PR activity from GitHub."""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from pl import config as C
from pl.accounts import exhausted_profiles, profile_state
from pl.agents import hold_reason, pane_tail, registry, worker_status, worker_view
from pl.board import cards, col_name, sections
from pl.commands import plan_path
from pl.dispatch import MAX_ATTEMPTS, approved_label, busy_agents, paused, stage_for
from pl.trackers import github
from pl.util import age, card_url, cmd_id, parse_iso, short_id


def watch_snapshot():
    """Everything one frame of pl watch needs: rows per column, profile state, dispatcher state. One board call."""
    wins = dict(l.split(" ", 1) for l in subprocess.run(
        ["tmux", "list-windows", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{window_name}"],
        capture_output=True, text=True).stdout.splitlines() if " " in l)
    reg = registry()
    pipeline = cards()
    by_col = {}
    for c in pipeline:
        by_col.setdefault(col_name(c["list_id"]), []).append(c)
    rows = []  # dicts: kind, text, card, worker, window name (loop rows: card None, "loop" = service name)
    panes, activity = {}, {}
    for l in subprocess.run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{pane_id} #{window_activity}"],
                            capture_output=True, text=True).stdout.splitlines():
        parts = l.split(" ")
        if len(parts) == 3:
            panes[parts[0]], activity[parts[0]] = parts[1], parts[2]
    by_name = {n: wid for wid, n in wins.items()}
    n_on = sum(n in by_name for n in C.SERVICES)
    rows.append({"kind": "loops_head", "text": f"LOOPS   {n_on} of {len(C.SERVICES)} running" + ("" if n_on == len(C.SERVICES) else "   (the dispatcher restarts stopped ones)"), "card": None})
    for name, svc in C.SERVICES.items():
        wid = by_name.get(name)
        pane = panes.get(wid) if wid else None
        rec = next((r for r in reg.values() if pane and (r.get("tmux") or "").endswith(pane)), None)
        state = (rec.get("status") or "on") if (wid and rec) else ("on" if wid else "off")
        dot = {"busy": "●", "off": "✕"}.get(state, "○")
        seen = f"active {age(int(activity.get(wid) or 0))} ago" if wid and activity.get(wid) else ""
        every = svc["prompt"].split()[1] if svc["prompt"].startswith("/loop ") else ""
        rows.append({"kind": {"busy": "loop_busy", "off": "loop_off"}.get(state, "loop_idle"), "card": None, "loop": name, "col": "Loops",
                     "worker": {"pane": pane, "window": wid} if wid else {}, "win": name if wid else "",
                     "text": f" {dot} {name:<15} {('working' if state == 'busy' else state):<8} every {every:<5} {seen}"})
    rows.append({"kind": "blank", "text": "", "card": None})
    try:
        prc = pr_counts()
    except (subprocess.TimeoutExpired, OSError):
        prc = _pr_cache["counts"]
    if prc:
        rows.append({"kind": "loops_head", "card": None,
                     "text": f"PULL REQUESTS   {prc['decide']} need your decision · {prc['merge']} to merge · {prc['rework']} need rework · {prc['gate']} awaiting merge check"})
        for grp, dot, kind in (("decide", "?", "review"), ("merge", "✓", "loop_busy"), ("rework", "!", "loop_off"), ("gate", "…", "loop_idle")):
            for pr in [x for x in prc["prs"] if x["state"] == grp]:
                rows.append({"kind": kind, "card": None, "pr": pr, "col": "PRs", "worker": {}, "win": "",
                             "text": f" {dot} {pr['repo'] + '#' + str(pr['number']):<16} {pr['title']}"})
        rows.append({"kind": "blank", "text": "", "card": None})
    cols = {}                                       # column -> card count
    running = {"spec": 0, "design": 0, "plan": 0, "run": 0}
    per_prof = {p: {"agents": 0, "queued": 0} for p in C.PROFILES}
    needs = {"review": 0, "attention": 0, "failed": 0, "manual": 0}
    for col in C.COLUMNS:
        if col == "Done":
            continue
        cs_ = sorted(by_col.get(col, []), key=lambda c: c.get("updated_at") or "", reverse=True)
        cols[col] = len(cs_)
        rows.append({"kind": "col", "text": f"{col}  ({len(cs_)})", "card": None})
        for c in cs_:
            m = c.get("metadata") or {}
            w = m.get("worker") or {}
            rec = reg.get(w.get("session_id")) if w else None
            win = wins.get(w.get("window"), "") if w else ""
            since = age(parse_iso(w.get("started_at") or "")) if w else ""
            auto = m.get("pipeline_mode") == "auto"
            profile = m.get("profile") or ("-" if auto else "")
            status = worker_status(w, reg)[0] if w else "none"
            failed = bool(w) and status == "dead" and int(w.get("attempts") or 0) >= MAX_ATTEMPTS   # pl retry / t
            view = worker_view(c, reg)
            if hold_reason(c):
                kind = "row"   # split or parked: nothing for a person to do on this card
            elif rec and rec.get("status") == "needs-attention":
                kind = "needs"
                needs["attention"] += 1
            elif col == "Plan for review" and auto:
                kind = "review"
                needs["review"] += 1
            elif failed:
                kind = "needs"
                needs["failed"] += 1
            elif rec and rec.get("status") == "working":
                kind = "working"
            else:
                kind = "row"
            if not auto:
                needs["manual"] += 1
            cur = stage_for(c, col) if auto else None
            if w and status in ("alive", "starting") and w.get("stage") == cur:
                running[cur] = running.get(cur, 0) + 1
                if w.get("profile") in per_prof:
                    per_prof[w["profile"]]["agents"] += 1
            elif auto and cur and kind != "review":
                if profile in per_prof:
                    per_prof[profile]["queued"] += 1
            pc = short_id(m.get("product_card") or "")
            rows.append({"kind": kind, "card": c, "worker": w, "win": win, "col": col, "profile": profile, "failed": failed,
                         "approved": approved_label(c, col, reg) or (view if view.startswith(("run agent waiting", "limit hit")) else None),
                         "text": f"{short_id(c['id'])}  {c['title'][:46]:<46}  {profile:<6} {view:<26} {since:>4}  {win:<34} {('P:' + pc) if pc else ''}"})
    bad = exhausted_profiles()
    stp = profile_state()
    prof = "  ".join(f"{p}: " + (f"PARKED until {parked_until(stp[p].get('until'))}" if p in bad else "ok")
                     + f" ({per_prof[p]['agents']} running, {per_prof[p]['queued']} queued)" for p in C.PROFILES)
    disp = subprocess.run(["tmux", "list-panes", "-t", f"{C.TMUX_SESSION}:dispatch", "-F", "#{pane_current_command}"],
                          capture_output=True, text=True).stdout.strip()
    n_auto = sum(1 for r in rows if r["card"] and (r["card"].get("metadata") or {}).get("pipeline_mode") == "auto")
    pause = paused()
    drain = (f"draining, {busy_agents(reg)} working" if busy_agents(reg) else "DRAINED, safe to shut down") if pause else ""
    try:
        pc = pr_counts()
    except (subprocess.TimeoutExpired, OSError):
        pc = _pr_cache["counts"]
    n_manual = cols.get("Manual", 0)
    prs = (f"{n_manual} manual tasks, " if n_manual else "") + (((f"{pc['decide']} PRs need your decision, " if pc.get("decide") else "") + f"{pc['merge']} PRs to merge," + (f" {pc['rework']} need rework," if pc["rework"] else "")) if pc else "PRs ?,")
    summary = ((f"PAUSED ({drain}; pl resume)   |   ") if pause else "") + (f"NEEDS YOU: {needs['review']} plans to review, {prs} {needs['attention']} agents waiting on you, {needs['failed']} failed"
               f"   |   RUNNING: " + (", ".join(f"{n} {k}" for k, n in running.items() if n) or "nothing"))
    summary2 = (f"CARDS: {n_auto} in the funnel, {needs['manual']} manual   |   " + " · ".join(f"{k} {v}" for k, v in cols.items())
                + "   |   LOOPS: " + " · ".join(f"{n} {'on' if n in wins.values() else 'off'}" for n in C.SERVICES)
                + (f"   |   PRs awaiting merge check: {pc['gate']}" if pc else ""))
    return {"rows": rows, "prof": prof, "parked": bool(bad), "at": datetime.now().strftime("%H:%M:%S"), "summary": summary, "summary2": summary2,
            "needs": needs, "disp": "running" if disp.startswith("python") else f"NOT RUNNING ({disp or 'no window'})"}


def parked_until(iso, now=None):
    """A park's end in UTC: "21:01 UTC" today, "Oct 4 21:01 UTC" on another day, "?" when it does not read."""
    try:
        t = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return "?"
    same = t.date() == (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return t.strftime("%H:%M UTC") if same else f"{t:%b} {t.day} {t:%H:%M} UTC"


def detail_for(r, full_card, full_pr):
    """(title, lines, live) for the detail pane. live=True: show the row's tmux screen instead of lines."""
    if r is None:
        return "dispatcher", [], True
    if r.get("loop"):
        return (f"{r['loop']} loop" if r["worker"] else f"{r['loop']} loop is off; showing the dispatcher"), [], True
    if r.get("pr"):
        pr = r["pr"]
        what = {"decide": "needs your decision (auto-review stopped on purpose)", "merge": "ready to merge",
                "rework": "merge check found problems", "gate": "waiting for the merge check"}[pr["state"]]
        head = [pr["title"], "", f"{pr['repo']}#{pr['number']}   {what}   updated {(pr.get('updatedAt') or '')[:16].replace('T', ' ')} UTC",
                pr["url"], "", "Enter/o or c opens it in the browser.", ""]
        d = full_pr(pr["repo"], pr["number"])
        if d is None:
            return f"PR {pr['repo']}#{pr['number']}", head + ["loading details…"], False
        checks = [(c.get("conclusion") or c.get("state") or "?").lower() for c in d.get("statusCheckRollup") or []]
        head.append(f"branch: {d.get('headRefName')}   mergeable: {d.get('mergeable')}   checks: "
                    + (", ".join(f"{n} {k}" for k, n in sorted({k: checks.count(k) for k in checks}.items())) or "none reported"))
        if pr["state"] == "decide":
            why = [c["body"] for c in d.get("comments") or [] if "auto-review-iter:" in (c.get("body") or "")]
            head += ["", f"── why auto-review stopped (decide, then add the {_labels().get('review') or 'review'} label back) ──"] + (
                re.sub(r"<!--.*?-->", "", why[-1]).strip().splitlines() if why else ["(no auto-review comment found)"])
        else:
            gate = [c["body"] for c in d.get("comments") or [] if "merge-gate:" in (c.get("body") or "")]
            head += ["", "── merge-gate verdict ──"] + (re.sub(r"<!--.*?-->", "", gate[-1]).strip().splitlines() if gate else ["(none yet)"])
        head += ["", "── description ──"] + (d.get("body") or "(empty)").splitlines()
        return f"PR {pr['repo']}#{pr['number']}", head, False
    c = r["card"]
    m = c.get("metadata") or {}
    if r.get("win") and r.get("col") != "Plan for review":   # the agent's tmux window is open: show its screen
        return f"{short_id(c['id'])} agent: {r.get('win') or 'starting'}", [], True
    if r.get("col") == "Plan for review":
        path = plan_path(c)
        if path.exists():
            return f"plan {short_id(c['id'])}: {path.name}", path.read_text().splitlines(), False
        full = full_card(c["id"])
        if full is None:
            return f"plan {short_id(c['id'])}", ["loading the plan from the board…"], False
        plan = sections(full.get("description")).get("PLAN")
        return f"plan {short_id(c['id'])}", (plan or "this card has no PLAN section yet").splitlines(), False
    full = full_card(c["id"]) or c
    secs = sections(full.get("description"))
    lines = [c["title"], "", f"column: {r.get('col')}   profile: {m.get('profile') or '-'}   tags: {', '.join(c.get('tags') or [])}",
             f"card: {card_url(c['id'])}"]
    if m.get("product_url"):
        lines.append(f"product card: {m['product_url']}")
    if m.get("pr_urls"):
        lines += [f"PR: {u}" for u in m["pr_urls"]]
    listed = ", ".join(f"{k} ({len(v)} chars)" for k, v in secs.items())
    lines += ["", "sections: " + (listed or ("(loading…)" if full is c else "none"))]
    first = secs.get("SPEC") or secs.get("INPUT")
    if first:
        lines += ["", "── " + ("SPEC" if secs.get("SPEC") else "INPUT") + " ──"] + first.splitlines()
    return f"card {short_id(c['id'])}", lines, False


_pr_cache = {"at": 0, "counts": None}
PR_TTL = 300   # PR searches are reused this long, across the profile's processes (4 searches cost 4 of 30 a minute)
ACTIVITY_TTL = 1800   # PR activity is 14 days of history: reused for 30 minutes
_FORCED = {}   # PR search name -> the r press (board.fresh_next) it already asked GitHub again for


def _forced(name):
    """r in the console (board.fresh_next) skips the PR caches: True once per press for each PR search."""
    from pl import board
    until = board._SHARE.get("fresh_until", 0)
    if board._fresh() and _FORCED.get(name) != until:
        _FORCED[name] = until
        return True
    return False


def _pr_saved(name, ttl=PR_TTL):
    """(saved at, value) another process of this profile saved for a PR search while it is young, else (0, None)."""
    from pl import board
    got = board._load(C.STATE_DIR / f"{name}.json") if C.STATE_DIR else {}
    at = got.get("at")
    return (at, got.get("value")) if isinstance(at, (int, float)) and 0 <= github._clock() - at < ttl else (0, None)


def _pr_save(name, value):
    from pl import board
    if C.STATE_DIR and value is not None:
        board._save(C.STATE_DIR / f"{name}.json", {"at": github._clock(), "value": value})


def _labels():
    return (C.CODE_HOST or {}).get("labels") or {}


def _owner_args():
    """--owner from [code_host] owner; unset searches every repo you can see."""
    owner = (C.CODE_HOST or {}).get("owner")
    return ["--owner", owner] if owner else []


def pr_counts():
    """Open PRs assigned to you that finished auto-review, by what they need next. Cached PR_TTL s, shared via the state folder (GitHub search is rate-limited).
    None when [code_host] labels has no `ready` label: pl then does not track PRs."""
    lab = _labels()
    if not lab.get("ready"):
        return None
    if not _forced("pr-counts"):
        if github._clock() - _pr_cache["at"] < PR_TTL and _pr_cache["counts"] is not None:
            return _pr_cache["counts"]
        at, saved = _pr_saved("pr-counts")
        if saved is not None:
            _pr_cache.update(at=at, counts=saved)
            return saved
    if github.limited("search"):
        return _pr_cache["counts"]
    search = ["gh", "search", "prs", *_owner_args(), "--state", "open", "--label"]
    tail = ["--assignee", "@me", "--limit", "300", "--json", "labels,number,repository,title,url,updatedAt"]
    r = subprocess.run([*search, lab["ready"], *tail], capture_output=True, text=True, timeout=30, env=C.gh_env())
    f = subprocess.run([*search, lab["failed"], *tail], capture_output=True, text=True, timeout=30, env=C.gh_env()) if lab.get("failed") else None
    if any(x is not None and x.returncode and github.back_off(x.stderr, resource="search") for x in (r, f)):
        return _pr_cache["counts"]
    failed = json.loads(f.stdout) if f and f.returncode == 0 and f.stdout.strip() else []
    counts = None
    if r.returncode == 0:
        try:
            prs = []
            seen = set()
            for pr in failed:   # stopped on purpose by auto-review: a human decision is needed
                pr["state"], pr["repo"] = "decide", (pr.get("repository") or {}).get("name", "?")
                seen.add(pr["url"])
                prs.append(pr)
            for pr in [x for x in json.loads(r.stdout) if x["url"] not in seen]:
                ls = [l["name"] for l in pr.get("labels") or []]
                pr["state"] = "rework" if lab.get("rework") in ls else ("merge" if lab.get("merge_ready") in ls else "gate")
                pr["repo"] = (pr.get("repository") or {}).get("name", "?")
                prs.append(pr)
            prs.sort(key=lambda x: x.get("updatedAt") or "", reverse=True)
            counts = {k: sum(1 for x in prs if x["state"] == k) for k in ("decide", "merge", "rework", "gate")}
            counts["prs"] = prs
        except (ValueError, TypeError, KeyError):
            counts = None
    _pr_cache.update(at=github._clock(), counts=counts if counts is not None else _pr_cache["counts"])
    _pr_save("pr-counts", counts)
    return _pr_cache["counts"]


def row_key(r):
    """Stable selection key for a pl watch row: the card id, or loop:<name> for a service loop."""
    if r.get("card"):
        return r["card"]["id"]
    if r.get("pr"):
        return f"pr:{r['pr']['repo']}#{r['pr']['number']}"
    return f"loop:{r.get('loop')}"


def render_watch(snap):
    """The plain-text frame (pl watch --plain / --once)."""
    out = [f"pl watch  {snap['at']}   dispatcher: {snap['disp']}   profiles: {snap['prof']}", snap["summary"], snap["summary2"], ""]
    needs_you = []
    for r in snap["rows"]:
        if r["card"] is None:
            out.append(r["text"])
            continue
        flag = {"needs": "!! ", "review": ">> "}.get(r["kind"], "   ")
        if r["kind"] == "review":
            needs_you.append(r["card"])
        out.append(flag + r["text"])
    out.append("")
    if needs_you:
        out.append("NEEDS YOU: " + "   ".join(f"pl review {cmd_id(c['id'])}" for c in needs_you))
    out.append("dispatcher, last lines:")
    out += ["  " + l[:150] for l in pane_tail(None, 6)]
    out.append("")
    out.append(f"agent screens: tmux attach -t {C.TMUX_SESSION}   then Ctrl-b w to pick a window   |   !! = agent needs you   >> = plan waits for your review   |   pl watch (no --plain) is the TUI")
    return "\n".join(out)


def cmd_watch(a):
    if a.once:
        print(render_watch(watch_snapshot()))
        return
    if a.plain or not sys.stdout.isatty():
        from pl.tui.app import default_interval
        while True:
            try:
                frame = render_watch(watch_snapshot())
            except SystemExit as e:
                frame = f"pl watch: board read failed this pass: {e}"
            sys.stdout.write("\033[2J\033[H" + frame + "\n")
            sys.stdout.flush()
            try:
                time.sleep(a.interval or default_interval())
            except KeyboardInterrupt:
                return
    from pl.tui.app import PlApp
    PlApp(interval=a.interval).run()




def _gh(args):
    """gh with argv only and a 30 s timeout; the parsed JSON list. Raises OSError on a gh error or while rate limited."""
    if hit := github.limited("search"):
        raise OSError(str(hit))
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, env=C.gh_env())
    if r.returncode:
        raise OSError(str(github.back_off(r.stderr, resource="search") or r.stderr.strip()[:200]))
    return json.loads(r.stdout or "[]")


def pr_activity(days=14):
    """PR open and merge times (ISO strings) for the last `days` days, for the Dashboard. None when gh fails.
    Cached ACTIVITY_TTL s and shared through the state folder; r in the console asks again."""
    if not _forced("pr-activity") and (saved := _pr_saved("pr-activity", ACTIVITY_TTL)[1]) is not None:
        return saved
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    base = ["search", "prs", *_owner_args(), "--assignee", "@me", "--limit", "300"]
    try:
        opened = _gh(base + ["--created", f">={since}", "--json", "createdAt"])
        merged = _gh(base + ["--merged-at", f">={since}", "--json", "closedAt"])
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    got = {"opened": [p["createdAt"] for p in opened if p.get("createdAt")],
           "merged": [p["closedAt"] for p in merged if p.get("closedAt")]}
    _pr_save("pr-activity", got)
    return got
