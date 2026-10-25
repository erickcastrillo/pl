import json
import subprocess
import time
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from pl import util
from pl.tui.review import ConfirmScreen

_run = subprocess.run  # the one subprocess runner here; tests fake it
HINT = "n new connector · t test · R revoke"


def _format_expiry(exp):
    if exp is None or exp == "":
        return Text("")
    if isinstance(exp, (int, float)):
        ts = float(exp)
    else:
        ts = util.parse_iso(str(exp))
    if not ts:
        return Text(str(exp))
    diff = int(ts - time.time())
    if diff <= 0:
        past = abs(diff)
        if past < 3600:
            rel = f"{max(1, round(past / 60))}m ago"
        elif past < 86400:
            rel = f"{max(1, round(past / 3600))}h ago"
        else:
            rel = f"{max(1, round(past / 86400))}d ago"
        return Text(f"expired {rel}", style="red")
    if diff < 3600:
        rel = f"{max(1, round(diff / 60))}m"
    elif diff < 86400:
        rel = f"{max(1, round(diff / 3600))}h"
    else:
        rel = f"{max(1, round(diff / 86400))}d"
    return Text(f"in {rel}")


class AddConnectorScreen(ModalScreen):
    DEFAULT_CSS = """
    AddConnectorScreen { align: center middle; }
    AddConnectorScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    #connector-progress { height: auto; max-height: 8; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self):
        with Vertical():
            yield Static(Text("Add OAuth connector: name, URL, team, role"))
            yield Input(placeholder="name (e.g. jira-prod)", id="connector-name")
            yield Input(placeholder="URL (e.g. https://example.test)", id="connector-url")
            yield Input(placeholder="team", id="connector-team")
            yield Input(placeholder="role", id="connector-role")
            yield Static("", id="connector-progress")
            yield Static(Text("enter connect · esc cancel", style="dim"))

    def on_input_submitted(self, _):
        self.action_submit()

    def action_submit(self):
        name = self.query_one("#connector-name", Input).value.strip()
        url = self.query_one("#connector-url", Input).value.strip()
        team = self.query_one("#connector-team", Input).value.strip()
        role = self.query_one("#connector-role", Input).value.strip()
        if not name or not url:
            return

        cmd = ["pl", "connectors", "add", name, url]
        if team:
            cmd.extend(["--team", team])
        if role:
            cmd.extend(["--role", role])
        cmd.append("--yes")

        def run():
            r = _run(cmd, capture_output=True, text=True)
            self.app.call_from_thread(self._finished, r)

        self.run_worker(run, thread=True, group="connector-add")

    def _finished(self, r):
        if r.returncode == 0:
            self.dismiss(True)
        else:
            err = util.mask(r.stderr.strip() or r.stdout.strip() or "failed")
            prog = self.query_one("#connector-progress", Static)
            prog.update(Text(err, style="red"))

    def action_cancel(self):
        self.dismiss(False)


class ConnectorsPanel(Widget):
    DEFAULT_CSS = """
    ConnectorsPanel { height: auto; min-height: 8; }
    #connectors-hint { height: auto; color: $text-muted; }
    #connectors-table { height: auto; max-height: 12; }
    #connectors-notice { height: auto; color: $text-muted; display: none; }
    """
    BINDINGS = [
        Binding("n", "new", "new"),
        Binding("t", "test", "test"),
        Binding("R", "revoke", "revoke"),
    ]

    def __init__(self):
        super().__init__()
        self.items = []

    def compose(self):
        yield Static(Text(HINT), id="connectors-hint")
        yield Static("", id="connectors-notice")
        yield DataTable(cursor_type="row", id="connectors-table", zebra_stripes=True)

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns("name", "role", "teams", "access", "status", "expiry", "tier warning")
        self.rescan()

    def rescan(self):
        def run():
            try:
                r = _run(["pl", "connectors", "list", "--json"], capture_output=True, text=True)
            except Exception as e:
                self.app.call_from_thread(self._show_error, str(e))
                return
            if r.returncode == 2 and "invalid choice" in (r.stderr or ""):
                self.app.call_from_thread(self._show_unavailable)
                return
            try:
                items = json.loads(r.stdout) if r.stdout.strip() else []
                if isinstance(items, dict):   # pl connectors list --json prints {"connectors": [...]}
                    items = items.get("connectors") or []
            except Exception:
                items = []
            self.app.call_from_thread(self._fill, items)

        self.run_worker(run, thread=True, group="connectors-scan", exclusive=True)

    def _show_unavailable(self):
        notice = self.query_one("#connectors-notice", Static)
        notice.update(Text("pl connectors is not available in this pl", style="dim"))
        notice.display = True
        self.query_one(DataTable).display = False

    def _show_error(self, err):
        self.app.notify(err, severity="error", markup=False)

    def _fill(self, items):
        self.items = items
        notice = self.query_one("#connectors-notice", Static)
        table = self.query_one(DataTable)
        notice.display = False
        table.display = True
        table.clear()
        for idx, it in enumerate(items):
            name = util.mask(str(it.get("name") or ""))
            role = util.mask(str(it.get("role") or ""))
            teams = it.get("teams")
            if isinstance(teams, list):
                teams_str = ", ".join(str(x) for x in teams)
            else:
                teams_str = str(teams or "")
            teams_str = util.mask(teams_str)
            access = util.mask(str(it.get("access") or ""))
            status = util.mask(str(it.get("status") or ""))
            status_text = Text(status, style="red" if status.lower() == "expired" else "")
            expiry_text = _format_expiry(it.get("expiry") or it.get("sign_in_expiry") or it.get("expires_at"))
            warn = util.mask(str(it.get("tier_warning") or it.get("warning") or ""))
            warn_text = Text(warn, style="yellow" if warn else "")
            table.add_row(
                Text(name),
                Text(role),
                Text(teams_str),
                Text(access),
                status_text,
                expiry_text,
                warn_text,
                key=str(idx),
            )

    def _selected_item(self):
        t = self.query_one(DataTable)
        if t.row_count and t.cursor_row < len(self.items):
            return self.items[t.cursor_row]
        return None

    def action_new(self):
        self.app.push_screen(AddConnectorScreen(), lambda added: added and self.rescan())

    def action_test(self):
        item = self._selected_item()
        if not item:
            self.app.notify("no connector selected", severity="warning")
            return
        name = item.get("name")

        def run():
            r = _run(["pl", "connectors", "test", name], capture_output=True, text=True)
            msg = r.stdout.strip() or r.stderr.strip() or f"test finished ({r.returncode})"
            sev = "information" if r.returncode == 0 else "error"
            self.app.call_from_thread(self.app.notify, msg, severity=sev)

        self.run_worker(run, thread=True, group="connectors-action")

    def action_revoke(self):
        item = self._selected_item()
        if not item:
            self.app.notify("no connector selected", severity="warning")
            return
        name = item.get("name")

        def confirmed(yes):
            if not yes:
                return

            def run():
                r = _run(["pl", "connectors", "revoke", name, "--yes"], capture_output=True, text=True)
                sev = "information" if r.returncode == 0 else "error"
                msg = r.stdout.strip() or r.stderr.strip() or f"revoked {name}"
                self.app.call_from_thread(self.app.notify, msg, severity=sev)
                self.app.call_from_thread(self.rescan)

            self.run_worker(run, thread=True, group="connectors-action")

        self.app.push_screen(ConfirmScreen(f"Revoke connector {name}?"), confirmed)
