"""Plan 35 slice 1: the New pipeline wizard on Home."""
import json
import subprocess
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, RadioButton, RadioSet, SelectionList, Static

from pl import config as C
from pl import boardcheck, pipeline, profiles, setup
from pl.trackers import github
from pl.tui import newpipeline
from pl.tui.home import HomeView
from pl.tui.newpipeline import NewPipelineScreen, board_label, check_board, name_problem

ACCOUNTS = {"main": {"harness": "claude", "config_dir": "~/.claude"}, "alt": {"harness": "codex", "config_dir": "~/.codex"}}


class HostApp(App):
    def __init__(self):
        super().__init__()
        self.switched = []

    def compose(self):
        yield HomeView()

    def switch_to(self, profile, card=None, tab=None):
        self.switched.append((profile, tab))


@pytest.fixture(autouse=True)
def state(monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setattr(C, "ACCOUNTS", dict(ACCOUNTS))
    yield
    for k, v in saved.items():
        setattr(C, k, v)


def text(app):
    return " ".join(str(w.render()) for w in app.screen.query(Static))


async def open_wizard(pilot):
    await pilot.press("n")
    await pilot.pause()
    assert isinstance(pilot.app.screen, NewPipelineScreen)
    return pilot.app.screen


async def next_step(pilot):
    pilot.app.screen.query_one("#np-next", Button).press()
    await pilot.pause()


async def to_review(pilot, name="sales"):
    screen = await open_wizard(pilot)
    await next_step(pilot)
    screen.query_one("#np-name", Input).value = name
    await pilot.pause()
    await next_step(pilot)
    await next_step(pilot)
    await next_step(pilot)
    return screen


async def test_n_on_home_opens_step_1_of_5_with_back_and_next():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await open_wizard(pilot)
        assert "Step 1 of 5" in text(pilot.app)
        assert screen.query_one("#np-back", Button) and screen.query_one("#np-next", Button)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, NewPipelineScreen)


async def test_step_1_lists_the_builtin_templates_and_copy_existing():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await open_wizard(pilot)
        labels = [str(b.label) for b in screen.query_one("#np-kind", RadioSet).query(RadioButton)]
        for name, t in pipeline.templates().items():
            assert any(l.startswith(name) and " → ".join(t["columns"]) in l for l in labels)
        assert any(l.startswith("marketing: Brief → Brief review → ") and l.endswith("→ Done") for l in labels)
        assert labels[-1].startswith("Start from an existing pipeline")


def test_name_check_flags_bad_characters_and_an_existing_profile():
    home = Path.home()
    (home / ".pl-taken").mkdir()
    assert "a-z, 0-9" in name_problem("Bad Name")
    assert "a-z, 0-9" in name_problem("")
    assert name_problem("taken") == "profile taken already exists"
    assert name_problem("fresh") is None


async def test_name_step_shows_the_folder_it_will_create():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await open_wizard(pilot)
        await next_step(pilot)
        assert "Step 2 of 5" in text(pilot.app)
        screen.query_one("#np-name", Input).value = "sales"
        await pilot.pause()
        assert "will create ~/.pl-sales" in text(pilot.app)
        assert not screen.query_one("#np-next", Button).disabled
        screen.query_one("#np-name", Input).value = "Bad Name"
        await pilot.pause()
        assert "use a-z, 0-9" in text(pilot.app)
        assert screen.query_one("#np-next", Button).disabled


async def test_step_3_offers_a_local_board():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await open_wizard(pilot)
        await next_step(pilot)
        screen.query_one("#np-name", Input).value = "sales"
        await pilot.pause()
        await next_step(pilot)
        assert "Step 3 of 5" in text(pilot.app)
        assert "Local board" in text(pilot.app) or any("Local board" in str(b.label) for b in screen.query_one("#np-board", RadioSet).query(RadioButton))


def two_profiles(monkeypatch, a_extra="", b_extra=""):
    home = Path.home()
    for name, text in (("a", '[accounts.main]\nconfig_dir = "~/.claude"\n' + a_extra),
                       ("b", '[accounts.codex1]\nharness = "codex"\nconfig_dir = "~/.codex"\nmodel = "gpt-x"\neffort = "high"\n' + b_extra)):
        (home / f".pl-{name}").mkdir()
        (home / f".pl-{name}" / "config.toml").write_text(text)
    monkeypatch.setattr(C, "PROFILE_NAME", "a")
    monkeypatch.setattr(C, "ACCOUNTS", {"main": {"config_dir": "~/.claude"}})


def pinned_template(monkeypatch, stages):
    t = {"name": "pinned", "template": None, "columns": ["Todo", "Done"], "stages": stages}
    monkeypatch.setattr(pipeline, "templates", lambda: {"pinned": t})


async def at_step_4(pilot):
    screen = await open_wizard(pilot)
    await next_step(pilot)
    screen.query_one("#np-name", Input).value = "sales"
    await pilot.pause()
    await next_step(pilot)
    await next_step(pilot)
    assert "Step 4 of 5" in text(pilot.app)
    return screen


def labels(screen):
    return [str(screen.query_one("#np-accounts", SelectionList).get_option_at_index(i).prompt)
            for i in range(screen.query_one("#np-accounts", SelectionList).option_count)]


def checked(screen):
    sel = screen.query_one("#np-accounts", SelectionList)
    return [str(sel.get_option_at_index(i).prompt).split()[0] for i in sel.selected]


async def test_step_4_lists_accounts_from_every_profile_with_harness_folder_and_source(monkeypatch):
    two_profiles(monkeypatch)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        main, codex = sorted(labels(screen))
        assert main.startswith("codex1 (codex) ~/.codex from b:") and codex.startswith("main (claude) ~/.claude from a:")


async def test_step_4_checks_only_the_current_profiles_accounts_at_first(monkeypatch):
    two_profiles(monkeypatch)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        assert checked(screen) == ["main"]
        await pilot.press("escape")


async def test_step_4_shows_the_stages_each_account_runs_or_not_used(monkeypatch):
    two_profiles(monkeypatch)
    pinned_template(monkeypatch, {"plan": {"account": "main"}, "build": {}})
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        by_name = {l.split()[0]: l for l in labels(screen)}
        assert by_name["main"].endswith("plan, build") and by_name["codex1"].endswith(": build")
    pinned_template(monkeypatch, {"plan": {"account": "main"}, "build": {"account": "main"}})
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        assert {l.split()[0]: l for l in labels(screen)}["codex1"].endswith("not used by this pipeline")


async def test_checking_two_accounts_with_the_same_name_is_refused(monkeypatch):
    two_profiles(monkeypatch)
    (Path.home() / ".pl-b" / "config.toml").write_text('[accounts.main]\nconfig_dir = "~/.claude-b"\n')
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        screen.query_one("#np-accounts", SelectionList).select_all()
        await pilot.pause()
        assert "two accounts named main; uncheck one" in text(pilot.app)
        assert screen.query_one("#np-next", Button).disabled


async def test_unchecking_every_account_keeps_step_4_and_says_one_is_needed(monkeypatch):
    two_profiles(monkeypatch)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        screen.query_one("#np-accounts", SelectionList).deselect_all()
        await pilot.pause()
        assert "pick at least one account" in text(pilot.app) and screen.query_one("#np-next", Button).disabled
        await next_step(pilot)
        assert "Step 4 of 5" in text(pilot.app)


async def test_unchecking_a_pinned_stages_account_blocks_next(monkeypatch):
    two_profiles(monkeypatch)
    pinned_template(monkeypatch, {"plan": {"account": "main"}})
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        sel = screen.query_one("#np-accounts", SelectionList)
        sel.select(1)
        sel.deselect(0)
        await pilot.pause()
        assert "stage plan uses main, which is unchecked" in text(pilot.app)
        assert screen.query_one("#np-next", Button).disabled


async def test_going_back_and_forward_keeps_the_picks(monkeypatch):
    two_profiles(monkeypatch)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        screen.query_one("#np-accounts", SelectionList).select_all()
        await pilot.pause()
        screen.query_one("#np-back", Button).press()
        await pilot.pause()
        await next_step(pilot)
        assert sorted(checked(screen)) == ["codex1", "main"]


async def test_create_writes_exactly_the_checked_accounts_with_their_settings(monkeypatch):
    two_profiles(monkeypatch)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await at_step_4(pilot)
        screen.query_one("#np-accounts", SelectionList).select_all()
        await pilot.pause()
        await next_step(pilot)
        pilot.app.screen.query_one("#np-create", Button).press()
        await pilot.pause()
    import tomllib
    written = tomllib.loads((Path.home() / ".pl-sales" / "config.toml").read_text())["accounts"]
    assert written == {"main": {"config_dir": "~/.claude"},
                       "codex1": {"harness": "codex", "config_dir": "~/.codex", "model": "gpt-x", "effort": "high"}}


async def test_step_4_lists_accounts_from_every_profile():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_review(pilot)
        screen.query_one("#np-switcher").current = "np-step-4"
        assert checked(screen) == ["main", "alt"]
        screen.query_one("#np-accounts", SelectionList).deselect_all()
        await pilot.pause()
        assert screen.query_one("#np-next", Button).disabled


async def test_dry_run_lists_the_file_and_writes_nothing():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        await to_review(pilot)
        pilot.app.screen.query_one("#np-dry", Button).press()
        await pilot.pause()
        out = text(pilot.app)
        assert "would write ~/.pl-sales/config.toml (mode 0600)" in out and "[pipeline]" in out
        assert not (Path.home() / ".pl-sales").exists()


async def test_create_writes_the_profile_and_switches_to_its_cards():
    app = HostApp()
    async with app.run_test(size=(120, 50)) as pilot:
        await to_review(pilot)
        pilot.app.screen.query_one("#np-create", Button).press()
        await pilot.pause()
    assert (Path.home() / ".pl-sales" / "config.toml").is_file()
    assert app.switched == [("sales", "cards")]


async def test_a_failed_create_leaves_no_folder_and_names_step_5(monkeypatch):
    def boom(name, **kw):
        (Path.home() / f".pl-{name}").mkdir()
        raise RuntimeError("disk full")
    monkeypatch.setattr(profiles, "new_profile", boom)
    app = HostApp()
    async with app.run_test(size=(120, 50)) as pilot:
        await to_review(pilot)
        pilot.app.screen.query_one("#np-create", Button).press()
        await pilot.pause()
        assert "step 5 (create) failed: disk full" in text(pilot.app)
        assert isinstance(pilot.app.screen, NewPipelineScreen)
    assert not (Path.home() / ".pl-sales").exists() and app.switched == []


# ---------- Plan 35 slice 2: GitHub Project and MCP boards ----------

PROJECTS = json.dumps({"projects": [{"number": 7, "title": "Sales", "owner": {"login": "acme"}},
                                    {"number": 2, "title": "Ops", "owner": {"login": "me"}}]})
GH_TRACKER = {"type": "github-project", "owner": "acme", "number": 7, "status_field": "pl stage", "repo": "acme/web"}
MCP_TRACKER = {"type": "mcp", "mcp_config": "/w/.mcp.json", "server": "board-server"}


def done(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, out, err)


def fake_gh(monkeypatch, replies):
    """replies: [(argv prefix, CompletedProcess or SystemExit)]; an unlisted call succeeds with no output."""
    calls = []

    def run(args):
        calls.append(list(args))
        for prefix, reply in replies:
            if list(args[:len(prefix)]) == list(prefix):
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        return done()
    monkeypatch.setattr(github, "_run", run)
    return calls


SIGNED_IN = [(("auth", "status"), done()), (("project", "list"), done(PROJECTS)), (("api", "user/orgs"), done(""))]


async def to_step_3(pilot, name="sales"):
    screen = await open_wizard(pilot)
    await next_step(pilot)
    screen.query_one("#np-name", Input).value = name
    await pilot.pause()
    await next_step(pilot)
    return screen


async def pick_board(pilot, index):
    screen = pilot.app.screen
    list(screen.query_one("#np-board", RadioSet).query(RadioButton))[index].value = True
    await pilot.pause()
    await pilot.pause()


async def test_step_3_offers_three_boards_local_selected():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_step_3(pilot)
        radios = screen.query_one("#np-board", RadioSet)
        labels = [str(b.label) for b in radios.query(RadioButton)]
        assert [l.split(":")[0] for l in labels] == ["Local board", "GitHub Project", "MCP board"]
        assert "needs a project and a repository" in labels[1] and "needs an MCP server name" in labels[2]
        assert radios.pressed_index == 0 and not screen.query_one("#np-next", Button).disabled


async def test_github_project_lists_projects_from_a_fake_gh_and_prefills_repo(monkeypatch):
    fake_gh(monkeypatch, SIGNED_IN)
    monkeypatch.setattr(setup, "_remote_repo", lambda d: "acme/web")
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_step_3(pilot)
        await pick_board(pilot, 1)
        projects = [str(b.label) for b in screen.query_one("#np-projects", RadioSet).query(RadioButton)]
        assert projects == ["acme #7 Sales", "me #2 Ops"]
        assert screen.query_one("#np-repo", Input).value == "acme/web"
        assert screen.query_one("#np-next", Button).disabled
        list(screen.query_one("#np-projects", RadioSet).query(RadioButton))[0].value = True
        await pilot.pause()
        assert not screen.query_one("#np-next", Button).disabled
        screen.query_one("#np-repo", Input).value = "not a repo"
        await pilot.pause()
        assert screen.query_one("#np-next", Button).disabled


@pytest.mark.parametrize("replies,said", [
    ([(("auth", "status"), SystemExit("pl: gh is not installed: https://cli.github.com"))], "gh is not installed"),
    ([(("auth", "status"), done(code=1))], "gh is not signed in: run gh auth login"),
    ([(("auth", "status"), done()), (("project", "list"), done('{"projects": []}'))], "gh lists no projects"),
])
async def test_gh_missing_not_signed_in_or_no_projects_says_which_and_disables_next(monkeypatch, replies, said):
    fake_gh(monkeypatch, replies)
    monkeypatch.setattr(setup, "_remote_repo", lambda d: None)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_step_3(pilot)
        await pick_board(pilot, 1)
        assert said in str(screen.query_one("#np-gh-note", Static).render())
        assert screen.query_one("#np-next", Button).disabled
        await pick_board(pilot, 0)
        assert not screen.query_one("#np-next", Button).disabled


async def test_mcp_board_needs_a_name_and_shows_the_config_file(monkeypatch):
    monkeypatch.setattr(setup, "_mcp_file", lambda flags, work: Path("/w/.mcp.json"))
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_step_3(pilot)
        await pick_board(pilot, 2)
        assert "looked up in /w/.mcp.json" in text(pilot.app)
        assert screen.query_one("#np-next", Button).disabled
        screen.query_one("#np-server", Input).value = "board-server"
        await pilot.pause()
        assert not screen.query_one("#np-next", Button).disabled
        screen.query_one("#np-server", Input).value = "  "
        await pilot.pause()
        assert screen.query_one("#np-next", Button).disabled


async def test_mcp_board_without_a_config_file_says_where_it_looked(monkeypatch):
    monkeypatch.setattr(setup, "_mcp_file", lambda flags, work: None)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        await to_step_3(pilot)
        await pick_board(pilot, 2)
        assert "no MCP config file found" in text(pilot.app) and ".claude.json" in text(pilot.app)


async def mcp_step_3(pilot, monkeypatch):
    monkeypatch.setattr(setup, "_mcp_file", lambda flags, work: Path("/w/.mcp.json"))
    screen = await to_step_3(pilot)
    await pick_board(pilot, 2)
    screen.query_one("#np-server", Input).value = "board-server"
    await pilot.pause()
    return screen


async def test_next_on_step_3_shows_checking_board_then_moves_on(monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(newpipeline, "check_board", lambda tracker, columns: gate.wait(5) and None)
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await mcp_step_3(pilot, monkeypatch)
        await next_step(pilot)
        assert "Checking board…" in text(pilot.app)
        assert screen.query_one("#np-next", Button).disabled and screen.query_one("#np-back", Button).disabled
        gate.set()
        await pilot.pause(0.3)
        assert "Step 4 of 5" in text(pilot.app)


async def test_failed_board_check_stays_on_step_3_with_the_reason_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(newpipeline, "check_board", lambda tracker, columns: "R3: card was not created")
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await mcp_step_3(pilot, monkeypatch)
        await next_step(pilot)
        await pilot.pause(0.3)
        assert "Step 3 of 5" in text(pilot.app)
        assert "board check failed: R3: card was not created" in text(pilot.app)
        assert not screen.query_one("#np-back", Button).disabled
    assert not (Path.home() / ".pl-sales").exists()


def test_check_board_mcp_runs_pl_board_check_for_that_server(monkeypatch):
    seen = []

    def check(a):
        seen.append((a.server, a.mcp_config))
        print(json.dumps({"server": a.server, "rules": [{"id": "R1", "ok": True, "reason": ""},
                                                        {"id": "R3", "ok": False, "reason": "no such column"}]}))
        raise SystemExit(1)
    monkeypatch.setattr(boardcheck, "cmd_check", check)
    assert check_board(MCP_TRACKER, ["Lead"]) == "R3: no such column"
    assert seen == [("board-server", "/w/.mcp.json")]
    monkeypatch.setattr(boardcheck, "cmd_check", lambda a: (_ for _ in ()).throw(SystemExit(0)))
    assert check_board(MCP_TRACKER, ["Lead"]) is None

    def refuse(a):
        print("pl board check: no such MCP config file /w/.mcp.json", file=__import__("sys").stderr)
        raise SystemExit(2)
    monkeypatch.setattr(boardcheck, "cmd_check", refuse)
    assert "no such MCP config file" in check_board(MCP_TRACKER, ["Lead"])


def test_check_board_github_needs_the_stage_field_and_a_reachable_repo(monkeypatch):
    field = {"name": "pl stage", "options": [{"name": "Lead"}, {"name": "Won"}]}
    monkeypatch.setattr(github, "stage_field", lambda owner, number, name: None)
    assert check_board(GH_TRACKER, ["Lead"]) == 'project acme #7 has no "pl stage" field (pl setup adds it)'
    monkeypatch.setattr(github, "stage_field", lambda owner, number, name: field)
    assert check_board(GH_TRACKER, ["Lead", "Lost", "Won"]) == 'the "pl stage" field lacks: Lost'
    calls = fake_gh(monkeypatch, [(("repo", "view"), done(code=1, err="not found"))])
    assert check_board(GH_TRACKER, ["Lead"]) == "repository acme/web is not reachable: not found"
    assert calls == [["repo", "view", "acme/web", "--json", "name"]]
    fake_gh(monkeypatch, [(("repo", "view"), done('{"name": "web"}'))])
    assert check_board(GH_TRACKER, ["Lead"]) is None
    fake_gh(monkeypatch, [(("repo", "view"), SystemExit("pl: gh is not installed: https://cli.github.com"))])
    assert "gh is not installed" in check_board(GH_TRACKER, ["Lead"])


def test_board_label_names_the_tracker():
    assert board_label({"type": "local"}) == "local"
    assert board_label(GH_TRACKER) == "github-project acme #7"
    assert board_label(MCP_TRACKER) == "mcp board-server"


async def pick_github_7(pilot, monkeypatch):
    fake_gh(monkeypatch, SIGNED_IN)
    monkeypatch.setattr(setup, "_remote_repo", lambda d: "acme/web")
    monkeypatch.setattr(newpipeline, "check_board", lambda tracker, columns: None)
    screen = await to_step_3(pilot)
    await pick_board(pilot, 1)
    list(screen.query_one("#np-projects", RadioSet).query(RadioButton))[0].value = True
    await pilot.pause()
    await next_step(pilot)
    await pilot.pause(0.3)
    await next_step(pilot)
    return screen


async def test_create_with_github_project_writes_the_tracker(monkeypatch):
    app = HostApp()
    async with app.run_test(size=(120, 50)) as pilot:
        await pick_github_7(pilot, monkeypatch)
        assert "Board: github-project acme #7" in text(pilot.app)
        pilot.app.screen.query_one("#np-create", Button).press()
        await pilot.pause()
    import tomllib
    written = tomllib.loads((Path.home() / ".pl-sales" / "config.toml").read_text())
    assert written["tracker"] == GH_TRACKER


async def test_dry_run_names_the_tracker_and_writes_nothing(monkeypatch):
    async with HostApp().run_test(size=(120, 50)) as pilot:
        await pick_github_7(pilot, monkeypatch)
        pilot.app.screen.query_one("#np-dry", Button).press()
        await pilot.pause()
        assert "board: github-project acme #7" in text(pilot.app)
        assert not (Path.home() / ".pl-sales").exists()


async def test_local_board_flow_unchanged():
    import tomllib
    async with HostApp().run_test(size=(120, 50)) as pilot:
        await to_review(pilot)
        assert "Board: local" in text(pilot.app)
        pilot.app.screen.query_one("#np-create", Button).press()
        await pilot.pause()
    assert tomllib.loads((Path.home() / ".pl-sales" / "config.toml").read_text())["tracker"] == {"type": "local"}
