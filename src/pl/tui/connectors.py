import json
import os
import subprocess
import time
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from pl import chat, util
from pl.tui.chatform import ChatAdapterPicker, ChatConnectorForm
from pl.tui.chrome import NEW_ROW, new_row
from pl.tui.review import ConfirmScreen

_run = subprocess.run  # the one subprocess runner here; tests fake it
HINT = "n new connector · t test · R revoke (t works on chat rows too)"
NEW_CHAT = "new-chat"
CHAT_NOTE = "Chat connectors send approvals, alerts and the digest to Telegram, Slack or Discord."


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
    #chat-title { height: auto; margin-top: 1; text-style: bold; }
    #chat-note { height: auto; color: $text-muted; }
    #chat-table { height: auto; max-height: 8; }
    """
    BINDINGS = [
        Binding("n", "new", "new connector"),
        Binding("t", "test", "test"),
        Binding("R", "revoke", "revoke"),
    ]

    def __init__(self):
        super().__init__()
        self.items = []
        self.chat = {}
        self.chat_tests = {}

    def compose(self):
        yield Static(Text(HINT), id="connectors-hint")
        yield Static("", id="connectors-notice")
        yield DataTable(cursor_type="row", id="connectors-table", zebra_stripes=True)
        yield Static("Chat connectors · shared by all profiles on this machine", id="chat-title")
        yield Static("", id="chat-note")
        yield DataTable(cursor_type="row", id="chat-table", zebra_stripes=True)

    def on_mount(self):
        self.query_one("#connectors-table", DataTable).add_columns(
            "name", "role", "teams", "access", "status", "expiry", "tier warning")
        self.query_one("#chat-table", DataTable).add_columns("name", "adapter", "allowed", "routes", "last test")
        self.rescan()
        self.rescan_chat()

    def rescan_chat(self):
        def run():
            conns = {}
            try:
                r = _run(["pl", "chat", "list", "--json"], capture_output=True, text=True)
                data = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
                found = data.get("connectors") if isinstance(data, dict) else None
                conns = found if isinstance(found, dict) else {}
            except Exception:
                conns = {}
            self.app.call_from_thread(self._fill_chat, conns)

        self.run_worker(run, thread=True, group="chat-scan", exclusive=True)

    def _fill_chat(self, conns):
        self.chat = {n: c for n, c in conns.items() if isinstance(c, dict)}
        table = self.query_one("#chat-table", DataTable)
        table.clear()
        self.query_one("#chat-note", Static).update(Text("" if self.chat else CHAT_NOTE, style="dim"))
        if not self.chat:
            table.add_row(new_row("chat connector"), *[""] * 4, key=NEW_CHAT)
            return
        for name, cfg in self.chat.items():
            result = self.chat_tests.get(name, "not tested")
            style = {"pass": "green", "fail": "red"}.get(result, "dim")
            table.add_row(
                Text(util.mask(name)),
                Text(str(cfg.get("adapter", ""))),
                Text(", ".join(str(a) for a in cfg.get("allow") or [])),
                Text(", ".join(cfg.get("routes") or ["gates"])),
                Text(result, style=style),
                key="chat:" + name,
            )

    def _selected_chat(self):
        t = self.query_one("#chat-table", DataTable)
        if not self.chat or not 0 <= t.cursor_row < len(self.chat):
            return None
        return list(self.chat)[t.cursor_row]

    def _chat_test(self, name):
        cfg = self.chat[name]
        m = chat.VAR_RE.match(str(cfg.get("token", "")))
        cmd = f"pl chat test {name}"
        if m and m.group(1) not in os.environ:
            var = m.group(1)
            self.chat_tests[name] = "fail"
            self._fill_chat(self.chat)
            self.app.notify(f"{var} is not set: export {var}=… in the shell that starts pl, then restart pl · {cmd}",
                            severity="error", markup=False)
            return

        def run():
            r = _run(["pl", "chat", "test", name], capture_output=True, text=True)
            self.chat_tests[name] = "pass" if r.returncode == 0 else "fail"
            out = r.stdout.strip() or r.stderr.strip() or f"test finished ({r.returncode})"
            sev = "information" if r.returncode == 0 else "error"
            self.app.call_from_thread(self._fill_chat, self.chat)
            self.app.call_from_thread(self.app.notify, f"{util.mask(out)} · {cmd}", severity=sev, markup=False)

        self.run_worker(run, thread=True, group="connectors-action")

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
        self.query_one("#connectors-table", DataTable).display = False

    def _show_error(self, err):
        self.app.notify(err, severity="error", markup=False)

    def _fill(self, items):
        self.items = items
        notice = self.query_one("#connectors-notice", Static)
        table = self.query_one("#connectors-table", DataTable)
        notice.display = False
        table.display = True
        table.clear()
        table.add_row(new_row("connector"), *[""] * 6, key=NEW_ROW)
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
        t = self.query_one("#connectors-table", DataTable)
        return self.items[t.cursor_row - 1] if 0 < t.cursor_row <= len(self.items) else None

    def on_data_table_row_selected(self, m):
        if m.row_key.value == NEW_CHAT:
            self.new_chat()
        elif m.row_key.value == NEW_ROW:
            self.action_new()

    def new_chat(self):
        def on_form(msg):
            if msg:
                self.rescan_chat()
                self.app.notify(msg, markup=False)

        def on_adapter(adapter):
            if adapter:
                self.app.push_screen(ChatConnectorForm(adapter), on_form)

        self.app.push_screen(ChatAdapterPicker(), on_adapter)

    def action_new(self):
        if self.query_one("#chat-table", DataTable).has_focus:
            self.new_chat()
            return
        self.app.push_screen(AddConnectorScreen(), lambda added: added and self.rescan())

    def action_test(self):
        if self.query_one("#chat-table", DataTable).has_focus:
            name = self._selected_chat()
            if name is None:
                self.app.notify("nothing selected", severity="warning")
            else:
                self._chat_test(name)
            return
        item = self._selected_item()
        if not item:
            self.app.notify("nothing selected", severity="warning")
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
            self.app.notify("nothing selected", severity="warning")
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
