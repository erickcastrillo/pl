import os
from pathlib import Path

import pytest

from pl import config as C
from pl import manager
from pl.tui import app as tui_app
from pl.tui.app import PlApp, StoppedScreen
from pl.tui.chrome import header_text
from pl.tui.switcher import PipelinesScreen, picker_rows
from test_tui_views import FakeAssistant, _open_assistant


def _three_summaries():
    return [
        {"name": "eng", "pipeline": "engineering", "running": True, "live_agents": 1, "needs_you": 0},
        {"name": "sales", "pipeline": "sales", "running": True, "live_agents": 0, "needs_you": 2},
        {"name": "support", "pipeline": "support", "running": False, "live_agents": 0, "needs_you": 1},
    ]


def _data():
    return {
        "snapshot": {"rows": [], "done": [], "disp": "running", "prof": "ok", "summary": "", "summary2": "",
                     "at": "00:00:00", "needs": {}},
        "home": _three_summaries(), "alerts": [], "machine": {},
    }


@pytest.fixture(autouse=True)
def current_profile(monkeypatch):
    monkeypatch.setattr(C, "PROFILE_NAME", "eng")
    monkeypatch.setattr(C, "PIPELINE", None)


def _app():
    return PlApp(snapshot_provider=_data, autostart=False, whatsnew=False, updates=False, restarts=False)


def test_header_shows_the_pipeline_chip_and_type():
    assert header_text({"home": _three_summaries()}).plain.startswith(" pl  eng ▾  engineering")


def test_header_notes_the_other_pipelines_that_need_you():
    plain = header_text({"home": _three_summaries()}).plain
    assert "sales 2 · support 1 need you" in plain
    assert "eng 0" not in plain


@pytest.mark.parametrize("data", [
    {"home": [{**s, "needs_you": 0} for s in _three_summaries()]}, {}, {"home": None}, None])
def test_header_has_no_note_when_no_other_pipeline_waits(data):
    assert "need you" not in header_text(data).plain


def test_picker_rows_list_every_pipeline_with_type_state_agents_and_needs():
    assert picker_rows(_data()) == [
        {"name": "eng", "type": "engineering", "running": True, "live_agents": 1, "needs_you": 0, "current": True},
        {"name": "sales", "type": "sales", "running": True, "live_agents": 0, "needs_you": 2, "current": False},
        {"name": "support", "type": "support", "running": False, "live_agents": 0, "needs_you": 1, "current": False},
    ]


def test_picker_row_for_a_pipeline_with_no_summary_yet_has_type_dash():
    rows = picker_rows({"home": [{"name": "new", "missing": True}]})
    assert rows[0]["type"] == "-" and rows[0]["running"] is False


async def test_P_opens_the_picker_and_enter_switches_on_the_same_tab():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        app.action_tab("prs")
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        assert isinstance(app.screen, PipelinesScreen)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.restart_into == ("profile", "sales", None, "prs")


async def test_escape_closes_the_picker_without_switching():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and app.restart_into is None


async def test_enter_on_the_current_pipeline_does_not_switch():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and app.restart_into is None


async def test_a_click_on_the_header_opens_the_picker():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.click("#header")
        await pilot.pause()
        assert isinstance(app.screen, PipelinesScreen)


async def test_initial_tab_the_pipeline_does_not_show_is_ignored():
    app = PlApp(snapshot_provider=_data, autostart=False, whatsnew=False, updates=False, restarts=False, tab="nope")
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert app.active_tab


class _FakeManager:
    def __init__(self, monkeypatch, fail=None, up=False):
        self.calls, self.fail, self.up = [], fail, up
        monkeypatch.setattr(manager, "enabled", lambda: True)
        monkeypatch.setattr(manager, "running", lambda: self.up)
        monkeypatch.setattr(manager, "restart", self.restart)
        monkeypatch.setattr(manager, "start", lambda: self.calls.append(("start",)) or "started")
        monkeypatch.setattr(tui_app, "SWITCH_AFTER_ERROR", 0.01)

    def restart(self, name, what, source=None):
        if self.fail:
            raise self.fail
        self.calls.append(("restart", name, what, source))
        return "restarted"


async def _tab_and_press(pilot, app, *keys, tab="prs"):
    app.action_tab(tab)
    await pilot.pause()
    await pilot.press(*keys)
    await pilot.pause()


async def test_alt_2_switches_to_the_second_pipeline_on_the_same_tab():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+2")
        assert app.restart_into == ("profile", "sales", None, "prs")


async def test_alt_2_switches_while_the_assistant_box_has_focus(monkeypatch):
    FakeAssistant(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("h", "i", "alt+2")
        await pilot.pause()
        assert app.restart_into == ("profile", "sales", None, "assistant")


async def test_alt_5_notes_no_pipeline_5_and_does_not_switch():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+5")
        assert app.restart_into is None
        assert [n.message for n in app._notifications] == ["no pipeline 5"]


async def test_alt_1_on_the_current_pipeline_does_nothing(monkeypatch):
    fm = _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+1")
        assert app.restart_into is None and not list(app._notifications) and fm.calls == []


async def test_ctrl_digit_and_digits_open_tabs_and_alt_digit_does_not():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        work = app.active_tab
        assert work != "dashboard"
        await pilot.press("ctrl+1")
        await pilot.pause()
        assert app.active_tab == "dashboard"
        await pilot.press("2", "alt+1")
        await pilot.pause()
        assert app.active_tab == work and app.restart_into is None


async def test_picker_rows_show_their_alt_keys():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("P")
        await pilot.pause()
        ol = app.screen.query_one("#pipeline-options")
        prompts = [str(ol.get_option_at_index(i).prompt) for i in range(3)]
        assert [p.startswith(f"alt+{i + 1}") for i, p in enumerate(prompts)] == [True] * 3


async def test_choosing_a_stopped_pipeline_in_the_picker_asks_first(monkeypatch):
    _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, StoppedScreen)
        assert "support is stopped" in str(app.screen.query("Static").first().render())
        assert app.restart_into is None


async def test_alt_3_on_a_stopped_pipeline_asks_first(monkeypatch):
    _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("alt+3")
        await pilot.pause()
        assert isinstance(app.screen, StoppedScreen) and app.restart_into is None
        await pilot.press("alt+2")
        await pilot.pause()
        assert len(app.screen_stack) == 2


async def test_enter_starts_the_dispatcher_and_switches(monkeypatch):
    fm = _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+3")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fm.calls == [("restart", "support", "restart", "console-D"), ("start",)]
        assert app.restart_into == ("profile", "support", None, "prs")


async def test_s_switches_without_starting(monkeypatch):
    fm = _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+3")
        await pilot.press("s")
        await pilot.pause()
        assert app.restart_into == ("profile", "support", None, "prs") and fm.calls == []


async def test_escape_cancels_and_stays(monkeypatch):
    fm = _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+3")
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and app.restart_into is None and fm.calls == []


async def test_a_failed_start_is_shown_and_still_switches(monkeypatch):
    _FakeManager(monkeypatch, fail=OSError("disk full"))
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+3")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert any("disk full" in n.message and n.severity == "error" for n in app._notifications)
        assert app.restart_into == ("profile", "support", None, "prs")


async def test_a_running_pipeline_switches_without_a_prompt(monkeypatch):
    _FakeManager(monkeypatch)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _tab_and_press(pilot, app, "alt+2")
        assert len(app.screen_stack) == 1 and app.restart_into == ("profile", "sales", None, "prs")


def _picker_options(app):
    return [o for o in app.screen.query_one("#pipeline-options").options]


def _only_current():
    return [_three_summaries()[0]]


async def _open_picker(app, pilot):
    await pilot.press("P")
    await pilot.pause()
    assert isinstance(app.screen, PipelinesScreen)


async def _select_new_pipeline(app, pilot):
    ol = app.screen.query_one("#pipeline-options")
    await pilot.press(*["down"] * (len(ol.options) - 1 - (ol.highlighted or 0)), "enter")
    await pilot.pause()


async def test_the_picker_ends_with_new_pipeline_when_only_the_current_pipeline_exists():
    app = PlApp(snapshot_provider=lambda: {**_data(), "home": _only_current()}, autostart=False,
                whatsnew=False, updates=False, restarts=False)
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_picker(app, pilot)
        options = _picker_options(app)
        assert len(options) == 2
        assert options[-1].prompt.plain == "+ New pipeline"
        assert "●" not in options[-1].prompt.plain


async def test_the_picker_ends_with_new_pipeline_after_three_pipelines():
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_picker(app, pilot)
        options = _picker_options(app)
        assert len(options) == 4
        assert [str(o.prompt).replace("●", "").split()[1] for o in options[:3]] == ["eng", "sales", "support"]   # after alt+N
        assert options[-1].prompt.plain == "+ New pipeline"


async def test_enter_on_new_pipeline_opens_the_wizard_at_step_1():
    from pl.tui.newpipeline import NewPipelineScreen
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_picker(app, pilot)
        await _select_new_pipeline(app, pilot)
        assert isinstance(app.screen, NewPipelineScreen)
        assert app.screen.query_one("#np-progress").content.plain.startswith("Step 1 of 5")
        assert not any(isinstance(s, PipelinesScreen) for s in app.screen_stack)


async def test_cancelling_the_wizard_from_the_picker_changes_nothing(temp_home):
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        app.action_tab("prs")
        await pilot.pause()
        await _open_picker(app, pilot)
        await _select_new_pipeline(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.active_tab == "prs"
        assert app.restart_into is None
    assert not list(Path(os.environ["HOME"]).glob(".pl-*"))


async def test_a_finished_wizard_switches_to_the_new_pipeline(monkeypatch):
    from textual.screen import ModalScreen
    import pl.tui.newpipeline as np

    class FakeWizard(ModalScreen):
        def on_mount(self):
            self.dismiss("ops")

    monkeypatch.setattr(np, "NewPipelineScreen", FakeWizard)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_picker(app, pilot)
        await _select_new_pipeline(app, pilot)
        assert app.restart_into == ("profile", "ops", None, "cards")


def test_picker_rows_never_include_new_pipeline():
    rows = picker_rows({"home": _three_summaries()})
    assert len(rows) == 3
    assert all(r["name"] != "+ New pipeline" for r in rows)
