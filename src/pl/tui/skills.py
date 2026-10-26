"""Settings section "Skills": every account's skills, agents and commands, a read-only Markdown preview, an in-app
editor (backup + diff confirm before saving), new from a template, delete to .pl-trash, and the shared library."""
import json
import os
import shlex
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Markdown, Select, Static, TextArea

from pl import config as C
from pl import skill_sources, skills
from pl.tui.extensions import TargetScreen
from pl.tui.review import ConfirmScreen

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it
HINT = ("e edit · E $EDITOR · n new · d delete (to .pl-trash) · S share to the library · l link a library skill "
        "to an account · I install · u update the selected skill (diff first) · plugin files are read-only")
DIFF_LINES = 30


def _origin_label(it, builtin, recs):
    if builtin:
        return "built in"
    rec = recs.get(it["name"])
    if rec and it["account"] == skills.LIBRARY:
        return f"GitHub {rec['repo']}@{rec['sha'][:7]}"
    if it["origin"] == "plugin":
        return "plugin"
    if it["origin"] == "omc":
        return "oh-my-claude"
    return "library link" if it["library"] else "you"


def grouped(items):
    """[(heading, [row, ...])]: the scan items under pl's stage skills, your library and one heading per account.
    Each row is the item plus origin_label and used_by."""
    builtins, recs, used = set(skills.builtin_names()), skill_sources.records(), {}

    def users(it):
        if it["kind"] != "skill":
            return ""
        if it["name"] not in used:
            try:
                used[it["name"]] = ", ".join(skill_sources.users(it["name"]))
            except Exception:  # noqa: BLE001 - a broken pod file must not empty the list
                used[it["name"]] = ""
        return used[it["name"]]

    def row(it):
        is_builtin = it["account"] == skills.LIBRARY and it["name"] in builtins
        return {**it, "origin_label": _origin_label(it, is_builtin, recs), "used_by": users(it)}

    stage = [row(i) for i in items if i["account"] == skills.LIBRARY and i["name"] in builtins]
    mine = [row(i) for i in items if i["account"] == skills.SHARED
            or (i["account"] == skills.LIBRARY and i["name"] not in builtins)]
    groups = [("pl's stage skills", stage), ("Your library", mine)]
    for account, _ in skills.sources():
        if account == skills.SHARED:
            continue
        harness = C.ACCOUNTS.get(account, {}).get("harness", "")
        groups.append((f"{account} ({harness})" if harness else account, [row(i) for i in items if i["account"] == account]))
    return [(h, rs) for h, rs in groups if rs]


def _from_github(it):
    return bool(it) and it["account"] == skills.LIBRARY and it["origin_label"].startswith("GitHub ")


def update_label(it, checks):
    """The row's "update" cell: the check's result for a GitHub-installed skill, empty for every other row."""
    if not _from_github(it):
        return ""
    if checks is None:
        return "checking…"
    r = checks.get(it["name"])
    if not r or r.get("error"):
        return "check failed"
    if r["update"]:
        return f"update available {r['pinned'][:7]} -> {r['latest'][:7]}"
    return "up to date"


def update_block(it, checks):
    """Why Update cannot run on this row; empty when it can."""
    if not it:
        return "nothing selected"
    if not _from_github(it):
        return "not installed from GitHub"
    if checks is None:
        return "checking for updates…"
    r = checks.get(it["name"]) or {}
    error = r.get("error") or checks.get("__error__") or (None if r else "no result for this skill")
    if error:
        return f"check failed: {error}"
    return "" if r["update"] else "up to date"


class PromptScreen(ModalScreen):
    """Asks for a single text input with a prompt."""
    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    PromptScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt, placeholder=""):
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self):
        with Vertical():
            yield Static(Text(self.prompt))
            yield Input(placeholder=self.placeholder, id="prompt-input")
            yield Static(Text("enter submit · esc cancel", style="dim"))

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, m):
        self.dismiss(m.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)



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
    #skills-update-note { height: auto; color: $text-muted; }
    """
    BINDINGS = [Binding("e", "edit", "edit"), Binding("E", "external_edit", "$EDITOR"), Binding("n", "new", "new"),
                Binding("d", "delete", "delete"), Binding("S", "share", "share to library"),
                Binding("l", "link", "link to account"), Binding("I", "install", "install"),
                Binding("u", "update", "update")]

    def __init__(self):
        super().__init__()
        self.items = []
        self.rows = []
        self.groups = []
        self.checks = None

    def compose(self):
        yield Static(Text(HINT), id="skills-hint")
        with Horizontal():
            yield DataTable(cursor_type="row", id="skills-table", zebra_stripes=True)
            with VerticalScroll(id="skills-preview-box"):
                yield Static("", id="skills-update-note")
                yield Markdown("", id="skills-preview")

    def on_mount(self):
        self.query_one(DataTable).add_columns("name", "kind", "origin", "update", "used by", "linked", "description")
        self.rescan()

    def rescan(self):
        def run():
            try:
                groups = grouped(skills.scan())
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, str(e), severity="error", markup=False)
                return
            self.app.call_from_thread(self._scanned, groups)
        self.run_worker(run, thread=True, group="skills-scan", exclusive=True)

    def _scanned(self, groups):
        self.checks = None
        self._fill(groups)
        self._check()

    def _check(self):
        def run():
            r = None
            try:
                r = _run(["pl", "skills", "update", "--check", "--json"], capture_output=True, text=True)
                checks = {c["name"]: c for c in json.loads(r.stdout)}
            except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
                said = ((r and r.stderr) or str(e)).strip().splitlines()
                checks = {"__error__": said[-1] if said else "no output"}
            self.app.call_from_thread(self._set_checks, checks)
        self.run_worker(run, thread=True, group="skills-check", exclusive=True)

    def _set_checks(self, checks):
        self.checks = checks
        self._fill(self.groups)

    def _fill(self, groups):
        self.groups = groups
        t = self.query_one(DataTable)
        keep = t.cursor_row
        t.clear()
        self.rows = []
        for n, (heading, rows) in enumerate(groups):
            t.add_row(Text(heading, style="bold"), "", "", "", "", "", "", key=f"h{n}")
            self.rows.append(None)
            for it in rows:
                lib = ", ".join(it["linked"]) if it["account"] == skills.LIBRARY else ("library" if it["library"] else "")
                label = update_label(it, self.checks)
                style = "bold" if label.startswith("update available") else "red" if label == "check failed" else ""
                t.add_row(Text(it["name"]), Text(it["kind"]), Text(it["origin_label"]), Text(label, style=style),
                          Text(it["used_by"]), Text(lib), Text(it["description"][:80]), key=f"r{len(self.rows)}")
                self.rows.append(it)
        self.items = [r for r in self.rows if r]
        if self.rows:
            row = min(keep, len(self.rows) - 1)
            t.move_cursor(row=row + 1 if self.rows[row] is None and row + 1 < len(self.rows) else row)
        self._preview()

    def _item(self):
        t = self.query_one(DataTable)
        return self.rows[t.cursor_row] if t.row_count and t.cursor_row < len(self.rows) else None

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
            note = (update_block(it, self.checks) or "u: update this skill") if _from_github(it) else ""
            self.query_one("#skills-update-note", Static).update(Text(note))

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

    def action_install(self):
        def asked(source):
            if not source:
                return

            def run():
                try:
                    r = _run(["pl", "skills", "install", source], capture_output=True, text=True)
                    msg = (r.stdout.strip() or r.stderr.strip() or "installed").splitlines()[-1]
                except Exception as e:
                    msg = str(e)
                self.app.call_from_thread(self.app.notify, msg, markup=False)
                self.app.call_from_thread(self.rescan)

            self.run_worker(run, thread=True, group="skills-install")

        self.app.push_screen(PromptScreen("Install skill: <github:...> or owner/repo", placeholder="github:owner/repo"), asked)

    def action_update(self):
        it = self._item()
        why = update_block(it, self.checks)
        if why:
            self._say(why, "information")
            return
        name = it["name"]

        def show_diff():
            r = _run(["pl", "skills", "update", name, "--dry-run"], capture_output=True, text=True)
            if r.returncode != 0:
                self.app.call_from_thread(self._say, (r.stderr.strip() or "update failed").splitlines()[-1])
                return
            *diff, last = r.stdout.strip().splitlines()
            repo, sha = last.rsplit(" ", 1)[-1].rsplit("@", 1)
            shown = "\n".join(diff[:DIFF_LINES]) + (f"\n… {len(diff) - DIFF_LINES} more lines" if len(diff) > DIFF_LINES else "")
            ask = ConfirmScreen(f"{shown}\n\nUpdate {name} to {repo}@{sha[:7]}?")
            self.app.call_from_thread(self.app.push_screen, ask, lambda yes: yes and apply(sha))

        def apply(sha):
            def run():
                r = _run(["pl", "skills", "update", name, "--yes", "--sha", sha], capture_output=True, text=True)
                said = (r.stdout if r.returncode == 0 else r.stderr).strip().splitlines()
                self.app.call_from_thread(self.app.notify, said[-1] if said else "update failed",
                                          severity="information" if r.returncode == 0 else "error", markup=False)
                self.app.call_from_thread(self.rescan)
            self.run_worker(run, thread=True, group="skills-update")

        def guarded():
            try:
                show_diff()
            except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self._say, e)
        self.run_worker(guarded, thread=True, group="skills-update")
