"""Claude Code hooks: a stage agent reports a permission wait or a finished turn to pl the moment it happens."""
import json
import shlex
from datetime import datetime

from pl import alerts, board, events, harnesses
from pl import config as C
from pl.util import now_iso

EVENTS = ("Notification", "Stop")
HOOK_TIMEOUT = 2   # seconds Claude Code may spend on a hook
SLACK = 5          # seconds a screen change may trail the hook that announced the same prompt


def settings_json(card_id, window):
    """The --settings JSON that makes Claude Code run `pl hook` on each event; `|| true` keeps pl's exit codes
    (2 for a foreign card) from ever blocking the agent, and covers pl missing from PATH."""
    def hook(event):
        cmd = f"pl hook {event} --card {shlex.quote(card_id)} --window {shlex.quote(window)} || true"
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": HOOK_TIMEOUT}]}
        if event == "Notification":   # only a permission prompt: an idle "waiting for your input" one is not a wait for you
            entry["matcher"] = "permission_prompt"
        return [entry]
    return json.dumps({"hooks": {e: hook(e) for e in EVENTS}})


def cmd_hook(a):
    """Record a hook event on the card's worker. Exits 2, changing nothing, for a card or window that is not this
    profile's; any other failure exits 0 so the agent is never slowed. The free text is never read."""
    foreign = False
    try:
        c = board.card(a.card)
        w = ((c or {}).get("metadata") or {}).get("worker") or {}
        foreign = not c or w.get("window") != a.window
        if not foreign:
            _record(c, w, a)
    except (Exception, SystemExit):  # noqa: BLE001
        pass
    if foreign:
        raise SystemExit(2)


def _record(c, w, a):
    w = {k: v for k, v in w.items() if k not in ("permission_wait", "finished_turn")}
    w["hook_event"] = {"event": a.event, "card": a.card, "window": a.window, "at": now_iso()}
    if a.event == "Notification":
        w["permission_wait"] = "waiting: permission"
    else:
        w["finished_turn"] = True
    board.update(c["id"], metadata={"worker": w})
    events.emit("state_transition", c["id"], source="hook", event=a.event)
    if a.event == "Notification":
        alerts.open(f"permission_wait:{c['id']}", "warn", f"{w.get('stage')} agent waiting for permission: {c['title'][:40]}",
                    f"answer it in the agent window (pl card {a.card}); README: Permissions", cards=[c["id"]])


def hook_permission(w):
    """The worker's hook-reported permission wait while the hook is newer than the pane's last change, else None
    (the screen moved on: the prompt was answered)."""
    ev = w.get("hook_event") or {}
    if ev.get("event") != "Notification" or not w.get("permission_wait") or not w.get("pane"):
        return None
    act = (harnesses._run(["tmux", "display-message", "-p", "-t", w["pane"], "#{window_activity}"]).stdout or "").strip()
    if act.isdigit() and datetime.fromisoformat(ev["at"]).timestamp() + SLACK >= int(act):
        return w["permission_wait"]
    return None


def enabled():
    return C.DISPATCH.get("hooks", True)
