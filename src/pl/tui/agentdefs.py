"""Extend → Agents: a read-only view of `pl agents list --json` with one agent's full definition on Enter."""
import json
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Static

from pl.tui.extend import MISSING_STYLE

_run = subprocess.run   # the one subprocess runner here; tests fake it
COLUMNS = ("name", "source", "role", "skills", "connector roles")


def row_cells(agent, problems):
    style = MISSING_STYLE if problems else ""
    values = (agent["name"], agent["source"], agent["role"], ", ".join(agent["skills"]), ", ".join(agent["connector_roles"]))
    return [Text(v, style=style) for v in values]


def detail_text(agent):
    text = Text(f"{agent['instructions']}\n")
    text.append(f"skills: {', '.join(agent['skills'])}\n")
    text.append(f"connector roles: {', '.join(agent['connector_roles'])}\n")
    text.append(f"model: {agent['model'] or 'stage default'}\n")
    text.append(f"approval: {', '.join(agent['approval']) or 'none'}\n")
    text.append(f"pod: {agent['pod'] or 'none'}")
    if agent.get("problems"):
        text.append("\nproblems:\n")
        text.append("\n".join(agent["problems"]), style=MISSING_STYLE)
    return text


def _json(argv):
    try:
        res = _run(argv, capture_output=True, text=True)
        if res.returncode == 0:
            return json.loads(res.stdout), None
        lines = [ln for ln in (res.stderr or res.stdout or "").splitlines() if ln.strip()]
        return None, lines[-1].strip() if lines else f"exit {res.returncode}"
    except Exception as e:
        return None, str(e)


class AgentsView(Widget):
    DEFAULT_CSS = """
    AgentsView { height: auto; min-height: 8; }
    #agents-table { height: 12; }
    #agents-detail { height: auto; padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "back", "back")]
    can_focus = True   # the hidden table loses focus, so Escape needs a focused owner

    def __init__(self):
        super().__init__()
        self.details = {}

    def compose(self):
        yield Static("", id="agents-message")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="agents-table")
        detail = Static("", id="agents-detail")
        detail.display = False
        yield detail

    def on_mount(self):
        self.query_one("#agents-table", DataTable).add_columns(*COLUMNS)
        self.run_worker(self._load, thread=True, exclusive=True)

    def _load(self):
        agents, err = _json(["pl", "agents", "list", "--json"])
        if err:
            self.app.call_from_thread(self._show_error, err)
            return
        self.app.call_from_thread(self._fill, agents)
        for agent in agents:
            detail, err = _json(["pl", "agents", "show", agent["name"], "--json"])
            if err:
                continue
            self.details[agent["name"]] = detail
            if detail.get("problems"):
                self.app.call_from_thread(self._mark, agent, detail["problems"])

    def _show_error(self, err):
        self.query_one("#agents-message", Static).update(Text(err, style="bold red"))
        self.query_one("#agents-table", DataTable).display = False

    def _fill(self, agents):
        table = self.query_one("#agents-table", DataTable)
        for agent in agents:
            table.add_row(*row_cells(agent, []), key=agent["name"])

    def _mark(self, agent, problems):
        table = self.query_one("#agents-table", DataTable)
        for column, cell in zip(table.columns, row_cells(agent, problems)):
            table.update_cell(agent["name"], column, cell)

    def on_data_table_row_selected(self, event):
        event.stop()
        name = event.row_key.value
        if name not in self.details:
            self.run_worker(lambda: self._fetch_and_open(name), thread=True)
            return
        self._open(self.details[name])

    def _fetch_and_open(self, name):
        detail, err = _json(["pl", "agents", "show", name, "--json"])
        if err:
            self.app.call_from_thread(self._show_error, err)
            return
        self.details[name] = detail
        self.app.call_from_thread(self._open, detail)

    def _open(self, detail):
        self.query_one("#agents-table", DataTable).display = False
        pane = self.query_one("#agents-detail", Static)
        pane.update(detail_text(detail))
        pane.display = True
        self.focus()

    def action_back(self):
        pane = self.query_one("#agents-detail", Static)
        if not pane.display:
            return
        pane.display = False
        table = self.query_one("#agents-table", DataTable)
        table.display = True
        table.focus()
