"""The console chrome: tab list and the header line (canvas row 0)."""
import time
from pathlib import Path

from rich.text import Text

from pl import config as C


class _TabsList(list):
    """Tabs list that yields the standard 0-9 keyed tabs during iteration, while retaining Home."""
    def __iter__(self):
        return (t for t in super().__iter__() if t[0] != "home")


TABS = _TabsList([("home", "Home"), ("dashboard", "Dashboard"), ("assistant", "Assistant"),
                  ("ideas", "Ideas"), ("cards", "Pipeline"), ("prs", "Pull requests"),
                  ("loops", "Loops"), ("alerts", "Alerts"), ("activity", "Activity"),
                  ("settings", "Settings"), ("subagents", "Background"), ("brain", "Brain")])
# tab key = position in this list: 0 Dashboard, 1 Assistant, 2 Ideas, 3 Pipeline, ... 9 Background.
# Keys stay fixed when tabs() hides the Assistant.
TAB_KEYS = {"home": "H", **{tid: str(i) for i, (tid, _) in enumerate(TABS) if tid != "brain"}, "brain": "b"}   # brain uses b

# What each bound action means to the person at the keys. A key may serve several actions only when they share a meaning;
# tests/test_tui_views.py fails on a key with two, and on a binding whose action is missing here.
MEANING = {
    "tab": "go to tab", "step": "step", "cycle_window": "step", "app.focus_next": "next field",
    "standup": "standup", "refresh": "refresh", "rescan": "refresh", "dispatcher": "dispatcher",
    "kill_switch": "kill switch", "help": "help", "update": "update", "switcher": "switcher",
    "oldest": "oldest waiting", "restart_console": "restart console", "quit": "quit",
    "close": "close", "cancel": "close", "back": "close", "leave_box": "close",
    "approve": "approve", "review('approve')": "approve", "send_back": "send back", "review('send_back')": "send back",
    "hold": "hold", "restart": "restart", "copy": "copy", "open": "open in browser", "open_card": "open in browser",
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
    "settings": "Accounts, models, skills and the other settings of this profile.",
    "subagents": "Agents running in the background and what each is doing.",
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
    """The tabs this console shows: Home when >= 2 profiles have config.toml, less Assistant when disabled."""
    count = sum(1 for p in Path.home().glob(".pl-*") if (p / "config.toml").is_file())
    res = [("home", "Home")] if count >= 2 else []
    return res + [t for t in TABS if t[0] != "assistant" or C.ASSISTANT.get("enabled", True) is not False]


def stopped_by(data):
    """"console-D 19:16:02" or "cli 19:16:02" when the manager marks this profile's dispatcher stopped by a request."""
    row = next((p for p in ((data or {}).get("machine") or {}).get("profiles") or [] if p.get("name") == C.PROFILE_NAME), {})
    if not (row.get("stopped") and row.get("stopped_by") and row.get("stopped_at")):
        return None
    return f"{row['stopped_by']} {time.strftime('%H:%M:%S', time.localtime(row['stopped_at']))}"


def header_text(data, error=None, note=None):
    """pl, profile badge, tracker type, dispatcher state, accounts, pause state, clock, and the last refresh error."""
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" pl ", style="bold #e0a040")
    t.append(f" {C.PROFILE_NAME or 'no profile'} ", style="bold #101010 on #e0a040")
    t.append(f"  {(C.TRACKER or {}).get('type') or 'no tracker'}", style="dim")
    snap = (data or {}).get("snapshot")
    if snap:
        up = snap["disp"] == "running"
        t.append("   ● " if up else "   ✕ ", style="green" if up else "red")
        t.append(f"dispatcher {snap['disp']}")
        if not up and (by := stopped_by(data)):
            t.append(f" (stopped by you: {by})", style="bold red")
        t.append(f"   {snap['prof']}", style="red" if snap.get("parked") else "dim")
        t.append("   PAUSED" if snap["summary"].startswith("PAUSED") else "   not paused",
                 style="bold #e0a040" if snap["summary"].startswith("PAUSED") else "dim")
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
    if note:
        t.append(f"   {note}", style="bold red" if "fail" in note or "still running" in note else "#e0a040")
    if error:
        t.append(f"   {error}", style="bold red")
    return t
