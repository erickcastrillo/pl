"""The pipeline steward: finds cards stuck past their column's wait and gives each one reason."""
import hashlib
import json
import re
import time
from collections import defaultdict

from pl import accounts, agent_screen, agents, alerts, board, events, inbox, usage
from pl import config as C
from pl.agent_alerts import CARD_RE
from pl.util import load_state, parse_iso, save_state, short_id

THRESHOLDS = {"Spec": 60, "Design": 60, "Plan": 60, "Approved": 30, "In progress": 120, "PR open": 240,
              "Spec ready": 1440, "Plan for review": 1440}
DEFAULT_MINUTES = 120
EXCLUDED = ("Inbox", "Done")
SIZE_RE = re.compile(r"max_lines|max_files|over the size limit")
WAITING_ALERTS = ("agent_waiting", "permission_wait", "pr_person")
BLOCKED_ALERTS = ("agent_blocked_dep", "dep_broken")


def _minutes(text, default):
    """Minutes in a "30m", "4h" or "1d" setting, else default."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", str(text))
    return int(m[1]) * {"m": 1, "h": 60, "d": 1440}[m[2]] if m else default


def due(st, now):
    if not C.STEWARD["enabled"]:
        return False
    ran = (st.get("steward") or {}).get("ran_at")
    return ran is None or now - ran >= _minutes(C.STEWARD["every"], 30) * 60


def threshold(col):
    custom = (C.STEWARD.get("thresholds") or {}).get(col)
    return (_minutes(custom, THRESHOLDS.get(col, DEFAULT_MINUTES)) if custom else THRESHOLDS.get(col, DEFAULT_MINUTES)) * 60


def stuck_since(c, col, evs):
    """When the card reached its column: the newest moved event there, else its updated_at (as the inbox does)."""
    moved = [parse_iso(e.get("ts") or "") for e in evs if e.get("kind") == "moved" and e.get("card") == c["id"] and e.get("to") == col]
    return max(moved) if moved else parse_iso(c.get("updated_at") or "")


def _open_alerts():
    by_card = defaultdict(set)
    for a in alerts.listing():
        kind, _, rest = a["key"].partition(":")
        for cid in {*a.get("cards", []), rest.split(":")[0]}:
            by_card[cid].add(kind)
    return by_card


def _screen_hash(text):
    return hashlib.sha1(text.encode()).hexdigest()


def classify(c, col, st, all_cards, kinds, reg):
    """(reason, line): the reason is waiting_owner, blocked, dead or slow, else None (the rules cannot settle it);
    the line is a fixed phrase for the inbox, never card or screen text."""
    from pl import dispatch
    m = c.get("metadata") or {}
    if gate := inbox.gate_of(c, col):
        return "waiting_owner", f"{gate} waiting for approval"
    if m.get("question"):
        return "waiting_owner", "question on the agent screen"
    if "pr_person" in kinds:
        return "waiting_owner", "PR needs a person"
    if kinds.intersection(WAITING_ALERTS):
        return "waiting_owner", "agent is waiting for an answer"
    w = m.get("worker") or {}
    text = agent_screen.read(w["pane"])[0] if w.get("pane") else None
    if text and SIZE_RE.search(text):
        return "blocked", "size limit (max_lines)"
    if unmet := agents.deps_open(c, all_cards):
        return "blocked", f"unmerged dependency {', '.join(short_id(x) for x in unmet)}"
    if kinds.intersection(BLOCKED_ALERTS):
        return "blocked", "unmerged dependency"
    status = dispatch.worker_status(w, reg)[0] if w else None
    if "stage_failed" in kinds or (w and status not in ("alive", "starting")):
        return "dead", "agent died"
    seen = st["steward"].setdefault("screens", {})
    digest = _screen_hash(text) if text else None
    changed = digest is not None and seen.get(c["id"]) not in (None, digest)
    if digest:
        seen[c["id"]] = digest
    return ("slow", "agent is still working") if status == "alive" and changed else (None, None)


def fix_accounts(now):
    """Check each account whose park is past its reset time, even when its next_check says to wait."""
    rows = []
    for name in sorted(C.PROFILES):
        until = accounts.parking(name).get("until")
        if not (until and parse_iso(until) < now and accounts.checkable(name)):
            continue
        with accounts._check_lock(name) as mine:
            if not mine:
                continue
            kind = accounts._check(name)
        events.emit("steward", None, account=name, reason="dead", action="account check", result=kind, by="rule")
        rows.append({"card": None, "subject": name, "column": "account", "reason": "dead", "action": f"account check: {kind}", "by": "rule"})
    return rows


def _loop_every(name, svc):
    if name == "auto-merge":
        return _minutes(C.AUTO_MERGE.get("every", "15m"), 15) * 60 if C.AUTO_MERGE.get("enabled") is True else None
    if svc.get("enabled") is False:
        return None
    return usage.loop_every(svc.get("prompt")) or (_minutes(svc["every"], 0) * 60 if svc.get("every") else None)


def _window_id(name):
    from pl import dispatch
    out = dispatch.tmux("list-windows", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{window_name}", check=False) or ""
    return next((wid for wid, _, wname in (l.partition(" ") for l in out.splitlines()) if wname == name), None)


def fix_loops(st, now):
    """Restart a loop with no run for 2 x its interval, once for each stale ran_at."""
    from pl import dispatch
    rows, done = [], st.setdefault("steward", {}).setdefault("loop_restarts", {})
    for name, svc in {**C.SERVICES, "auto-merge": {}}.items():
        s = (st.get("services") or {}).get(name) or {}
        every, ran = _loop_every(name, svc), s.get("ran_at")
        if not (every and ran and now - parse_iso(ran) > 2 * every) or done.get(name) == ran:
            continue
        done[name] = ran
        if svc.get("every") or name == "auto-merge":
            s["run_now"] = True
            s.pop("next_at", None)
        elif wid := _window_id(name):
            dispatch.tmux("kill-window", "-t", wid, check=False)
        why = f"no run for {int((now - parse_iso(ran)) // 3600)} h (every {every // 60} min)"
        events.emit("steward", None, loop=name, reason="dead", action="restart", message=f"restarted loop {name}: {why}", by="rule")
        rows.append({"card": None, "subject": name, "column": "loop", "reason": "dead", "action": "restart", "by": "rule"})
    return rows


def fix_dead(c, st, now):
    """pl retry for a dead agent, at most [steward] retries times a day for each card; "needs you" past that."""
    from pl import commands
    tries = st.setdefault("steward", {}).setdefault("retries", {})
    recent = [t for t in tries.get(c["id"], []) if now - t < 86400]
    tries[c["id"]] = recent
    if len(recent) >= C.STEWARD["retries"]:
        return "needs you"
    commands.retry(c["id"])
    recent.append(now)
    return "retry"


def fix_held(c, all_cards):
    """Unhold a card whose dependencies are all met; replace a hold reason that names cards now done."""
    from pl import commands
    m = c.get("metadata") or {}
    if not m.get("depends_on"):
        return None
    still = agents.deps_open(c, all_cards)
    if not still:
        commands.unhold(c["id"])
        events.emit("steward", c["id"], reason="blocked", action="unhold", by="rule")
        return "unhold"
    old = str(m.get("hold_reason") or "")
    named = {x["id"] for x in all_cards for ref in CARD_RE.findall(old) if x["id"] == ref or x["id"].endswith("/" + ref)}
    if not named or named & set(still):
        return None
    new = f"waiting on {', '.join(short_id(x) for x in still)}"
    commands.update(c["id"], metadata={"hold_reason": new})
    events.emit("steward", c["id"], reason="blocked", action="refresh reason", old_reason=old[:80], new_reason=new[:80], by="rule")
    return "refresh reason"


REUSED_ALERTS = ("agent_waiting", "agent_blocked_dep", "pr_person", "pr_waiting", "stalled", "stage_failed")
ESCALATE = ("waiting_owner", "blocked", "dead")


def _existing_alert(card):
    for a in alerts.listing():
        kind = a["key"].partition(":")[0]
        if kind in REUSED_ALERTS and card in a.get("cards", []):
            return a["key"]
    return None


def items_update(rows, st, now):
    """One needs-you item and one alert for each card and reason no fix settled; cleared when the card or reason changes."""
    items = st.setdefault("steward", {}).setdefault("items", {})
    current = {r["card"]: r for r in rows if r.get("card") and r["reason"] in ESCALATE and r["action"] in ("none", "needs you")}
    for cid in [k for k, it in items.items() if k not in current or (it["reason"], it["column"]) != (current[k]["reason"], current[k]["column"])]:
        del items[cid]
    for cid, r in current.items():
        if cid in items:
            continue
        head, key = short_id(cid), f"steward:{cid}:{r['reason']}"
        item = {"reason": r["reason"], "line": r["line"], "column": r["column"], "opened_at": now, "pushed": False, "notified": False}
        if other := _existing_alert(cid):
            item["attached_to"] = other
            with alerts._locked() as d:
                d[other]["tried"] = f"steward: {r['reason']}"
        else:
            alerts.open(key, "warn", f"Card {head}: {r['line']}", f"pl card {head} shows where it stands", cards=[cid])
        items[cid] = item
    own = {f"steward:{cid}:{it['reason']}" for cid, it in items.items() if "attached_to" not in it}
    alerts.sweep("steward:", own, lambda title, fix: None)


def _past(now_local, hhmm):
    hour, _, minute = hhmm.partition(":")
    return (now_local.hour, now_local.minute) >= (int(hour), int(minute or 0))


def notify_items(st, now):
    """One notification for each new item, one digest a day after digest_at, one push once an item is old."""
    from pl import dispatch, notifications
    state = st.setdefault("steward", {})
    items = state.setdefault("items", {})
    for cid, it in items.items():
        if not it["notified"]:
            notifications.send("alert_opened", f"needs you: {short_id(cid)}", it["line"])
            it["notified"] = True
        elif not it["pushed"] and now - it["opened_at"] > _minutes(C.STEWARD["push_after"], 240) * 60:
            notifications.send("alert_opened", f"still needs you: {short_id(cid)}", it["line"])
            it["pushed"] = True
    today = dispatch._local_now()
    if items and state.get("digest_on") != today.date().isoformat() and _past(today, C.STEWARD["digest_at"]):
        notifications.send("alert_opened", f"{len(items)} cards need you", "\n".join(f"{short_id(c)}: {it['line']}" for c, it in items.items()))
        state["digest_on"] = today.date().isoformat()


def run_pass(all_cards, st, act=True, reg=None, now=None):
    from pl import dispatch
    now = time.time() if now is None else now
    reg = dispatch.registry() if reg is None else reg
    st.setdefault("steward", {})
    evs, kinds, rows = events._read(), _open_alerts(), []
    for c in all_cards:
        col = c.get("col") or dispatch.col_name(c.get("list_id"))
        if col in EXCLUDED or not (col in THRESHOLDS or dispatch.stage_for(c, col) or inbox.gate_of(c, col)):
            continue
        if act and "parked" in (c.get("tags") or []) and (action := fix_held(c, all_cards)):
            rows.append({"card": c["id"], "column": col, "reason": "blocked", "action": action, "by": "rule"})
            continue
        if now - stuck_since(c, col, evs) < threshold(col):
            continue
        reason, line = classify(c, col, st, all_cards, kinds[c["id"]], reg)
        action = fix_dead(c, st, now) if reason == "dead" and act else "none"
        if action == "needs you":
            line = f"agent died, retried {C.STEWARD['retries']} times today"
        rows.append({"card": c["id"], "column": col, "reason": reason or "unsettled", "line": line or "unclear", "action": action, "by": "rule"})
        if reason and act:
            events.emit("steward", c["id"], reason=reason, action=action, by="rule")
    if act:
        rows += fix_accounts(now) + fix_loops(st, now)
        items_update(rows, st, now)
        notify_items(st, now)
        st["steward"]["ran_at"] = now
    return rows


def cmd_steward(a):
    if not a.once:
        print("usage: pl steward --once [--dry-run] [--json]")
        return
    st = load_state()
    rows = run_pass(board.cards(), st, act=not a.dry_run)
    if not a.dry_run:
        save_state(st)
    if a.json:
        print(json.dumps({"rows": rows}))
        return
    for r in rows:
        print(f"{short_id(r['card']) if r['card'] else r['subject']}  {r['column']:<16} {r['reason']:<14} {r['action']}")
