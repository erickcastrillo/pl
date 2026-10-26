"""Home tab: overview of every pipeline, read only from state/summary.json."""
import json
import time
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Static

from pl import config as C
from pl import util
from pl.tui import getstarted


def read(profile_dir: Path | str) -> dict | None:
    """Read state/summary.json from a profile directory. Never parses TOML or loads config."""
    p = Path(profile_dir)
    f = p / "state" / "summary.json"
    name = p.name.removeprefix(".pl-")
    if not f.is_file():
        return {"name": name, "missing": True}
    try:
        data = json.loads(f.read_text("utf-8"))
        if not isinstance(data, dict):
            return {"name": name, "missing": True}
        data.setdefault("name", name)
        return data
    except Exception:
        return {"name": name, "missing": True}


def home_rows(summaries) -> list[dict]:
    """One dict per pipeline from summary dicts. Handles stale or missing summaries."""
    now = time.time()
    out = []
    for s in summaries or []:
        if not s or not isinstance(s, dict):
            continue
        name = s.get("name") or s.get("pipeline") or "unknown"
        if s.get("missing"):
            out.append({"name": name, "running": False, "status": "no summary yet: its dispatcher has not run",
                        "cards": "", "needs_you": 0, "live_agents": 0, "oldest_wait": "-", "alerts": 0})
            continue

        at, interval = s.get("at"), s.get("interval", 30) or 30
        is_stale = (now - at > 3 * interval) if isinstance(at, (int, float)) else False
        is_running = False if is_stale else bool(s.get("running", False))

        cols = s.get("columns", [])
        if isinstance(cols, list):
            cards = " · ".join(f"{c.get('title')} {c.get('cards')}" for c in cols
                               if isinstance(c, dict) and c.get("cards", 0) > 0)
        elif isinstance(cols, dict):
            cards = " · ".join(f"{k} {v}" for k, v in cols.items() if v > 0)
        else:
            cards = ""

        oldest = s.get("oldest_wait")
        ts = oldest.get("since") or oldest.get("ts") if isinstance(oldest, dict) else (oldest if isinstance(oldest, (int, float)) else None)
        if isinstance(ts, str):   # summary.py writes the card's updated_at, an ISO time
            ts = util.parse_iso(ts) or None
        al = s.get("alerts", 0)

        out.append({
            "name": name, "running": is_running, "status": "running" if is_running else "stopped",
            "cards": cards, "needs_you": int(s.get("needs_you", 0) or 0),
            "live_agents": int(s.get("live_agents", 0) or 0), "oldest_wait": util.age(ts) if ts else "-",
            "alerts": len(al) if isinstance(al, list) else int(al or 0),
        })
    return out


class HomeView(Widget):
    """The Home tab DataTable showing all configured pipelines."""
    BINDINGS = [
        Binding("enter", "select", "switch pipeline", show=True), Binding("space", "details", "details"),
        Binding("B", "get_started('settings')", "board"),
        Binding("C", "get_started('settings')", "accounts", show=False),
        Binding("W", "get_started('idea')", "first idea"),
        Binding("V", "get_started('cards')", "board view", show=False),
        Binding("Y", "get_started('prs')", "pull requests", show=False),
        Binding("G", "collapse_start", "collapse"),
        Binding("Z", "dismiss_start", "dismiss"),
    ]
    CHECKLIST_ACTIONS = {"get_started", "collapse_start", "dismiss_start"}

    def __init__(self, selected_profile=None):
        super().__init__()
        self.selected_profile = selected_profile
        self.rows = []
        self.started = self.collapsed = False
        self.first = True

    def compose(self):
        box = Static(id="get-started")
        box.display = False
        yield box
        yield DataTable(id="home-table")

    def check_action(self, action, parameters):
        return self.started if action in self.CHECKLIST_ACTIONS else True

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns("pipeline", "status", "cards", "needs you", "live agents", "wait", "alerts")
        t.cursor_type = "row"

    def selected(self):
        t = self.query_one(DataTable)
        return self.rows[t.cursor_row] if self.rows and 0 <= t.cursor_row < len(self.rows) else None

    def show_checklist(self, data):
        box = self.query_one("#get-started", Static)
        box.display = self.started
        if self.started:
            box.update(getstarted.text(getstarted.ticks(data), self.collapsed))

    def show(self, data):
        try:
            self.started = getstarted.active(data)
        except Exception:   # a broken state file only hides the checklist
            self.started = False
        self.show_checklist(data)
        self.app.refresh_bindings()
        if self.first and self.started and not self.app.initial_tab:
            self.app.action_tab("home")
        self.first = False
        t = self.query_one(DataTable)
        keep, rows = t.cursor_row, home_rows((data or {}).get("home", []))
        if rows != self.rows:
            self.rows = rows
            t.clear()
            for r in self.rows:
                t.add_row(Text(r["name"]), Text(r["status"], style="green" if r["running"] else "dim"),
                          Text(r["cards"]), Text(str(r["needs_you"]), style="bold red" if r["needs_you"] > 0 else "bold"),
                          Text(str(r["live_agents"])), Text(r["oldest_wait"]), Text(str(r["alerts"])))
            if self.rows:
                t.move_cursor(row=min(max(keep, 0), len(self.rows) - 1))

    def action_select(self):
        """Enter opens the pipeline: this profile's board, or another profile's console (as ctrl+k does)."""
        if r := self.selected():
            if r["name"] == C.PROFILE_NAME:
                self.app.action_tab("cards")
            else:
                self.app.switch_to(r["name"])

    def on_data_table_row_selected(self, _):
        self.action_select()

    def action_details(self):
        from pl.tui.details import DetailsScreen
        r = self.selected()
        self.app.push_screen(DetailsScreen((r or {}).get("name") or self.selected_profile or C.PROFILE_NAME or "default"))

    def action_get_started(self, place):
        if place == "idea":
            self.app.action_tab("ideas")
            if box := next(iter(self.app.query("#idea-input")), None):
                box.focus()
        else:
            self.app.action_tab(place)

    def action_collapse_start(self):
        self.collapsed = not self.collapsed
        self.show_checklist(self.app.data)

    def action_dismiss_start(self):
        getstarted.mark(dismissed=time.time())
        self.started = False
        self.show_checklist(self.app.data)
        self.app.refresh_bindings()
