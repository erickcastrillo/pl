"""List-and-actions panel for settings: runs a CLI command returning JSON, displays a DataTable,
and offers action key bindings configured with argument prompts and confirmation."""
import json
import re
import subprocess
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from pl.tui.chrome import NEW_ROW, new_row
from pl.tui.dashboard import copy_text
from pl.tui.review import ConfirmScreen

_run = subprocess.run  # single subprocess runner seam; tests fake it


class InputModal(ModalScreen):
    """Asks for an argument value in a small modal."""
    DEFAULT_CSS = """
    InputModal { align: center middle; }
    InputModal > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt, placeholder=""):
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self):
        with Vertical():
            yield Static(Text(self.prompt))
            yield Input(placeholder=self.placeholder, id="modal-input")
            yield Static(Text("enter submit · esc cancel", style="dim"))

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, m):
        self.dismiss(m.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class CommandPanel(Widget):
    """Displays items from a command returning JSON and provides key-driven actions."""
    DEFAULT_CSS = """
    CommandPanel { height: auto; min-height: 8; }
    .panel-hint { height: auto; color: $text-muted; }
    .panel-table { height: 10; }
    .panel-message { height: auto; color: #e0a040; }
    """

    def __init__(self, list_argv, columns, actions=None, id=None, classes=None, new=None):
        super().__init__(id=id, classes=classes)
        self.list_argv = list(list_argv)
        self.columns = tuple(columns)
        self.actions = actions or {}
        self.new = new   # (thing, text) for a first "+ New <thing>" row naming the command that creates one
        self.items = []
        if new:
            self._bindings.bind("y", "copy", "copy")
        for key, (label, _, _) in self.actions.items():
            self._bindings.bind(key, f"cmd_{key}", label)

    def compose(self):
        hint = " · ".join(f"{k} {label}" for k, (label, _, _) in self.actions.items())
        yield Static(Text(hint), classes="panel-hint", id="panel-hint")
        yield Static("", classes="panel-message", id="panel-message")
        yield DataTable(cursor_type="row", zebra_stripes=True, classes="panel-table", id="panel-table")

    def on_mount(self):
        table = self.query_one("#panel-table", DataTable)
        table.add_columns(*self.columns)
        self.refresh_items()

    def refresh_items(self):
        def run():
            try:
                res = _run(self.list_argv, capture_output=True, text=True)
            except Exception as e:
                self.app.call_from_thread(self._fill, [])
                self.app.call_from_thread(self._show_error, str(e))
                return
            if res.returncode == 2 and ("invalid choice" in (res.stderr or "") or "invalid choice" in (res.stdout or "")):
                cmd = self.list_argv[1] if len(self.list_argv) > 1 and self.list_argv[0] == "pl" else self.list_argv[0]
                self.app.call_from_thread(self._show_unavailable, f"pl {cmd} is not available in this pl")
                return
            if res.returncode != 0:
                lines = (res.stderr or res.stdout or "").strip().splitlines()
                self.app.call_from_thread(self._fill, [])
                self.app.call_from_thread(self._show_error, lines[-1] if lines else f"exit {res.returncode}")
                return
            try:
                data = json.loads(res.stdout) if res.stdout else []
                items = (data.get("items") or next((v for v in data.values() if isinstance(v, list)), [])) if isinstance(data, dict) else data
            except Exception:
                items = []
            self.app.call_from_thread(self._fill, items)

        if hasattr(self, "run_worker"):
            self.run_worker(run, thread=True, exclusive=True)
        else:
            run()

    def _show_unavailable(self, msg):
        self.query_one("#panel-message", Static).update(Text(msg, style="bold #e0a040"))
        self.query_one("#panel-table", DataTable).display = False

    def _show_error(self, err):
        self.query_one("#panel-message", Static).update(Text(err, style="bold red"))

    def _fill(self, items):
        self.items = items if isinstance(items, list) else []
        self.query_one("#panel-message", Static).update("")
        table = self.query_one("#panel-table", DataTable)
        table.display = True
        table.clear()
        if self.new:
            thing, text = self.new
            table.add_row(new_row(thing), Text(text, style="dim"), *[Text("")] * (len(self.columns) - 2), key=NEW_ROW)
        for i, item in enumerate(self.items):
            if isinstance(item, dict):
                row = [Text(str(item.get(col, ""))) for col in self.columns]
            else:
                row = [Text(str(item))] + [Text("")] * (len(self.columns) - 1)
            table.add_row(*row, key=str(i))

    def _on_new_row(self):
        return bool(self.new) and self.query_one("#panel-table", DataTable).cursor_row == 0

    def _selected_item(self):
        table = self.query_one("#panel-table", DataTable)
        i = table.cursor_row - (1 if self.new else 0)
        if table.row_count and 0 <= i < len(self.items) and not self._on_new_row():
            return self.items[i]
        return None

    def action_copy(self):
        if self._on_new_row():
            copy_text(self.app, self.new[1])
            self.app.notify("copied", markup=False)

    def trigger_action(self, key):
        if key not in self.actions or self._on_new_row():
            return
        label, argv_template, confirm = self.actions[key]
        placeholders = [tok for arg in argv_template for tok in re.findall(r"\{(\w+)\}", arg)]

        if placeholders:
            item = self._selected_item() or {}
            ph = placeholders[0]
            initial = str(item.get(ph, "")) if ph in item else ""

            def asked(val):
                if val:
                    self.execute_action(key, val)

            self.app.push_screen(InputModal(f"{label} ({ph}):", placeholder=initial), asked)
            return

        if confirm:
            def confirmed(yes):
                if yes:
                    self.execute_action(key)
            self.app.push_screen(ConfirmScreen(f"Run {label}?"), confirmed)
            return

        self.execute_action(key)

    def execute_action(self, key, value=None):
        if key not in self.actions:
            return
        label, argv_template, confirm = self.actions[key]
        argv = []
        for arg in argv_template:
            if value is not None and "{" in arg and "}" in arg:
                argv.append(re.sub(r"\{\w+\}", str(value), arg))
            else:
                argv.append(arg)
        if confirm and "--yes" not in argv:
            argv.append("--yes")

        def run():
            try:
                res = _run(argv, capture_output=True, text=True)
                lines = (res.stdout.strip() or res.stderr.strip() or "done").splitlines()
                last_line = lines[-1] if lines else "done"
            except Exception as e:
                last_line = str(e)
            self.app.call_from_thread(self.app.notify, last_line, markup=False)
            self.app.call_from_thread(self.refresh_items)

        if hasattr(self, "run_worker"):
            self.run_worker(run, thread=True)
        else:
            run()

    def on_key(self, event):
        if event.key == "y" and self.new:
            self.action_copy()
            event.stop()
        elif event.key in self.actions:
            self.trigger_action(event.key)
            event.stop()

    def on_data_table_row_selected(self, event):
        if "enter" in self.actions:
            self.trigger_action("enter")
            event.stop()


def make_pods_panel():
    return CommandPanel(
        list_argv=["pl", "pods", "list", "--json"],
        columns=("name", "rule", "status"),
        actions={
            "r": ("rule add", ["pl", "pods", "rule", "add", "{rule}"], False),
            "enter": ("show", ["pl", "pods", "show", "{name}"], False),
        },
        new=("pod", "add [pods.NAME] to config.toml"),
    )


def make_triggers_panel():
    return CommandPanel(
        list_argv=["pl", "triggers", "list", "--json"],
        columns=("name", "pipeline", "every", "paused"),
        new=("trigger", "pl triggers add NAME --connector C --query Q --every E --pipeline P"),
    )


def make_models_panel():
    return CommandPanel(
        list_argv=["pl", "models", "list", "--json"],
        columns=("name", "status"),
        actions={
            "t": ("test", ["pl", "models", "test", "{name}"], False),
            "s": ("set", ["pl", "models", "set", "{name}"], False),
        },
    )


def make_chat_panel():
    return CommandPanel(
        list_argv=["pl", "chat", "list", "--json"],
        columns=("channel", "route", "status"),
        actions={
            "a": ("add", ["pl", "chat", "add", "{channel}"], False),
            "t": ("test", ["pl", "chat", "test", "{name}"], False),
            "o": ("route", ["pl", "chat", "route", "{route}"], False),
        },
    )


def make_connectors_panel():
    return CommandPanel(
        list_argv=["pl", "connectors", "list", "--json"],
        columns=("name", "status", "expires"),
        actions={
            "n": ("add", ["pl", "connectors", "add", "{name}"], False),
        },
    )


PANELS = {
    "pods": make_pods_panel,
    "triggers": make_triggers_panel,
    "models": make_models_panel,
    "chat": make_chat_panel,
    "connectors": make_connectors_panel,
}
