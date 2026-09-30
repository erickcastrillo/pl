"""Settings tab: edits the profile's config.toml on one screen; writes only through config.save."""
import tomlkit
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select, Static

from pl import config as C
from pl import harnesses, trackers
from pl.trackers import github
from pl.tui.extensions import ExtensionsView, missing_skill
from pl.tui.review import ConfirmScreen

SUBSCRIPTION_LINE = ("pl runs the harness CLIs you already have installed and signed in, on your own subscription. "
                     "It never asks for or stores an API key.")
LEGACY_HINT = "no profile loaded: settings are read-only. Create a profile to edit them: pl profiles new <name>"


def create_project(owner, title):
    """Create a GitHub Project and write its number and "pl stage" field into this profile's tracker. Saves nothing on failure."""
    p = C.path()
    if p is None:
        raise SystemExit(C.READ_ONLY)
    doc = tomlkit.parse(p.read_text())
    made = github.GitHubProject.create_project(owner, title, C.COLUMNS)
    if "tracker" not in doc:
        doc["tracker"] = tomlkit.table()
    doc["tracker"]["owner"], doc["tracker"]["number"], doc["tracker"]["status_field"] = owner, made["number"], made["status_field"]
    try:
        C.save(doc)
    except SystemExit as e:
        raise SystemExit(f"{e}\npl: the project was created but not saved; delete it or add it by hand: {made['url']}") from None
    C.load(config_dir=str(C.CONFIG_DIR))
    return made


def _set(doc, path, value):
    """Set doc[a][b]...[z] = value, creating tables on the way."""
    t = doc
    for k in path[:-1]:
        if k not in t:
            t[k] = tomlkit.table()
        t = t[k]
    t[path[-1]] = value


def _gh_summary():
    try:
        out = github._gh(["auth", "status"])
    except SystemExit as e:
        return str(e)
    lines = [ln.strip(" ✓") for ln in out.splitlines() if "Logged in" in ln]
    return "; ".join(lines) or "gh: signed in"


class ProjectScreen(ModalScreen):
    """Asks the owner and title of the GitHub Project to create."""
    DEFAULT_CSS = """
    ProjectScreen { align: center middle; }
    ProjectScreen > Vertical { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self):
        with Vertical():
            yield Static(Text("Create a GitHub Project for this profile's cards (adds a \"pl stage\" field)"))
            yield Input(placeholder="owner (user or organisation)", id="project-owner")
            yield Input(value=f"pl {C.PROFILE_NAME}", placeholder="title", id="project-title")
            yield Static(Text("enter create · esc cancel", style="dim"))

    def on_input_submitted(self, _):
        self.dismiss((self.query_one("#project-owner", Input).value.strip(), self.query_one("#project-title", Input).value))

    def action_cancel(self):
        self.dismiss(None)


class SettingsView(Widget):
    DEFAULT_CSS = """
    SettingsView { height: 1fr; }
    #settings-scroll { height: 1fr; }
    SettingsView .panel { height: auto; }
    SettingsView .field { height: 1; }
    SettingsView .field Label { width: 28; color: $text-muted; }
    SettingsView .field Input, SettingsView .field Select { width: 1fr; }
    #settings-hint { height: 1; margin: 0 1; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "save"), Binding("t", "test", "test connections")]

    def __init__(self):
        super().__init__()
        self.fields = {}      # widget id -> (toml path, initial value, kind)

    # ---------- building the screen ----------

    def _field(self, label, path, value, kind="str", options=None):
        wid = f"set-{len(self.fields)}"
        if options is not None and value not in options:
            value = None    # shown blank, so only a real pick counts as a change
        self.fields[wid] = (path, value, kind)
        ro = C.path() is None
        with Horizontal(classes="field"):
            yield Label(label)
            if options is not None:
                yield Select([(o, o) for o in options], value=Select.NULL if value is None else value,
                             allow_blank=True, compact=True, id=wid, disabled=ro)
            else:
                yield Input(value="" if value is None else str(value), compact=True, id=wid, disabled=ro)

    def _panel(self, title):
        v = Vertical(classes="panel")
        v.border_title = title
        return v

    def compose(self):
        yield Static(Text(LEGACY_HINT if C.path() is None else
                          f"profile {C.PROFILE_NAME} · {C.path()} · ctrl+s save (the dispatcher uses it from its next pass) · t test connections",
                          style="bold #e0a040" if C.path() is None else "dim"), id="settings-hint")
        accounts = list(C.PROFILES)
        with VerticalScroll(id="settings-scroll"):
            with self._panel("General"):
                yield from self._field("user name", ("user", "name"), C.USER_NAME)
                yield from self._field("user email", ("user", "email"), C.USER_EMAIL)
                yield from self._field("tmux session", ("tmux_session",), C.TMUX_SESSION)
                yield from self._field("work dir", ("paths", "work_dir"), str(C.WORK_DIR))
                yield from self._field("plans dir", ("paths", "plans_dir"), str(C.PLANS))
            with self._panel("Connections"):
                yield Static(Text(SUBSCRIPTION_LINE, style="bold"))
                yield Static(self._accounts_text())
                for kind, cfg in (("tracker", C.TRACKER), ("intake", C.INTAKE)):
                    typ = cfg.get("type") or "not set"
                    target = cfg.get("board_id") or f"{cfg.get('owner') or cfg.get('repo') or '?'} {cfg.get('number') or ''}"
                    yield Static(Text(f"{kind:<8} {typ}  {target}"))
                    yield Static(Text("  press t to test", style="dim"), id=f"test-{kind}")
                    if typ == "mcp":
                        yield from self._field(f"{kind} board id", (kind, "board_id"), cfg.get("board_id"))
                if (C.TRACKER.get("type") == "github-project" and not C.TRACKER.get("number")) and C.path() is not None:
                    yield Button("Create project", id="create-project", compact=True)
                yield Static(Text("code host  gh: checking…", style="dim"), id="codehost")
            with self._panel("Stages and harnesses"):
                names = list(harnesses.BUILTINS)
                for stage in C.PROMPTS:
                    st = C.STAGES.get(stage) or {}
                    yield from self._field(f"{stage} harness", ("stages", stage, "harness"), st.get("harness"), options=names)
                    yield from self._field(f"{stage} account", ("stages", stage, "account"), st.get("account"), options=accounts)
                    yield from self._field(f"{stage} prompt", ("stages", stage, "prompt"), C.PROMPTS[stage])
                    warn = Static("", classes="stage-warning", id=f"warn-{stage}")
                    warn.display = False
                    yield warn
            with self._panel("Extensions"):
                yield ExtensionsView(SUBSCRIPTION_LINE)
            with self._panel("Loops"):
                for name, svc in C.SERVICES.items():
                    yield from self._field(f"{name} prompt", ("loops", name, "prompt"), svc["prompt"])
                    yield from self._field(f"{name} account", ("loops", name, "account"), svc.get("profile"), options=accounts)
            with self._panel("Dispatcher"):
                for key in ("max_runs", "max_prep", "interval"):
                    yield from self._field(key.replace("_", " "), ("dispatch", key), C.DISPATCH.get(key), kind="int")
                yield from self._field("spec approval gate", ("gates", "spec"), "on" if C.GATES.get("spec") else "off",
                                       kind="bool", options=["on", "off"])
            with self._panel("Notifications"):
                yield from self._field("attention command", ("paths", "attention_cmd"), str(C.ATTENTION or ""))

    def _accounts_text(self):
        t = Text()
        for name, a in C.ACCOUNTS.items():
            hname = a.get("harness") or "claude"
            try:
                h = harnesses.get(hname)
                ok = "installed" if harnesses.available(h) else f"{h.bin} not found"
                exp = "  experimental" if h.experimental else ""
            except SystemExit:
                ok, exp = "unknown harness", ""
            t.append(f"account  {name:<12} {hname:<12} {a.get('config_dir', '')}  {ok}{exp}\n")
        return t

    def on_mount(self):
        def run():   # gh off the UI thread
            try:
                s = _gh_summary()
            except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
                s = f"gh: {type(e).__name__}: {e}"
            s += f"  (gh folder {C.GH_CONFIG_DIR})" if C.GH_CONFIG_DIR else ""   # the path only, never its contents
            self.app.call_from_thread(self.query_one("#codehost", Static).update, Text(f"code host  {s}"))
        self.run_worker(run, thread=True, group="settings-gh")

        def warn():   # skill files are read off the UI thread
            for stage, prompt in list(C.PROMPTS.items()):
                try:
                    h, account = harnesses.harness_for(stage)
                    name = missing_skill(prompt, h, account)
                except (SystemExit, Exception):  # noqa: BLE001 - a worker that raises kills the app
                    continue
                if name:
                    text = Text(f"  /{name} is not a skill or command of account {account}", style="bold #e0a040")
                    self.app.call_from_thread(self._warn, stage, text)
        self.run_worker(warn, thread=True, group="settings-skills")

    def _warn(self, stage, text):
        w = self.query_one(f"#warn-{stage}", Static)
        w.update(text)
        w.display = True

    # ---------- actions ----------

    def action_test(self):
        def run():
            for kind in ("tracker", "intake"):
                try:
                    r = trackers.get(kind).test()
                except (SystemExit, Exception) as e:
                    r = f"failed: {e}"
                self.app.call_from_thread(self.query_one(f"#test-{kind}", Static).update, Text(f"  {r}"))
        self.run_worker(run, thread=True, group="settings-test")

    def changes(self):
        """[(path, value)] for every field whose value differs from what the screen started with."""
        out = []
        for wid, (path, initial, kind) in self.fields.items():
            w = self.query_one(f"#{wid}")
            v = None if isinstance(w, Select) and w.value is Select.NULL else w.value
            shown = "" if initial is None and not isinstance(w, Select) else initial
            if v == shown or (kind == "int" and str(v) == str(initial)):
                continue
            if kind == "int":
                try:
                    v = int(v)
                except ValueError:
                    raise SystemExit(f"pl: not saved: {'.'.join(path)} must be a whole number") from None
            elif kind == "bool":
                v = v == "on"
            out.append((path, v))
        return out

    def action_save(self):
        p = C.path()
        if p is None:
            self.app.notify(LEGACY_HINT, severity="warning", markup=False)
            return
        try:
            doc = tomlkit.parse(p.read_text())
            changed = self.changes()
            for path, v in changed:
                if path[0] == "loops" and "loops" not in doc:   # [loops] replaces every default loop: write them all
                    doc["loops"] = {n: {"prompt": s["prompt"], **({"account": s["profile"]} if s.get("profile") else {})}
                                    for n, s in C.SERVICES.items() if not s.get("builtin")}
                if v is None:
                    t = doc
                    for k in path[:-1]:
                        t = t.get(k, {})
                    t.pop(path[-1], None)
                else:
                    _set(doc, path, v)
            C.save(doc)
            C.load(config_dir=str(C.CONFIG_DIR))
        except (SystemExit, Exception) as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        for wid, (path, initial, kind) in list(self.fields.items()):
            w = self.query_one(f"#{wid}")
            self.fields[wid] = (path, None if isinstance(w, Select) and w.value is Select.NULL else w.value, kind)
        self.app.notify(f"saved {len(changed)} change(s); the dispatcher uses them from its next pass", markup=False)

    def on_button_pressed(self, m):
        if m.button.id != "create-project":
            return

        def asked(answer):
            if not answer:
                return
            owner, title = answer

            def run():
                try:
                    made = create_project(owner, title)
                    msg, sev = f"created {made['url']} and saved it as this profile's tracker", "information"
                except (SystemExit, Exception) as e:
                    msg, sev = str(e), "error"
                self.app.call_from_thread(self.app.notify, msg, severity=sev, markup=False)

            def confirmed(yes):
                if yes:
                    self.run_worker(run, thread=True, group="settings-project")
            self.app.push_screen(ConfirmScreen(f"Create GitHub Project {title!r} owned by {owner}?"), confirmed)
        self.app.push_screen(ProjectScreen(), asked)
