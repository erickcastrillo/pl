"""New pipeline wizard (Home, key n): five steps that create a profile through `pl pipelines add`'s own code."""
import argparse
import contextlib
import io
import json
import shutil
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, RadioButton, RadioSet, SelectionList, Static

from pl import config as C
from pl import boardcheck, pipeline, profiles, setup
from pl.trackers import github

STEPS = ["What kind", "Name", "Where cards live", "Who does the work", "Review"]
COPY = "Start from an existing pipeline (copy its settings)"


def name_problem(name):
    if not C.NAME_RE.match(name):
        return "use a-z, 0-9, - and _, up to 32 characters"
    if (Path.home() / f".pl-{name}").exists():
        return f"profile {name} already exists"
    return None


def _project_rows(args):
    r = github._run(args)
    if r.returncode:
        return []
    return [{"owner": p["owner"]["login"], "number": p["number"], "title": p["title"]}
            for p in json.loads(r.stdout).get("projects", [])]


def gh_projects():
    """(projects, None) for the person and each organisation, or (None, why not)."""
    try:
        if github._run(["auth", "status"]).returncode:
            return None, "gh is not signed in: run gh auth login"
        rows = _project_rows(["project", "list", "--format", "json", "--limit", "100"])
        for org in github._run(["api", "user/orgs", "--jq", ".[].login"]).stdout.split():
            rows += _project_rows(["project", "list", "--owner", org, "--format", "json", "--limit", "100"])
    except SystemExit as e:
        return None, str(e.code).removeprefix("pl: ")
    except (ValueError, KeyError, TypeError):
        return None, "gh gave a project list pl cannot read"
    return (rows, None) if rows else (None, "gh lists no projects for you or your organisations")


def _check_mcp(tracker):
    ns = argparse.Namespace(server=tracker["server"], mcp_config=tracker.get("mcp_config"), board=None, json=True,
                            header_env=None)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            boardcheck.cmd_check(ns)
    except SystemExit as e:
        if not e.code:
            return None
        try:
            rules = json.loads(out.getvalue())["rules"]
        except (ValueError, KeyError):
            return err.getvalue().strip() or "the board check failed"
        return "; ".join(f"{r['id']}: {r['reason']}" for r in rules if not r["ok"]) or err.getvalue().strip()
    return None


def _check_github(tracker, columns):
    owner, number, repo = tracker["owner"], tracker["number"], tracker["repo"]
    try:
        field = github.stage_field(owner, number, github.STAGE_FIELD)
        if field is None:
            return f'project {owner} #{number} has no "pl stage" field (pl setup adds it)'
        have = {o.get("name") for o in field.get("options") or []}
        if missing := [c for c in columns if c not in have]:
            return f'the "pl stage" field lacks: {", ".join(missing)}'
        r = github._run(["repo", "view", repo, "--json", "name"])
        if r.returncode:
            return f"repository {repo} is not reachable: {r.stderr.strip()}"
    except SystemExit as e:
        return str(e.code).removeprefix("pl: ")
    except Exception as e:
        return str(e) or type(e).__name__
    return None


def check_board(tracker, columns):
    """None when the board is fine, else why not. Read-only for a GitHub Project; an MCP board gets `pl board check`."""
    return _check_mcp(tracker) if tracker["type"] == "mcp" else _check_github(tracker, columns)


def board_label(tracker):
    if tracker["type"] == "github-project":
        return f"github-project {tracker['owner']} #{tracker['number']}"
    return f"mcp {tracker['server']}" if tracker["type"] == "mcp" else "local"


def create(choices, dry_run):
    """Make the profile (or print what it would write) with `pl pipelines add`'s code, which is private to pipeline.py.
    Returns (ok, text); a failure removes the folder it made."""
    name = choices["name"]
    folder = Path.home() / f".pl-{name}"
    existed = folder.exists()
    ns = argparse.Namespace(pipelines_cmd="add", board="local", account=None, accounts=choices["accounts"], yes=True,
                            dry_run=dry_run, tracker=choices["tracker"])
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
        return True, f"would write ~/.pl-{name}/config.toml (mode 0600)\nboard: {board_label(choices['tracker'])}\n{out.getvalue()}"
    return True, out.getvalue()


class NewPipelineScreen(ModalScreen):
    DEFAULT_CSS = """
    NewPipelineScreen { align: center middle; }
    NewPipelineScreen > Vertical { width: 100; height: auto; max-height: 95%; border: round $accent; padding: 1 2; background: $surface; }
    NewPipelineScreen ContentSwitcher { height: auto; }
    NewPipelineScreen .np-step { height: auto; }
    NewPipelineScreen Horizontal, NewPipelineScreen #np-gh, NewPipelineScreen #np-mcp { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self):
        super().__init__()
        self.step = 1
        self.templates = pipeline.templates()
        self.profile_names = [r["name"] for r in profiles.list_profiles(check_lock=False)]
        self._projects = []
        self._gh_loaded = False

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
                    yield RadioSet(RadioButton("Local board: nothing to set up", value=True),
                                   RadioButton("GitHub Project: needs a project and a repository"),
                                   RadioButton("MCP board: needs an MCP server name"), id="np-board")
                    with Vertical(id="np-gh"):
                        yield RadioSet(id="np-projects")
                        yield Input(placeholder="owner/name, where new cards go", id="np-repo")
                        yield Static("", id="np-gh-note")
                    with Vertical(id="np-mcp"):
                        yield Input(placeholder="MCP server name", id="np-server")
                        yield Static("", id="np-mcp-note")
                    yield Static("", id="np-board-note")
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
        self.query_one("#np-gh").display = self.query_one("#np-mcp").display = False
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
        if self.step == 3:
            return self._board_valid()
        if self.step == 4:
            return bool(self._selected_accounts()) or not C.ACCOUNTS
        return True

    def _board(self):
        return self.query_one("#np-board", RadioSet).pressed_index

    def _project(self):
        i = self.query_one("#np-projects", RadioSet).pressed_index
        return self._projects[i] if 0 <= i < len(self._projects) else None

    def _board_valid(self):
        if self._board() == 1:
            repo = self.query_one("#np-repo", Input).value.strip()
            return self._project() is not None and bool(setup.REPO_RE.fullmatch(repo))
        if self._board() == 2:
            return bool(self.query_one("#np-server", Input).value.strip())
        return True

    def _board_changed(self):
        board = self._board()
        self.query_one("#np-gh").display = board == 1
        self.query_one("#np-mcp").display = board == 2
        if board == 1 and not self._gh_loaded:
            self._gh_loaded = True
            projects, problem = gh_projects()
            self._projects = projects or []
            self.query_one("#np-gh-note", Static).update(Text(problem or ""))
            self.query_one("#np-projects", RadioSet).mount(
                *[RadioButton(f"{p['owner']} #{p['number']} {p['title']}") for p in self._projects])
            self.query_one("#np-repo", Input).value = setup._remote_repo(C.WORK_DIR) or ""
        if board == 2:
            path = setup._mcp_file(argparse.Namespace(mcp_config=None), C.WORK_DIR)
            self.query_one("#np-mcp-note", Static).update(Text(
                f"looked up in {path}" if path else f"no MCP config file found ({C.WORK_DIR}/.mcp.json, ~/.claude.json)"))
        self.query_one("#np-board-note", Static).update("")

    def _tracker_choice(self):
        if self._board() == 1 and (p := self._project()):
            return {"type": "github-project", "owner": p["owner"], "number": p["number"],
                    "status_field": github.STAGE_FIELD, "repo": self.query_one("#np-repo", Input).value.strip()}
        if self._board() == 2:
            path = setup._mcp_file(argparse.Namespace(mcp_config=None), C.WORK_DIR)
            return {"type": "mcp", **({"mcp_config": str(path)} if path else {}),
                    "server": self.query_one("#np-server", Input).value.strip()}
        return {"type": "local"}

    def _columns(self):
        copy, template = self._kind()
        return (pipeline._pipeline_of(copy) if copy else self.templates[template])["columns"]

    def _check_board(self):
        self.query_one("#np-board-note", Static).update(Text("Checking board…"))
        self.query_one("#np-next", Button).disabled = self.query_one("#np-back", Button).disabled = True
        tracker, columns = self._tracker_choice(), self._columns()
        self.run_worker(lambda: self.app.call_from_thread(self._checked, check_board(tracker, columns)), thread=True)

    def _checked(self, problem):
        self.query_one("#np-board-note", Static).update(Text(f"board check failed: {problem}" if problem else ""))
        if problem is None:
            self.step = 4
        self._show()

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
        return (f"Pipeline {c['name']}: {kind}\nFolder: ~/.pl-{c['name']}\nBoard: {board_label(c['tracker'])}\n"
                f"Accounts: {', '.join(c['accounts']) or 'none'}")

    def _choices(self):
        copy, template = self._kind()
        return {"copy": copy, "template": template, "name": self.query_one("#np-name", Input).value,
                "accounts": self._selected_accounts(), "tracker": self._tracker_choice()}

    def on_radio_set_changed(self, m):
        if m.radio_set.id == "np-kind":
            self.query_one("#np-copy").display = m.radio_set.pressed_index == len(list(m.radio_set.query(RadioButton))) - 1
        if m.radio_set.id == "np-board":
            self._board_changed()
        self._show()

    def on_input_changed(self, m):
        if m.input.id != "np-name":
            self._show()
            return
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
            if self.step == 3 and self._tracker_choice()["type"] != "local":
                self._check_board()
                return
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
