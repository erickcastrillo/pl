"""Pipeline details screen: columns, stages, gates, board, loops, agents and dispatcher switch."""
import json
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from pl.tui.review import ConfirmScreen


def _run(argv, timeout=10):
    """The one subprocess seam of this module (tests fake it)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _load_summary(name):
    path = Path.home() / f".pl-{name}" / "state" / "summary.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


class DetailsScreen(Screen):
    """Pipeline details for one profile."""

    DEFAULT_CSS = """
    #details-head { height: auto; padding: 0 1; border: round $primary; }
    #details-body { height: 1fr; border: round $primary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("q", "close", "close"),
        Binding("D", "dispatcher", "dispatcher"),
    ]

    def __init__(self, name, summary=None):
        super().__init__(name=name)
        self.profile = name
        self.summary = summary if summary is not None else _load_summary(name)
        self.data = None

    def compose(self):
        yield Static(Text(f"loading pipeline {self.profile}…", style="dim"), id="details-head")
        with VerticalScroll(id="details-body"):
            yield Static("", id="details-text")
        yield Footer()

    def on_mount(self):
        name = self.profile

        def fetch():
            argv = [sys.executable, "-m", "pl", "--profile", name, "pipelines", "show", "--json"]
            try:
                res = _run(argv)
                out = res.stdout if hasattr(res, "stdout") else (res[1] if isinstance(res, (tuple, list)) else str(res))
                parsed = json.loads(out)
            except Exception as e:
                parsed = {"error": str(e)}
            self.app.call_from_thread(self._show, parsed)

        self.run_worker(fetch, thread=True, group="details")

    def _show(self, data):
        self.data = data
        disp_state = self.summary.get("disp") or self.summary.get("running_state") or "stopped"

        head = Text()
        head.append(f"Pipeline details: {self.profile}", style="bold")
        head.append(f"  ● {disp_state}", style="green" if disp_state == "running" else "red")
        head.append("  [D] switch dispatcher", style="dim")
        self.query_one("#details-head", Static).update(head)

        body = Text()
        if "error" in data:
            body.append(f"Could not load pipeline: {data['error']}\n", style="red")
            self.query_one("#details-text", Static).update(body)
            return

        # Columns in order
        cols = data.get("columns") or []
        body.append("Columns:\n", style="bold")
        body.append(f"  {' → '.join(cols) if cols else 'none'}\n\n")

        # Stages
        stages = data.get("stages") or {}
        body.append("Stages:\n", style="bold")
        if isinstance(stages, dict):
            for sname, sinfo in stages.items():
                skill = sinfo.get("skill", "")
                account = sinfo.get("account", "")
                sec = sinfo.get("section", "")
                gate = sinfo.get("gate", "none")
                body.append(f"  • {sname}: skill {skill} · account {account} · section {sec} · gate {gate}\n")
        elif isinstance(stages, list):
            for sinfo in stages:
                sname = sinfo.get("name", "")
                skill = sinfo.get("skill", "")
                account = sinfo.get("account", "")
                sec = sinfo.get("section", "")
                gate = sinfo.get("gate", "none")
                body.append(f"  • {sname}: skill {skill} · account {account} · section {sec} · gate {gate}\n")
        if not stages:
            body.append("  none\n")
        body.append("\n")

        # Board
        board = data.get("board")
        b_type, b_id = "", ""
        if isinstance(board, dict):
            b_type, b_id = board.get("type", ""), board.get("id", "")
        elif board:
            b_type = str(board)
        body.append("Board:\n", style="bold")
        body.append(f"  {b_type} {b_id}".strip() or "  none")
        body.append("\n\n")

        # Connectors
        connectors = data.get("connectors")
        body.append("Connectors:\n", style="bold")
        if connectors:
            body.append(f"  {', '.join(connectors) if isinstance(connectors, list) else connectors}\n\n")
        else:
            body.append("  none\n\n")

        # Loops
        loops = data.get("loops") or []
        body.append("Loops:\n", style="bold")
        if loops:
            body.append(f"  {', '.join(loops) if isinstance(loops, list) else loops}\n\n")
        else:
            body.append("  none\n\n")

        # Live agents & running state from summary
        agents = self.summary.get("live_agents") or self.summary.get("live agents") or []
        body.append("Live agents:\n", style="bold")
        if agents:
            body.append(f"  {', '.join(agents) if isinstance(agents, list) else agents}\n\n")
        else:
            body.append("  none\n\n")

        body.append("Running state:\n", style="bold")
        body.append(f"  {disp_state}\n\n")

        body.append("Dispatcher:\n", style="bold")
        body.append("  Press D to restart or stop the dispatcher\n")

        self.query_one("#details-text", Static).update(body)

    def action_close(self):
        self.app.pop_screen()

    def action_dispatcher(self, action="restart"):
        cmd = [sys.executable, "-m", "pl", "manager", action, self.profile]
        msg = (
            f'Restart the dispatcher of profile "{self.profile}"? Restart stops and starts it again; stop '
            f"stops it until you press D again. Running agents keep running, and so do the manager and other "
            f"profiles' dispatchers (pl manager stop stops the manager)."
        )

        def on_confirm(yes):
            if yes:
                self.run_worker(lambda: _run(cmd), thread=True, group="dispatcher")

        self.app.push_screen(ConfirmScreen(msg), on_confirm)
