"""Settings section "Skills": every account's skills, agents and commands, a read-only Markdown preview, an in-app
editor (backup + diff confirm before saving), new from a template, delete to .pl-trash, and the shared library."""
import io
import os
import re
import shlex
import subprocess
from itertools import takewhile

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Markdown, Static, TextArea

from pl import config as C
from pl import skill_sources, skills
from pl.tui.extensions import TargetScreen
from pl.tui.review import ConfirmScreen

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it
HINT = ("e edit · E $EDITOR · n new (install or write) · d remove (a skill: its users are shown first) · S share to the library · l link a library skill "
        "to an account · I install from GitHub · u update check · plugin files are read-only")
DIFF_LINES = 30
PINNED_RE = re.compile(r"^Source: (github:\S+@[0-9a-f]{40})$", re.M)
INSTALL_PROMPT = "Install from GitHub: github:owner/repo[/path][@ref] or a github.com URL"


def pinned_source(stdout):
    m = PINNED_RE.search(stdout)
    return m.group(1) if m else None


def review_title(stdout, pinned):
    repo, sha = pinned.removeprefix("github:").rsplit("@", 1)
    where = f"{'/'.join(repo.split('/')[:2])}@{sha[:7]}"
    plugin = re.search(r"^Plugin (\S+)", stdout, re.M)
    if not plugin:
        return f"Install from {where}?"
    listed = stdout.split("Skills (installed):", 1)[-1].split("\n")[1:]
    count = len(list(takewhile(lambda ln: ln.startswith(" "), listed)))
    return f"Install plugin {plugin.group(1)} ({count} skills) from {where}?"


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


def _users_line(name):
    """(sentence for the confirmation, the users it found)."""
    try:
        used = skill_sources.users(name)
    except Exception as e:  # noqa: BLE001 - a broken pod file must not stop the person removing a skill
        return f"Could not check which stages, loops or pods use it: {e}.", []
    if used:
        return f"Used by: {', '.join(used)}.", used
    return "Not used by any stage, loop or pod.", []


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


class NewChoiceScreen(ModalScreen):
    """Install a skill from GitHub or write a new one."""
    DEFAULT_CSS = """
    NewChoiceScreen { align: center middle; }
    NewChoiceScreen > Vertical { width: 50; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    NewChoiceScreen Button { width: 100%; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self):
        with Vertical():
            yield Static("New skill")
            yield Button("Install from GitHub", id="skills-choose-install")
            yield Button("Write a new one", id="skills-choose-write")
            yield Static(Text("esc cancel", style="dim"))

    def on_mount(self):
        self.query_one("#skills-choose-install").focus()

    def on_button_pressed(self, event):
        self.dismiss("install" if event.button.id == "skills-choose-install" else "write")

    def action_cancel(self):
        self.dismiss(None)


class InstallReviewScreen(ModalScreen):
    """The dry run of an install, shown in full; Install runs the pinned commit, Cancel is focused."""
    DEFAULT_CSS = """
    InstallReviewScreen { align: center middle; }
    InstallReviewScreen > Vertical { width: 90; height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    InstallReviewScreen VerticalScroll { height: 1fr; }
    InstallReviewScreen Horizontal { height: auto; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title, listing):
        super().__init__()
        self.title_text, self.listing = title, listing

    def compose(self):
        with Vertical():
            yield Static(Text(self.title_text, style="bold"))
            with VerticalScroll():
                yield Static(Text(self.listing))
            with Horizontal():
                yield Button("Install", id="skills-review-install", variant="warning")
                yield Button("Cancel", id="skills-review-cancel")

    def on_mount(self):
        self.query_one("#skills-review-cancel").focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "skills-review-install")

    def action_cancel(self):
        self.dismiss(False)


class NewScreen(ModalScreen):
    """Name and one-line description of a new skill in the library."""
    DEFAULT_CSS = """
    NewScreen { align: center middle; }
    NewScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self):
        with Vertical():
            yield Static(Text("New skill in your library: name (lowercase letters, digits, dashes) and when to use it"))
            yield Input(placeholder="my-skill", id="skills-new-name")
            yield Input(placeholder="Say in one sentence when to use this skill.", id="skills-new-description")
            yield Static("", id="skills-new-error")
            yield Static(Text("enter create · esc cancel", style="dim"))

    def on_mount(self):
        self.query_one("#skills-new-name").focus()

    def on_input_submitted(self, m):
        name = self.query_one("#skills-new-name", Input).value.strip()
        description = self.query_one("#skills-new-description", Input).value
        if m.input.id == "skills-new-name" and not description.strip():
            self.query_one("#skills-new-description").focus()
            return
        try:
            path = skills.create(skills.LIBRARY, "skill", name, description)
        except SystemExit as e:
            self.query_one("#skills-new-error", Static).update(Text(str(e).removeprefix("pl: "), style="red"))
            return
        self.dismiss((name, path))

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
                Binding("d", "delete", "remove"), Binding("S", "share", "share to library"),
                Binding("l", "link", "link to account"), Binding("I", "install", "install"),
                Binding("u", "update", "update check")]

    def __init__(self):
        super().__init__()
        self.items = []
        self.rows = []

    def compose(self):
        yield Static(Text(HINT), id="skills-hint")
        with Horizontal():
            yield DataTable(cursor_type="row", id="skills-table", zebra_stripes=True)
            with VerticalScroll(id="skills-preview-box"):
                yield Markdown("", id="skills-preview")

    def on_mount(self):
        self.query_one(DataTable).add_columns("name", "kind", "origin", "used by", "linked", "description")
        self.rescan()

    def rescan(self):
        def run():
            try:
                groups = grouped(skills.scan())
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, str(e), severity="error", markup=False)
                return
            self.app.call_from_thread(self._fill, groups)
        self.run_worker(run, thread=True, group="skills-scan", exclusive=True)

    def _fill(self, groups):
        t = self.query_one(DataTable)
        keep = t.cursor_row
        t.clear()
        self.rows = []
        for n, (heading, rows) in enumerate(groups):
            t.add_row(Text(heading, style="bold"), "", "", "", "", "", key=f"h{n}")
            self.rows.append(None)
            for it in rows:
                lib = ", ".join(it["linked"]) if it["account"] == skills.LIBRARY else ("library" if it["library"] else "")
                t.add_row(Text(it["name"]), Text(it["kind"]), Text(it["origin_label"]), Text(it["used_by"]), Text(lib),
                          Text(it["description"][:80]), key=f"r{len(self.rows)}")
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
        def picked(choice):
            if choice == "install":
                self.action_install()
            elif choice == "write":
                self.app.push_screen(NewScreen(), written)

        def written(made):
            if not made:
                return
            name, path = made
            self.rescan()
            self.open_editor({"name": name, "account": skills.LIBRARY, "kind": "skill", "path": str(path),
                              "root": str(skills._root(skills.LIBRARY)), "origin": ""})
        self.app.push_screen(NewChoiceScreen(), picked)

    def action_delete(self):
        it = self._item()
        if it is None:
            self._say("nothing selected", "information")
            return
        is_library = it["account"] == skills.LIBRARY
        if is_library and it["name"] in skills.builtin_names():
            self._say("pl's built-in stage skills are not removed; use pl skills reset <name> to restore one", "warning")
            return
        try:
            skills.check_editable(it)
        except SystemExit as e:
            self._say(e, "warning")
            return
        line, used = _users_line(it["name"]) if it["kind"] == "skill" else ("", [])
        if is_library and it["kind"] == "skill" and it["name"] in skill_sources.records():
            self._confirm_library_removal(it, line, used)
            return
        if is_library and it.get("linked"):
            self._say(f"{it['name']} is still linked from {', '.join(it['linked'])}; unlink it there first", "warning")
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
                                           f"from accounts of other profiles will break. {line}".strip()),
                             answered)

    def _confirm_library_removal(self, it, line, used):
        name, lib = it["name"], skills.library()

        def answered(yes):
            if not yes:
                return
            try:
                if skill_sources.remove(name, True, io.StringIO(), used=used) == 0:
                    self._say(f"removed {name}", "information")
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self._say(e)
            self.rescan()
        self.app.push_screen(ConfirmScreen(f"Remove {name} from {lib}? It is unlinked from every account and moved to "
                                           f"{lib}/.pl-trash; its install record is dropped. {line}"), answered)

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

    def _notify_from(self, msg, severity="information"):
        self.app.call_from_thread(self.app.notify, msg, severity=severity, markup=False)

    def action_install(self):
        def asked(source):
            if source:
                self.run_worker(lambda: dry_run(source), thread=True, group="skills-install")

        def dry_run(source):
            try:
                r = _run(["pl", "skills", "install", source, "--dry-run"], capture_output=True, text=True)
            except Exception as e:  # noqa: BLE001
                return self._notify_from(str(e), "error")
            pinned = pinned_source(r.stdout)
            if r.returncode != 0 or not pinned:
                msg = r.stderr.strip() if r.returncode != 0 and r.stderr.strip() else "the dry run named no commit; nothing installed"
                return self._notify_from(msg, "error")
            review = InstallReviewScreen(review_title(r.stdout, pinned), r.stdout)
            self.app.call_from_thread(self.app.push_screen, review, lambda yes: reviewed(yes, pinned))

        def reviewed(yes, pinned):
            if not yes:
                self.app.notify("nothing installed", markup=False)
                return
            self.run_worker(lambda: install(pinned), thread=True, group="skills-install")

        def install(pinned):
            try:
                r = _run(["pl", "skills", "install", pinned, "--yes"], capture_output=True, text=True)
                msg = (r.stdout.strip() or r.stderr.strip() or "installed").splitlines()[-1]
                severity = "error" if r.returncode != 0 else "information"
            except Exception as e:  # noqa: BLE001
                msg, severity = str(e), "error"
            self._notify_from(msg, severity)
            self.app.call_from_thread(self.rescan)

        self.app.push_screen(PromptScreen(INSTALL_PROMPT, placeholder="github:owner/repo"), asked)

    def action_update(self):
        def run():
            try:
                r = _run(["pl", "skills", "update", "--check"], capture_output=True, text=True)
                msg = (r.stdout.strip() or r.stderr.strip() or "updated").splitlines()[-1]
            except Exception as e:
                msg = str(e)
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.rescan)

        self.run_worker(run, thread=True, group="skills-update")


