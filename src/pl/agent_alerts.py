"""Alerts for stage agents that wait for a person, finish without moving their card, or are blocked on an unmerged card."""
import re
import time

from pl import agent_screen, agents, alerts
from pl import config as C
from pl.util import cmd_id, short_id

BLOCK_RE = re.compile(r"missing prerequisite|not (?:yet )?merged|needs? .* merged|depends on|prerequisite slice", re.I)
CARD_RE = re.compile(r"[\w.-]+/[\w.-]+#\d+|[\w.-]+#\d+")
SLICE_RE = re.compile(r"slice (\d+)", re.I)
DEFAULT_WAIT = 30   # minutes; [dispatch] release_waiting_after = 0 turns release off, not the alert
BLOCK_LINES = 8
TITLE_WIDTH = 80
KINDS = ("agent_waiting:", "agent_unmoved:", "agent_blocked_dep:")


def _dispatch():
    from pl import dispatch   # imported here: dispatch imports this module
    return dispatch


def check(all_cards, st, restarting):
    """One pass over the stage agents with a pane: open the alerts that hold, resolve the ones that cleared.
    restarting: ids of the cards the dispatcher starts again this pass; their dead agent opens nothing."""
    d = _dispatch()
    waits, seen = st.get("agent_waits") or {}, {}
    active = set()
    for c in all_cards:
        m = c.get("metadata") or {}
        w = m.get("worker") or {}
        if c.get("unreadable") or m.get("pipeline_mode") != "auto" or m.get("dropped_at") or c["id"] in restarting:
            continue
        col = d.col_name(c["list_id"])
        if not w.get("pane") or w.get("stage") != d.stage_for(c, col):
            continue
        text, word, why = agent_screen.read(w["pane"])
        if text is None:   # an unreadable pane keeps its alerts: resolving them would notify again on the next good read
            active.update(k for k in (f"{p}{c['id']}" for p in KINDS) if alerts.get(k))
            continue
        lines = agent_screen.plain(text).splitlines()
        for found in (_waiting(c, w, text, word, why, waits, seen), _unmoved(c, w, col, word),
                      _blocked(c, w, all_cards, lines)):
            if found:
                active.add(found)
    st["agent_waits"] = seen   # a card no longer waiting starts a new clock next time
    for prefix in KINDS:
        alerts.sweep(prefix, active, _dispatch().notify)


def _raise(key, severity, title, fix, cards):
    alerts.open(key, severity, title, fix, cards=cards)
    return key


def _limit():
    return C.DISPATCH.get("release_waiting_after") or DEFAULT_WAIT


def _waiting(c, w, text, word, why, waits, seen):
    d = _dispatch()
    gate = next((g for g in agents.GATE_RE.findall(agent_screen.plain(text)) if g in d.HUMAN_GATES), None)
    asked = w.get("permission_wait") or w.get("trust_wait")
    if not (word == "waiting for you" or gate or asked) or alerts.get(f"permission_wait:{c['id']}"):
        return None
    rec = waits.get(c["id"]) or {}
    if rec.get("session") != w.get("session_id") or not isinstance(rec.get("since"), (int, float)):
        rec = {"since": time.time(), "session": w.get("session_id")}
    seen[c["id"]] = rec
    if time.time() - rec["since"] < _limit() * 60:
        return None
    first_line = (why or gate or (asked if isinstance(asked, str) else "") or "a prompt")[:TITLE_WIDTH]
    cid = c["id"]
    return _raise(f"agent_waiting:{cid}", "high" if gate else "warn",
                  f"Card {short_id(cid)}: its {w['stage']} agent waits for you: {first_line}",
                  f"see tmux window {w.get('window')} in {C.TMUX_SESSION}; pl drop {cmd_id(cid)} or pl retry {cmd_id(cid)}", [cid])


def _unmoved(c, w, col, word):
    d = _dispatch()
    stage, cid = w["stage"], c["id"]
    if not (word == "finished" or w.get("finished_turn")) or d.stage_complete(c, col, stage):
        return None
    prs = (c.get("metadata") or {}).get("pr_urls") or []
    if stage == "run" and prs:
        return _raise(f"agent_unmoved:{cid}", "warn",
                      f"Card {short_id(cid)}: its run agent finished with a PR but the card was not moved",
                      f'pl move {cmd_id(cid)} "PR open" --pr {prs[-1]}', [cid])
    return _raise(f"agent_unmoved:{cid}", "warn", f"Card {short_id(cid)}: its {stage} agent finished without moving the card",
                  f"pl retry {cmd_id(cid)} or pl drop {cmd_id(cid)}", [cid])


def _named_cards(lines, c, all_cards):
    """Board ids named in the lines, by card id or by "slice N" of the card's own plan."""
    text = "\n".join(lines)
    m = c.get("metadata") or {}
    named = []
    for x in all_cards:
        xm = x.get("metadata") or {}
        by_slice = m.get("plan") is not None and xm.get("plan") == m.get("plan") and str(xm.get("slice")) in SLICE_RE.findall(text)
        by_id = any(t == x["id"] or t.rsplit("/", 1)[-1] == short_id(x["id"]) for t in CARD_RE.findall(text))
        if x["id"] != c["id"] and (by_slice or by_id):
            named.append(x["id"])
    return named


def _blocked(c, w, all_cards, lines):
    cid = c["id"]
    tail = [ln for ln in lines if ln.strip()][-BLOCK_LINES:]
    ids = agents.deps_open(c, all_cards) if w["stage"] == "run" else []
    if any(BLOCK_RE.search(ln) for ln in tail):
        ids += _named_cards(tail, c, all_cards)
    unmerged = list(dict.fromkeys(agents.deps_open({"metadata": {"depends_on": ids}}, all_cards)))
    if not unmerged:
        return None
    return _raise(f"agent_blocked_dep:{cid}", "warn",
                  f"Card {short_id(cid)}: its {w['stage']} agent is blocked on unmerged {', '.join(map(short_id, unmerged))}",
                  f"merge {', '.join(map(cmd_id, unmerged))} or pl depends {cmd_id(cid)} --clear", [cid, *unmerged])
