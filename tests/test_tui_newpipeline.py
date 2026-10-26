"""Plan 35 slice 1: the New pipeline wizard on Home."""
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, RadioButton, RadioSet, SelectionList, Static

from pl import config as C
from pl import pipeline, profiles
from pl.tui.home import HomeView
from pl.tui.newpipeline import NewPipelineScreen, name_problem

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
