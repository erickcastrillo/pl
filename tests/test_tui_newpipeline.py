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


async def test_step_4_lists_the_current_accounts_checked():
    async with HostApp().run_test(size=(120, 50)) as pilot:
        screen = await to_review(pilot)
        screen.query_one("#np-switcher").current = "np-step-4"
        sel = screen.query_one("#np-accounts", SelectionList)
        assert list(sel.selected) == ["main", "alt"]
        sel.deselect_all()
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
