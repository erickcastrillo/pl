"""New pipeline wizard (Home, key n): five steps that create a profile through `pl pipelines add`'s own code."""
import argparse
import contextlib
import io
import shutil
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, RadioButton, RadioSet, SelectionList, Static

from pl import config as C
from pl import pipeline, profiles

STEPS = ["What kind", "Name", "Where cards live", "Who does the work", "Review"]
COPY = "Start from an existing pipeline (copy its settings)"


def name_problem(name):
    if not C.NAME_RE.match(name):
        return "use a-z, 0-9, - and _, up to 32 characters"
    if (Path.home() / f".pl-{name}").exists():
        return f"profile {name} already exists"
    return None


def create(choices, dry_run):
    """Make the profile (or print what it would write) with `pl pipelines add`'s code, which is private to pipeline.py.
    Returns (ok, text); a failure removes the folder it made."""
    name = choices["name"]
    folder = Path.home() / f".pl-{name}"
    existed = folder.exists()
    ns = argparse.Namespace(pipelines_cmd="add", board="local", account=None, accounts=choices["accounts"], yes=True,
                            dry_run=dry_run)
    out, err = io.StringIO(), io.StringIO()
    problem = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            source = pipeline._pipeline_of(choices["copy"]) if choices["copy"] else pipeline.templates()[choices["template"]]
            pipeline._add(name, source, ns)
    except SystemExit:
        problem = err.getvalue().strip() or "stopped"
    except Exception as e:
        problem = str(e) or type(e).__name__
    if problem:
        if not existed:
            shutil.rmtree(folder, ignore_errors=True)
        return False, f"step 5 (create) failed: {problem}"
    if dry_run:
        return True, f"would write ~/.pl-{name}/config.toml (mode 0600)\n{out.getvalue()}"
    return True, out.getvalue()


class NewPipelineScreen(ModalScreen):
    DEFAULT_CSS = """
    NewPipelineScreen { align: center middle; }
    NewPipelineScreen > Vertical { width: 100; height: auto; max-height: 95%; border: round $accent; padding: 1 2; background: $surface; }
    NewPipelineScreen ContentSwitcher { height: auto; }
    NewPipelineScreen .np-step { height: auto; }
    NewPipelineScreen Horizontal { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self):
        super().__init__()
        self.step = 1
        self.templates = pipeline.templates()
        self.profile_names = [r["name"] for r in profiles.list_profiles(check_lock=False)]

    def compose(self):
        with Vertical():
            yield Static("", id="np-progress")
            with ContentSwitcher(initial="np-step-1", id="np-switcher"):
                with Vertical(id="np-step-1", classes="np-step"):
                    yield Static("Which kind of pipeline? Each one is a chain of board columns.")
                    yield RadioSet(*[RadioButton(f"{n}: {' → '.join(t['columns'])}", value=i == 0)
                                     for i, (n, t) in enumerate(self.templates.items())], RadioButton(COPY), id="np-kind")
                    yield RadioSet(*[RadioButton(n) for n in self.profile_names], id="np-copy")
                with Vertical(id="np-step-2", classes="np-step"):
                    yield Static("Name the pipeline. It becomes a profile folder.")
                    yield Input(placeholder="sales", id="np-name")
                    yield Static("", id="np-name-note")
                with Vertical(id="np-step-3", classes="np-step"):
                    yield Static("Where do the cards live?")
                    yield RadioSet(RadioButton("Local board: nothing to set up", value=True), id="np-board")
                with Vertical(id="np-step-4", classes="np-step"):
                    yield Static("Which accounts do the work? Accounts are never created or signed in here.")
                    yield SelectionList(*[(n, n, True) for n in C.ACCOUNTS], id="np-accounts")
                    yield Static("", id="np-accounts-note")
                with Vertical(id="np-step-5", classes="np-step"):
                    yield Static("", id="np-summary")
                    yield Static("To import cards from another board later, see: pl board migrate", classes="dim")
                    with Horizontal():
                        yield Button("Create", id="np-create", variant="primary")
                        yield Button("Show what this would do", id="np-dry")
                    yield Static("", id="np-result")
            with Horizontal():
                yield Button("Back", id="np-back")
                yield Button("Next", id="np-next", variant="primary")

    def on_mount(self):
        self.query_one("#np-copy").display = False
        self._show()

    def _kind(self):
        """('copy', None) or ('template', name) for step 1."""
        radios = list(self.query_one("#np-kind", RadioSet).query(RadioButton))
        i = self.query_one("#np-kind", RadioSet).pressed_index
        if i == len(radios) - 1:
            pressed = self.query_one("#np-copy", RadioSet).pressed_button
            return (str(pressed.label) if pressed else None), None
        return None, list(self.templates)[max(i, 0)]

    def _selected_accounts(self):
        return list(self.query_one("#np-accounts", SelectionList).selected)

    def _valid(self):
        if self.step == 1:
            copy, template = self._kind()
            return bool(copy or template)
        if self.step == 2:
            return name_problem(self.query_one("#np-name", Input).value) is None
        if self.step == 4:
            return bool(self._selected_accounts()) or not C.ACCOUNTS
        return True

    def _show(self):
        self.query_one("#np-progress", Static).update(Text(f"Step {self.step} of 5: {STEPS[self.step - 1]}", style="bold"))
        self.query_one("#np-switcher", ContentSwitcher).current = f"np-step-{self.step}"
        self.query_one("#np-back", Button).disabled = self.step == 1
        self.query_one("#np-next", Button).disabled = self.step == 5 or not self._valid()
        if self.step == 5:
            self.query_one("#np-summary", Static).update(Text(self._summary()))

    def _summary(self):
        c = self._choices()
        kind = f"a copy of {c['copy']}" if c["copy"] else f"the {c['template']} template"
        return (f"Pipeline {c['name']}: {kind}\nFolder: ~/.pl-{c['name']}\nBoard: local\n"
                f"Accounts: {', '.join(c['accounts']) or 'none'}")

    def _choices(self):
        copy, template = self._kind()
        return {"copy": copy, "template": template, "name": self.query_one("#np-name", Input).value,
                "accounts": self._selected_accounts()}

    def on_radio_set_changed(self, m):
        if m.radio_set.id == "np-kind":
            self.query_one("#np-copy").display = m.radio_set.pressed_index == len(list(m.radio_set.query(RadioButton))) - 1
        self._show()

    def on_input_changed(self, m):
        problem = name_problem(m.value)
        self.query_one("#np-name-note", Static).update(Text(problem or f"will create ~/.pl-{m.value}"))
        self._show()

    def on_selection_list_selected_changed(self, _):
        only = self._selected_accounts()
        note = "runs every stage" if len(only) == 1 else "runs any stage"
        self.query_one("#np-accounts-note", Static).update(Text(
            ", ".join(f"{n} ({C.ACCOUNTS[n].get('harness') or 'claude'}) {note}" for n in only) or "pick at least one account"))
        self._show()

    def on_button_pressed(self, m):
        i = m.button.id
        if i == "np-back":
            self.step -= 1
        elif i == "np-next":
            self.step += 1
        elif i in ("np-create", "np-dry"):
            self._run(dry_run=i == "np-dry")
            return
        self._show()

    def _run(self, dry_run):
        ok, out = create(self._choices(), dry_run)
        if ok and not dry_run:
            self.dismiss(self._choices()["name"])
        else:
            self.query_one("#np-result", Static).update(Text(out))

    def action_cancel(self):
        self.dismiss(None)
