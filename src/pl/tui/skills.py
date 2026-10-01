"""Settings section "Skills": every account's skills, agents and commands, a read-only Markdown preview, an in-app
editor (backup + diff confirm before saving), new from a template, delete to .pl-trash, and the shared library."""
import os
import shlex
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Markdown, Select, Static, TextArea

from pl import skills
from pl.tui.extensions import TargetScreen
from pl.tui.review import ConfirmScreen

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it
HINT = ("e edit · E $EDITOR · n new · d delete (to .pl-trash) · S share to the library · l link a library skill "
        "to an account · plugin files are read-only")
DIFF_LINES = 30


class EditorScreen(ModalScreen):
    """A TextArea over one file; ctrl+s shows the diff and saves (with a backup) only after a yes."""
    DEFAULT_CSS = """
    EditorScreen { align: center middle; }
    EditorScreen > Vertical { width: 95%; height: 90%; border: round $accent; background: $surface; }
    #skills-editor { height: 1fr; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "save"), Binding("escape", "cancel", "cancel")]

    def __init__(self, item, text):
        super().__init__()
        self.item, self.text = item, text

    def compose(self):
        with Vertical():
            ed = TextArea(self.text, id="skills-editor")
            ed.border_title = f"{self.item['path']} · ctrl+s save · esc cancel"
            yield ed

    def on_mount(self):
        self.query_one(TextArea).focus()

    def action_save(self):
        new = self.query_one(TextArea).text
        if new == self.text:
            self.app.notify("no changes", markup=False)
            return
        lines = skills.diff(self.text, new, os.path.basename(self.item["path"])).splitlines()
        shown = "\n".join(lines[:DIFF_LINES]) + (f"\n… {len(lines) - DIFF_LINES} more lines" if len(lines) > DIFF_LINES else "")

        def answered(yes):
            if not yes:
                return
            try:
                bak = skills.save(self.item, new, expect=self.text)
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.notify(str(e), severity="error", markup=False)
                return
            self.app.notify(f"saved; the old text is in {bak}", markup=False)
            self.dismiss(True)
        self.app.push_screen(ConfirmScreen(f"Save {self.item['path']}? A backup is kept as <file>.bak-<time>.\n\n{shown}"),
                             answered)

    def action_cancel(self):
        if self.query_one(TextArea).text == self.text:
            self.dismiss(False)
            return
        self.app.push_screen(ConfirmScreen("Discard your unsaved edits?"), lambda yes: yes and self.dismiss(False))


class NewScreen(ModalScreen):
    """Name, account and kind of a new skill, agent or command."""
    DEFAULT_CSS = """
    NewScreen { align: center middle; }
    NewScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, accounts, account):
        super().__init__()
        self.accounts, self.account = accounts, account

    def compose(self):
        with Vertical():
            yield Static(Text("New skill, agent or command: name (lowercase letters, digits, dashes), account, kind"))
            yield Input(placeholder="my-skill", id="skills-new-name")
            yield Select([(a, a) for a in self.accounts], value=self.account, allow_blank=False, compact=True,
                         id="skills-new-account")
            yield Select([(k, k) for k in skills.KINDS], value="skill", allow_blank=False, compact=True, id="skills-new-kind")
            yield Static(Text("enter create · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.dismiss((m.value.strip(), self.query_one("#skills-new-account", Select).value,
                      self.query_one("#skills-new-kind", Select).value))

    def action_cancel(self):
        self.dismiss(None)


class SkillsView(Widget):
    DEFAULT_CSS = """
    SkillsView { height: 26; }
    #skills-hint { height: auto; color: $text-muted; }
    #skills-table { width: 3fr; height: 1fr; }
    #skills-preview-box { width: 2fr; height: 1fr; border: round $primary; }
    """
    BINDINGS = [Binding("e", "edit", "edit"), Binding("E", "external_edit", "$EDITOR"), Binding("n", "new", "new"),
                Binding("d", "delete", "delete"), Binding("S", "share", "share to library"),
                Binding("l", "link", "link to account")]

    def __init__(self):
        super().__init__()
        self.items = []

    def compose(self):
        yield Static(Text(HINT), id="skills-hint")
        with Horizontal():
            yield DataTable(cursor_type="row", id="skills-table", zebra_stripes=True)
            with VerticalScroll(id="skills-preview-box"):
                yield Markdown("", id="skills-preview")

    def on_mount(self):
        self.query_one(DataTable).add_columns("name", "account", "kind", "origin", "library", "description")
        self.rescan()

    def rescan(self):
        def run():
            try:
                items = skills.scan()
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, str(e), severity="error", markup=False)
                return
            self.app.call_from_thread(self._fill, items)
        self.run_worker(run, thread=True, group="skills-scan", exclusive=True)

    def _fill(self, items):
        t = self.query_one(DataTable)
        keep = t.cursor_row
        t.clear()
        self.items = items
        for i, it in enumerate(items):
            lib = ", ".join(it["linked"]) if it["account"] == skills.LIBRARY else ("library" if it["library"] else "")
            t.add_row(Text(it["name"]), Text(it["account"]), Text(it["kind"]), Text(it["origin"]), Text(lib),
                      Text(it["description"][:80]), key=str(i))
        if items:
            t.move_cursor(row=min(keep, len(items) - 1))
        self._preview()

    def _item(self):
        t = self.query_one(DataTable)
        return self.items[t.cursor_row] if t.row_count and t.cursor_row < len(self.items) else None

    def _preview(self):
        it = self._item()

        def run():
            try:
                text = skills.read(it) if it else "*nothing selected*"
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a worker that raises kills the app
                text = f"*{e}*"
            self.app.call_from_thread(self._show, it, text)
        self.run_worker(run, thread=True, group="skills-preview", exclusive=True)

    def _show(self, it, text):
        if it is self._item():        # a newer row is selected: its own read follows
            self.query_one("#skills-preview", Markdown).update(text)

    def on_data_table_row_highlighted(self, _):
        self._preview()

    def focus_table(self):
        t = self.query_one(DataTable)
        t.focus()
        self.scroll_visible()

    # ---------- actions ----------

    def _say(self, e, severity="error"):
        self.app.notify(str(e), severity=severity, markup=False)

    def _editable(self):
        it = self._item()
        if it is None:
            self._say("nothing selected", "information")
            return None, None
        try:
            return it, skills.check_editable(it)
        except SystemExit as e:
            self._say(e, "warning")
            return None, None

    def open_editor(self, it):
        try:
            text = skills.edit_text(it)
        except (SystemExit, Exception) as e:  # noqa: BLE001
            self._say(e)
            return
        self.app.push_screen(EditorScreen(it, text), lambda saved: saved and self.rescan())

    def action_edit(self):
        it, _ = self._editable()
        if it:
            self.open_editor(it)

    def action_external_edit(self):
        it, path = self._editable()
        if not it:
            return
        try:
            with self.app.suspend():
                _run([*shlex.split(os.environ.get("EDITOR") or "vi"), str(path)])
        except (SystemExit, Exception) as e:  # noqa: BLE001
            self._say(e)
        self.rescan()

    def action_new(self):
        accounts = [a for a, _ in skills.sources()] + [skills.LIBRARY]
        cur = self._item()
        account = cur["account"] if cur and cur["account"] in accounts else accounts[0]

        def asked(answer):
            if not answer:
                return
            name, acct, kind = answer
            try:
                path = skills.create(acct, kind, name)
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self._say(e)
                return
            root = str(skills._root(acct))
            self.rescan()
            self.open_editor({"name": name, "account": acct, "kind": kind, "path": str(path), "root": root, "origin": ""})
        self.app.push_screen(NewScreen(accounts, account), asked)

    def action_delete(self):
        it, _ = self._editable()
        if not it:
            return
        what = "folder" if it["kind"] == "skill" else "file"

        def answered(yes):
            if not yes:
                return
            try:
                dest = skills.trash(it)
                self._say(f"moved to {dest}", "information")
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self._say(e)
            self.rescan()
        self.app.push_screen(ConfirmScreen(f"Move the {it['kind']} {it['name']} of {it['account']} to .pl-trash? "
                                           f"The {what} goes to {it['root']}/.pl-trash; nothing is deleted. Links to it "
                                           f"from accounts of other profiles and /pl:{it['name']} prompts will break."),
                             answered)

    def action_share(self):
        it = self._item()
        if it is None or it["kind"] != "skill" or it["account"] in (skills.LIBRARY,) or it["library"]:
            self._say("select one of an account's own skills to share it", "information")
            return

        def answered(yes):
            if not yes:
                return
            try:
                self._say(f"shared: {skills.share(it['account'], it['name'])}; a link is left in its place", "information")
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self._say(e)
            self.rescan()
        self.app.push_screen(ConfirmScreen(f"Move {it['name']} of {it['account']} into the skills library "
                                           f"({skills.library()}) and leave a link in its place? Other accounts can "
                                           f"then link it with l."), answered)

    def action_link(self):
        it = self._item()
        if it is None or it["account"] != skills.LIBRARY:
            self._say("select a library skill (account: library) to link it; S shares a skill into the library",
                      "information")
            return
        targets = [a for a, _ in skills.sources() if a not in it["linked"]]
        if not targets:
            self._say("every account links it already", "information")
            return

        def chosen(account):
            if not account:
                return
            try:
                self._say(f"linked: {skills.link(it['name'], account)}", "information")
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self._say(e)
            self.rescan()
        self.app.push_screen(TargetScreen(it["name"], targets, verb="Link"), chosen)

