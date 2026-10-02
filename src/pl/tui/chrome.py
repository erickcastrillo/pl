"""The console chrome: tab list and the header line (canvas row 0)."""
from rich.text import Text

from pl import config as C

TABS = [("assistant", "Assistant"), ("dashboard", "Dashboard"), ("needs", "Needs you"), ("ideas", "Ideas"), ("pipeline", "Kanban"),
        ("prs", "Pull requests"), ("loops", "Loops"), ("activity", "Activity"), ("settings", "Settings"),
        ("subagents", "Background"), ("alerts", "Alerts"), ("cards", "Pipeline")]
# tab key = position in this list: 0 Assistant, 1 Dashboard, ... 9 Background; the digits run out, so Alerts is ! and Pipeline @
TAB_KEYS = {tid: str(i) for i, (tid, _) in enumerate(TABS[:10])} | {"alerts": "!", "cards": "@"}
KEY_NAMES = {"!": "exclamation_mark", "@": "at"}   # Textual's names for the keys


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


def tabs():
    """The tabs this console shows: all of them, less the Assistant when [assistant] enabled = false."""
    return [t for t in TABS if t[0] != "assistant" or C.ASSISTANT.get("enabled", True) is not False]


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
        t.append(f"   {snap['prof']}", style="red" if snap.get("parked") else "dim")
        t.append("   PAUSED" if snap["summary"].startswith("PAUSED") else "   not paused",
                 style="bold #e0a040" if snap["summary"].startswith("PAUSED") else "dim")
        t.append(f"   {snap['at']}", style="dim")
    else:
        t.append("   loading…", style="dim")
    if note:
        t.append(f"   {note}", style="bold red" if "fail" in note or "still running" in note else "#e0a040")
    if error:
        t.append(f"   {error}", style="bold red")
    return t
