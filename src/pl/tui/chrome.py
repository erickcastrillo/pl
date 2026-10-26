"""The console chrome: tab list and the header line (canvas row 0)."""
import re
import time
from pathlib import Path

from rich.text import Text

from pl import config as C
from pl.tui.needs import needs_you_count


GROUPS = [
    ("dashboard", "Dashboard", [("dashboard", "Dashboard")]),
    ("assistant", "Assistant", [("assistant", "Assistant"), ("brain", "Brain")]),
    ("work", "Work", [("ideas", "Ideas"), ("cards", "Board")]),
    ("review", "Review", [("prs", "Pull requests")]),
    ("automate", "Automate", [("loops", "Loops"), ("subagents", "Running")]),
    ("extend", "Extend", [("skills", "Skills"), ("connectors", "Connectors"), ("pods", "Pods"), ("models", "Models"),
                          ("extensions", "Extensions")]),
    ("activity", "Activity", [("activity", "Activity"), ("alerts", "Alerts")]),
    ("settings", "Settings", [("settings", "Settings")]),
    ("home", "Home", [("home", "Home")]),
]
# Keys never shift: the Assistant keeps 2 while it is off.
GROUP_KEYS = {**{gid: str(i + 1) for i, (gid, _, _) in enumerate(g for g in GROUPS if g[0] != "home")}, "home": "H"}
TABS = [(vid, name) for _, _, views in GROUPS for vid, name in views]

# What each bound action means to the person at the keys. A key may serve several actions only when they share a meaning;
# tests/test_tui_views.py fails on a key with two, and on a binding whose action is missing here.
MEANING = {
    "tab": "go to tab", "step": "step", "cycle_window": "step", "app.focus_next": "next field",
    "standup": "standup", "refresh": "refresh", "rescan": "refresh", "dispatcher": "dispatcher",
    "kill_switch": "kill switch", "help": "help", "update": "update", "switcher": "switcher",
    "oldest": "oldest waiting", "restart_console": "restart console", "quit": "quit",
    "close": "close", "cancel": "close", "back": "close", "leave_box": "close",
    "approve": "approve", "review('approve')": "approve", "send_back": "send back", "review('send_back')": "send back",
    "hold": "hold", "pause": "hold", "run_now": "run now", "restart": "restart", "copy": "copy", "open": "open in browser", "open_card": "open in browser",
    "open_window": "open window", "jump": "open window", "drop": "remove", "discard": "remove", "delete": "remove",
    "revoke": "remove", "retry": "try again", "move_agent": "move to account", "move_column": "move to column",
    "only_mine": "filter", "plan_filter": "filter", "next_filter": "filter", "done": "done", "show_done": "show done", "undrop": "undo",
    "undo": "undo", "adopt": "to pipeline", "hand_off": "to Manual", "answer": "answer", "edit": "edit",
    "edit_brief": "edit", "edit_input": "edit", "external_edit": "edit in $EDITOR", "ack": "acknowledge",
    "select": "select", "details": "details", "merge": "merge", "new": "new", "rename": "rename",
    "harness": "harness", "save": "save", "test": "test connections", "install": "install", "share": "share",
    "link": "link", "mode": "chat/idea", "file_idea": "file idea", "skip": "skip question",
}

HELP = {
    "dashboard": "What needs you first, then what the agents did in the chosen window. [ and ] change the window, s writes a standup.",
    "assistant": "Ask the assistant about your cards, pull requests and settings. Type in the box and press Enter.",
    "ideas": "Ideas waiting to become cards. Pick one to read it, edit it or send it into the pipeline.",
    "cards": "Every card by column. A card that waits on you shows approve and send back in the footer; the details are on the right.",
    "prs": "Pull requests by what they need: a decision, a merge, a rework, or only a wait.",
    "loops": "The background loops that keep the pipeline moving, with when each last ran.",
    "alerts": "Things that went wrong or need a look. Pick one to read it.",
    "activity": "A log of what the agents and you did, newest first.",
    "settings": "Accounts, board, stages, models and effort, dispatcher limits and notifications of this profile.",
    "skills": "Every account's skills, agents and commands. Pick one to read or edit it, n makes a new one, s shares it, l links it.",
    "connectors": "The services agents can reach. n adds a connector, t tests it, R revokes it.",
    "pods": "Pods hold the rules agents follow. r adds a rule to the pod, Enter shows it.",
    "models": "Local models: t test, s set.",
    "extensions": "Hooks, plugins and other extensions of every account.",
    "subagents": "Running: agents working in the background and what each is doing.",
    "brain": "One read-only brain session per machine, for questions across your pipelines.",
    "home": "Your pipelines, one row each. Press Enter to open one.",
}


def skip_headings(t, is_item, last=None):
    """Keep a DataTable's cursor off heading rows. On a heading it moves to the next row is_item(key) accepts in the
    direction of travel (down unless it came up from row `last`), else the nearest one the other way; with no such
    row it stays. Returns the cursor row."""
    keys = [r.key.value for r in t.ordered_rows]
    row = t.cursor_row
    if not keys or row >= len(keys) or is_item(keys[row]):
        return row
    after = [i for i in range(row + 1, len(keys)) if is_item(keys[i])]
    before = [i for i in range(row - 1, -1, -1) if is_item(keys[i])]
    to = next(iter(after + before if last is None or row > last else before + after), None)
    if to is not None:
        t.move_cursor(row=to)
        return to
    return row


def show_first_heading(t, is_item):
    """After the table's own scroll to the cursor (it runs after the next refresh): when only headings sit above the
    cursor, scroll to the top so the first heading shows. A cursor further down keeps its scroll."""
    def top():
        keys = [r.key.value for r in t.ordered_rows]
        if t.cursor_row < len(keys) and not any(is_item(k) for k in keys[:t.cursor_row]):
            t.scroll_home(animate=False)
    t.call_after_refresh(top)


def tabs():
    """The views this console shows: Home when >= 2 profiles have config.toml, less Assistant when disabled."""
    count = sum(1 for p in Path.home().glob(".pl-*") if (p / "config.toml").is_file())
    return [(vid, name) for vid, name in TABS
            if (vid != "home" or count >= 2) and (vid != "assistant" or C.ASSISTANT.get("enabled", True) is not False)]


def groups():
    """The groups with at least one visible view: (gid, label, [(view_id, sub_label), ...])."""
    shown = {vid for vid, _ in tabs()}
    shown_groups = ((gid, label, [v for v in views if v[0] in shown]) for gid, label, views in GROUPS)
    return [g for g in shown_groups if g[2]]


def resolve(name):
    """(gid, view_id) for a visible view id, (gid, None) for a group id (its first view), else None."""
    shown = groups()
    for gid, _, views in shown:
        if any(vid == name for vid, _ in views):
            return gid, name
    return next(((gid, None) for gid, _, _ in shown if gid == name), None)


def stopped_at(data):
    """"19:16" when the manager marks this profile's dispatcher stopped by a request."""
    row = next((p for p in ((data or {}).get("machine") or {}).get("profiles") or [] if p.get("name") == C.PROFILE_NAME), {})
    if not (row.get("stopped") and row.get("stopped_by") and row.get("stopped_at")):
        return None
    return time.strftime("%H:%M", time.localtime(row["stopped_at"]))


def _plural(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def status_sentence(snap, stopped=None):
    """The dispatcher status as a sentence: "Running, 2 agents working, 1 card waiting". `stopped` is the HH:MM a
    person stopped it."""
    if snap["disp"] != "running":
        return "Stopped" + (f" (by you at {stopped})" if stopped else "")
    summary = snap["summary"]
    if summary.startswith("PAUSED"):
        draining = re.search(r"draining, (\d+) working", summary)
        return f"Paused, draining {_plural(int(draining[1]), 'agent')}" if draining else "Paused, drained"
    working = sum(1 for r in snap.get("rows", []) if r.get("kind") == "working")
    waiting = sum((snap.get("needs") or {}).values())
    parts = ["Running"]
    if working:
        parts.append(f"{_plural(working, 'agent')} working")
    parts.append(f"{_plural(waiting, 'card')} waiting" if waiting else "no cards waiting")
    sentence = ", ".join(parts)
    if snap.get("parked") and (out := re.search(r"(\S+): PARKED until (.+?)(?:,| \(\d+ running|$)", snap["prof"])):
        sentence += f"; account {out[1]} is out of credits until {out[2]}"
    return sentence


def header_text(data, error=None, note=None, last_good=None):
    """pl, profile badge, tracker type, dispatcher state, accounts, pause state, clock, and the last refresh error."""
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" pl ", style="bold #e0a040")
    t.append(f" {C.PROFILE_NAME or 'no profile'} ", style="bold #101010 on #e0a040")
    t.append(f"  {(C.TRACKER or {}).get('type') or 'no tracker'}", style="dim")
    snap = (data or {}).get("snapshot")
    if snap:
        up = snap["disp"] == "running"
        paused = snap["summary"].startswith("PAUSED")
        t.append("   ● " if up else "   ✕ ", style="green" if up else "red")
        t.append(status_sentence(snap, stopped_at(data)),
                 style="bold #e0a040" if paused else "red" if snap.get("parked") or not up else "")
        t.append(f"   {snap['at']}", style="dim")
    else:
        t.append("   loading…", style="dim")
    kill = (data or {}).get("kill")
    if kill is None and isinstance(data, dict):
        try:
            from pl import outward
            kill = outward.state()
        except Exception:
            pass
    if kill and kill.get("on"):
        since = kill.get("since") or ""
        hhmm = since.split("T")[1][:5] if "T" in since else since[:5]
        since_str = f" since {hhmm}" if hhmm else ""
        t.append(f"   KILL SWITCH ON{since_str}", style="bold red")
    if snap and (n := needs_you_count(data)):
        t.append(f"   needs you {n}", style="bold red")
    if note:
        t.append(f"   {note}", style="bold red" if "fail" in note or "still running" in note else "#e0a040")
    if error:
        if last_good:
            t.append(f"   data from {last_good}", style="bold red")
        t.append(f"   {error}", style="bold red")
    return t
