"""Checklist items 13 (console layout, pipelines, wizard, chat connectors) and 12 (Activity levels and filters)."""
import ast
import json
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, RadioButton, RadioSet, SelectionList, Static, Tab

from conftest import press
from pl import activity, events, pipeline, pods
from pl import config as C
from pl.tui import connectors, loops
from pl.tui.app import StoppedScreen
from pl.tui.newpipeline import NewPipelineScreen
from pl.tui.switcher import SwitcherScreen, picker_rows
from tui_wait import settle, wait_for_screen, wait_until

WIZARD_TEMPLATES = {"sales": "new-client", "support": "customer-support", "marketing": "marketing"}
SECOND = "sales"


@pytest.fixture
def two_pipelines(profile, tmp_path):
    """The throwaway profile plus a second pipeline beside it; both have a stopped dispatcher."""
    folder = tmp_path / f".pl-{SECOND}"
    folder.mkdir()
    (folder / "config.toml").write_text('[tracker]\ntype = "local"\n')
    return profile, folder


def pipeline_names(app):
    return [r["name"] for r in picker_rows(app.data)]


async def pipelines_loaded(pilot, count=2):
    await wait_until(pilot, lambda: len(pipeline_names(pilot.app)) >= count, f"{count} pipelines in the home data", timeout=20.0)


async def answer_stopped(pilot):
    await wait_for_screen(pilot, "StoppedScreen")
    await press(pilot, "s")


def restart_target(app):
    return app.restart_into[1]


async def test_item_13_no_brain_tab(console):
    async with console() as pilot:
        labels = [str(tab.label) for tab in pilot.app.query(Tab)]
        assert labels
        assert not [label for label in labels if "Brain" in label], f"a Brain tab is shown: {labels}"


async def test_item_13_opens_on_dashboard(console):
    async with console() as pilot:
        assert pilot.app.active_group() == "dashboard"


async def test_item_13_switcher_ctrl_k(console, two_pipelines):
    async with console() as pilot:
        app = pilot.app
        await pipelines_loaded(pilot)
        await press(pilot, "ctrl+k")
        await wait_for_screen(pilot, "SwitcherScreen")
        assert isinstance(app.screen, SwitcherScreen)
        await press(pilot, "p", "i", "p", "e", "l", "i", "n", "e", "space", *SECOND)
        await wait_until(pilot, lambda: len(app.screen.filtered) >= 1 and app.screen.filtered[0].get("profile") == SECOND,
                         f"the switcher to list the {SECOND} pipeline first")
        await press(pilot, "enter")
        await wait_until(pilot, lambda: isinstance(app.screen, StoppedScreen) or app.restart_into, "the stopped-pipeline question")
        if isinstance(app.screen, StoppedScreen):
            await press(pilot, "s")
        await wait_until(pilot, lambda: app.restart_into, "the console to switch pipelines")
        assert restart_target(app) == SECOND


async def test_item_13_alt_n_switches_pipeline(console, two_pipelines, monkeypatch):
    profile, second = two_pipelines
    first_name = profile.name.removeprefix(".pl-")
    async with console() as pilot:
        app = pilot.app
        await pipelines_loaded(pilot)
        assert pipeline_names(app) == sorted([first_name, SECOND])
        await press(pilot, "alt+2")
        await answer_stopped(pilot)
        await wait_until(pilot, lambda: app.restart_into, "alt+2 to switch pipelines")
        assert restart_target(app) == SECOND
    monkeypatch.setenv("PL_CONFIG_DIR", str(second))
    C.load()
    async with console() as pilot:
        app = pilot.app
        await pipelines_loaded(pilot)
        await press(pilot, "alt+1")
        await answer_stopped(pilot)
        await wait_until(pilot, lambda: app.restart_into, "alt+1 to switch pipelines")
        assert restart_target(app) == first_name


@pytest.mark.parametrize("kind", list(WIZARD_TEMPLATES), ids=list(WIZARD_TEMPLATES))
async def test_item_13_wizard_creates(console, tmp_path, kind):
    name, template = f"{kind}-e2e", WIZARD_TEMPLATES[kind]
    async with console() as pilot:
        app = pilot.app
        await press(pilot, "H")
        await wait_until(pilot, lambda: app.active_group() == "home", "the Home tab")
        await press(pilot, "n")
        await wait_for_screen(pilot, "NewPipelineScreen")
        wizard = app.screen
        assert isinstance(wizard, NewPipelineScreen)
        await settle(pilot)
        kinds = wizard.query_one("#np-kind", RadioSet)
        buttons = list(kinds.query(RadioButton))
        buttons[list(pipeline.templates()).index(template)].toggle()
        await settle(pilot)
        for step in (1, 2, 3, 4):
            if step == 2:
                wizard.query_one("#np-name", Input).value = name
            if step == 4:
                wizard.query_one("#np-accounts", SelectionList).select_all()
            await wait_until(pilot, lambda: not wizard.query_one("#np-next", Button).disabled, f"Next on wizard step {step}")
            wizard.query_one("#np-next", Button).press()
            await wait_until(pilot, lambda: wizard.step == step + 1, f"wizard step {step + 1}")
        wizard.query_one("#np-create", Button).press()
        folder = tmp_path / f".pl-{name}"
        await wait_until(pilot, lambda: (folder / "config.toml").is_file(), f"{folder.name}/config.toml", timeout=20.0)
        text = (folder / "config.toml").read_text()
        assert f'template = "{template}"' in text
        await wait_until(pilot, lambda: name in pipeline_names(app) or app.restart_into, f"{name} in the pipeline list", timeout=20.0)
        if name not in pipeline_names(app):   # the wizard's own switch into the new pipeline already ran
            assert restart_target(app) == name
        pod = pipeline.templates()[template].get("pod") or kind
        assert pod in pods.names()
        assert pods.find(pod)["file"].is_file()


async def test_item_13_chat_connectors_under_extend(console, monkeypatch):
    reply = {"connectors": {"team-slack": {"adapter": "slack", "allow": ["U1"], "routes": ["gates"]}}}

    def fake_run(argv, **kw):
        assert argv == ["pl", "chat", "list", "--json"], f"the connectors view ran {argv}"
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    monkeypatch.setattr(connectors, "_run", fake_run)
    async with console() as pilot:
        app = pilot.app
        await press(pilot, "6")
        await wait_until(pilot, lambda: app.active_group() == "extend", "the Extend tab")
        await press(pilot, app.sub_keys["extend"] and next(k for k, v in app.sub_keys["extend"].items() if v == "connectors"))
        await wait_until(pilot, lambda: app.active_tab == "connectors", "the Connectors sub-tab")
        table = app.query_one("#chat-table", DataTable)
        await wait_until(pilot, lambda: table.row_count == 1, "the chat connector row", timeout=20.0)
        assert "slack" in [str(c) for c in table.get_row_at(0)]
        assert str(app.query_one("#chat-title", Static).render()) == "Chat connectors · shared by all profiles on this machine"


@pytest.fixture
def demo_activity(profile):
    for kind, level in activity.DEMO:
        events.emit(kind, level=level, demo=True, message=f"sample {level} entry")


def activity_rows(app):
    table = app.query_one("#activity-table", DataTable)
    return [(str(table.get_row_at(i)[1]), str(table.get_row_at(i)[2])) for i in range(table.row_count)]


async def open_activity(pilot):
    app = pilot.app
    await press(pilot, "7")
    await wait_until(pilot, lambda: app.active_group() == "activity", "the Activity tab")
    await wait_until(pilot, lambda: len(activity_rows(app)) == len(activity.DEMO), "the demo entries in Activity", timeout=20.0)
    return app.query_one(loops.ActivityView)


async def filter_to(pilot, view, target):
    for _ in loops.FILTERS:
        if view.filter == target:
            break
        await press(pilot, "f")
        await settle(pilot)
    assert view.filter == target


async def test_item_12_activity_shows_levels(console, demo_activity):
    async with console() as pilot:
        await open_activity(pilot)
        shown = {kind.removesuffix(" demo"): level for level, kind in activity_rows(pilot.app)}
        assert shown == {"demo_error": "error", "demo_warning": "warning", "demo_info": "info"}


@pytest.mark.parametrize("target,kind", [("errors", "demo_error"), ("warnings", "demo_warning")])
async def test_item_12_filter_only(console, demo_activity, target, kind):
    async with console() as pilot:
        view = await open_activity(pilot)
        await filter_to(pilot, view, target)
        await wait_until(pilot, lambda: len(activity_rows(pilot.app)) == 1, f"only the {target} entry")
        assert [k.removesuffix(" demo") for _, k in activity_rows(pilot.app)] == [kind]


async def test_item_12_filter_all(console, demo_activity):
    async with console() as pilot:
        view = await open_activity(pilot)
        await filter_to(pilot, view, "errors")
        await wait_until(pilot, lambda: len(activity_rows(pilot.app)) == 1, "the errors filter")
        await filter_to(pilot, view, "all")
        await wait_until(pilot, lambda: len(activity_rows(pilot.app)) == len(activity.DEMO), "every entry again")


def test_item_13_file_names_only(request):
    tree = ast.parse(Path(__file__).read_text())
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    assert names
    assert [n for n in names if not n.startswith(("test_item_13_", "test_item_12_"))] == []
    assert "e2e_ui" in {m.name for m in request.node.iter_markers()}
