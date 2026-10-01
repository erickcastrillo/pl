"""Settings sub-section: a harness account's skills, slash commands and hooks.
Everything read or written lives under the account's config dir (skills/, commands/ or prompts/, settings.json);
links that leave it are refused, nothing is overwritten, credential files are never opened."""
import os
import re
import shlex
import shutil
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from pl import config as C
from pl import harnesses

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
KINDS = ("skills", "commands", "hooks")
SKILL_TEMPLATE = "---\nname: {name}\ndescription: Describe when to use this skill.\n---\n\nInstructions for {name}.\n"
COMMAND_TEMPLATE = "Describe what /{name} should do.\n"


def _unsupported(h, kind):
    return SystemExit(f"pl: {kind} are not supported by {h.name} yet")


def _base(h, account, kind):
    """The skills/ or commands/ dir of an account, created when absent; refused when it resolves outside the config dir."""
    if kind not in ("skills", "commands"):
        raise SystemExit(f"pl: {kind} cannot be created or copied here")
    if not harnesses.supported(h)[kind]:
        raise _unsupported(h, kind)
    root = harnesses.config_dir(account)
    if root is None or not root.is_dir():
        raise SystemExit(f"pl: account {account!r} has no config dir")
    dname = "skills" if kind == "skills" else harnesses.COMMAND_DIR[h.name]
    base = root / dname
    if os.path.lexists(base):
        if harnesses.real_path(root, base, dname) is None:
            raise SystemExit(f"pl: refused: {base} is not a {dname}/ dir of an account")
    else:
        base.mkdir()
    return base


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SystemExit(f"pl: {name!r} is not a valid name: use lowercase letters, digits and dashes, starting with a letter or digit")


def _dest(base, kind, name):
    dest = base / (name if kind == "skills" else f"{name}.md")
    if os.path.lexists(dest):
        raise SystemExit(f"pl: {dest} already exists; pl never overwrites")
    return dest


def create(h, account, kind, name):
    """Write a minimal skill folder or command file; returns the new file's path."""
    _check_name(name)
    dest = _dest(_base(h, account, kind), kind, name)
    if kind == "skills":
        dest.mkdir()
        dest /= "SKILL.md"
    with open(dest, "x", encoding="utf-8") as f:
        f.write((SKILL_TEMPLATE if kind == "skills" else COMMAND_TEMPLATE).format(name=name))
    return dest


def copy(h, account, kind, name, target):
    """Copy a skill folder (links kept as links) or a command file to another account of the same harness."""
    if kind == "hooks":
        raise SystemExit("pl: hooks are not copied")
    if target == account:
        raise SystemExit("pl: the target is the same account")
    if target not in C.PROFILES:
        raise SystemExit(f"pl: unknown account {target!r}")
    th = harnesses.account_harness(target)
    if not harnesses.supported(th)[kind]:
        raise _unsupported(th, kind)
    if th.name != h.name:
        raise SystemExit(f"pl: {target} uses {th.name}, not {h.name}; copy stays within one harness")
    _check_name(name)
    root = harnesses.config_dir(account)
    src = root / ("skills" if kind == "skills" else harnesses.COMMAND_DIR[h.name]) / (name if kind == "skills" else f"{name}.md")
    shape = "skill" if kind == "skills" else f"command:{harnesses.COMMAND_DIR[h.name]}"
    if root is None or harnesses.real_path(root, src, shape) is None or not (src.is_dir() if kind == "skills" else src.is_file()):
        raise SystemExit(f"pl: {account} has no {kind[:-1]} {name!r}")
    if kind == "skills":
        _check_tree(root, src)
    dest = _dest(_base(th, target, kind), kind, name)
    if kind == "skills":
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)
    return dest


def _check_tree(root, src):
    """Refuse a skill folder holding a credential file (by name, any case, or a hard link to one), or a link to one
    or to a config dir or an ancestor of one (stat only, nothing opened)."""
    for d, dirs, files in os.walk(src.resolve()):
        for n in dirs + files:
            p = os.path.join(d, n)
            bad = harnesses.SECRET_RE.fullmatch(n)
            if not bad and os.path.islink(p):
                r = os.path.realpath(p)
                bad = harnesses.SECRET_RE.fullmatch(os.path.basename(r)) or harnesses.covers_root(root, r)
            if not bad and os.path.isfile(p):
                bad = harnesses.is_secret_file(root, os.stat(p))
            if bad:
                raise SystemExit(f"pl: refused: {p} is or points at a credential file or a config dir")


def missing_skill(prompt, h, account):
    """The `/name` a stage prompt starts with when the account has no such skill or command, else None."""
    m = re.match(r"\s*/([a-z0-9][a-z0-9-]*)(?=\s|$)", prompt or "")
    if not m or not harnesses.supported(h)["skills"]:
        return None
    ext = harnesses.extensions(h, account)
    if ext["error"] and not ext["skills"] and not ext["commands"]:
        return None
    return None if m.group(1) in {i["name"] for i in ext["skills"] + ext["commands"]} else m.group(1)


class NameScreen(ModalScreen):
    DEFAULT_CSS = """
    NameScreen { align: center middle; }
    NameScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, what):
        super().__init__()
        self.what = what

    def compose(self):
        with Vertical():
            yield Static(Text(f"Name of the new {self.what} (lowercase letters, digits, dashes)"))
            yield Input(placeholder="my-name", id="ext-name")
            yield Static(Text("enter create · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.dismiss(m.value.strip())

    def action_cancel(self):
        self.dismiss(None)


class TargetScreen(ModalScreen):
    DEFAULT_CSS = """
    TargetScreen { align: center middle; }
    TargetScreen > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, what, accounts, verb="Copy"):
        super().__init__()
        self.what, self.accounts, self.verb = what, accounts, verb

    def compose(self):
        with Vertical():
            yield Static(Text(f"{self.verb} {self.what} to which account?"))
            yield OptionList(*[Option(a, id=a) for a in self.accounts], id="ext-target")
            yield Static(Text(f"enter {self.verb.lower()} · esc cancel", style="dim"))

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, m):
        self.dismiss(m.option.id)

    def action_cancel(self):
        self.dismiss(None)


class ExtensionsView(Widget):
    DEFAULT_CSS = """
    ExtensionsView { height: auto; }
    ExtensionsView DataTable { height: auto; max-height: 8; }
    ExtensionsView .kind { color: $text-muted; margin-top: 1; }
    #ext-detail { margin-top: 1; height: auto; }
    """
    BINDINGS = [Binding("e", "edit", "open in $EDITOR"), Binding("n", "new", "new"), Binding("r", "rescan", "rescan"),
                Binding("y", "copy", "copy to account")]

    def __init__(self, subscription_line=None):
        super().__init__()
        self.subscription_line = subscription_line
        self.account = None
        self.data = {k: [] for k in KINDS}
        self.kind = "skills"

    def compose(self):
        line = self.subscription_line
        if line is None:
            from pl.tui.settings import SUBSCRIPTION_LINE as line
        yield Static(Text(line, style="bold"))
        accounts = list(C.PROFILES)
        yield Select([(a, a) for a in accounts], allow_blank=not accounts, compact=True, id="ext-account",
                     value=harnesses.default_account() if accounts else Select.NULL, disabled=not accounts)
        yield Static(Text("" if accounts else "no harness accounts are configured", style="dim"), id="ext-note")
        for k in KINDS:
            yield Label(k, classes="kind", id=f"ext-label-{k}")
            yield Static("", id=f"ext-off-{k}")
            yield DataTable(cursor_type="row", id=f"ext-{k}", zebra_stripes=True)
        yield Static("", id="ext-detail")

    def on_mount(self):
        for k in KINDS:
            self.query_one(f"#ext-{k}", DataTable).add_columns("name", "summary")
            self.query_one(f"#ext-off-{k}").display = False
        if C.PROFILES:
            self.show(harnesses.default_account())

    # ---------- scanning ----------

    def show(self, account):
        """Select an account and rescan it in a worker thread."""
        self.account = account
        sel = self.query_one("#ext-account", Select)
        if sel.value != account:
            sel.value = account

        def run():
            try:
                ext = harnesses.extensions(harnesses.account_harness(account), account)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, str(e), severity="error", markup=False)
                return
            self.app.call_from_thread(self._fill, account, ext)
        self.run_worker(run, thread=True, group="ext-scan", exclusive=True)

    def _fill(self, account, ext):
        if account != self.account:
            return
        name = harnesses.account_harness(account).name
        self.query_one("#ext-note", Static).update(Text(ext["error"] or "", style="bold #e0a040"))
        for k in KINDS:
            table = self.query_one(f"#ext-{k}", DataTable)
            off = self.query_one(f"#ext-off-{k}", Static)
            table.clear()
            self.data[k] = ext[k]
            ok = ext["supported"][k]
            table.display, off.display = ok, not ok
            off.update(Text(f"not supported by {name} yet", style="dim"))
            for i, item in enumerate(ext[k]):
                table.add_row(Text(item["name"]), Text(item["summary"]), key=str(i))
        self._detail()

    def _detail(self):
        item = self._item()
        self.query_one("#ext-detail", Static).update(
            Text(f"{item['name']}\n{item['path']}\n{item['summary']}") if item else Text("nothing selected", style="dim"))

    def _current_kind(self):
        f = self.app.focused
        return f.id[4:] if isinstance(f, DataTable) and f.id and f.id[4:] in KINDS else self.kind

    def _item(self):
        kind = self._current_kind()
        table = self.query_one(f"#ext-{kind}", DataTable)
        if not table.row_count or table.cursor_row >= len(self.data[kind]):
            return None
        return self.data[kind][table.cursor_row]

    def on_select_changed(self, m):
        if m.select.id == "ext-account" and m.value is not Select.NULL and m.value != self.account:
            self.show(m.value)

    def on_data_table_row_highlighted(self, m):
        if m.data_table.id and m.data_table.id[4:] in KINDS:
            self.kind = m.data_table.id[4:]
            self._detail()

    # ---------- actions ----------

    def action_rescan(self):
        if self.account:
            self.show(self.account)

    def action_edit(self):
        item = self._item()
        if item is None:
            self.app.notify("nothing selected", markup=False)
            return
        try:
            argv = [*shlex.split(os.environ.get("EDITOR") or "vi"), item["path"]]
            with self.app.suspend():
                _run(argv)
        except (SystemExit, Exception) as e:  # noqa: BLE001
            self.app.notify(str(e), severity="error", markup=False)
        self.action_rescan()

    def action_new(self):
        kind = self._current_kind()
        if kind == "hooks":
            self.app.notify("hooks live in settings.json: select one and press e", markup=False)
            return
        account = self.account
        try:
            h = harnesses.account_harness(account)
            if not harnesses.supported(h)[kind]:
                raise _unsupported(h, kind)
        except SystemExit as e:
            self.app.notify(str(e), severity="warning", markup=False)
            return

        def named(name):
            if not name:
                return
            try:
                path = create(h, account, kind, name)
                self.app.notify(f"created {path}", markup=False)
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.notify(str(e), severity="error", markup=False)
            self.action_rescan()
        self.app.push_screen(NameScreen(kind[:-1]), named)

    def action_copy(self):
        kind, item, account = self._current_kind(), self._item(), self.account
        if kind == "hooks":
            self.app.notify("hooks are not copied", markup=False)
            return
        if item is None:
            self.app.notify("nothing selected", markup=False)
            return
        try:
            h = harnesses.account_harness(account)
            targets = [a for a in C.PROFILES if a != account and harnesses.account_harness(a).name == h.name]
        except SystemExit as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        if not targets:
            self.app.notify(f"no other {h.name} account to copy to", markup=False)
            return

        def chosen(target):
            if not target:
                return

            def run():
                try:
                    path = copy(h, account, kind, item["name"], target)
                    msg, sev = f"copied to {path}", "information"
                except (SystemExit, Exception) as e:  # noqa: BLE001
                    msg, sev = str(e), "error"
                self.app.call_from_thread(self.app.notify, msg, severity=sev, markup=False)
            self.run_worker(run, thread=True, group="ext-copy")
        self.app.push_screen(TargetScreen(item["name"], targets), chosen)
