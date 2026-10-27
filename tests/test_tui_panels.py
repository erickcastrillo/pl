"""The Pods, Skills, Local models and Chat panels of the Settings tab."""
import json
import subprocess

import pytest
from textual.widgets import DataTable, Static

from pl.tui import panels
from pl.tui.panels import CommandPanel, PANELS
from pl.tui.skills import SkillsView, PromptScreen
from pl.tui.parity import PARITY_MAP
from tui_wait import settle, view_app, wait_until


class FakeRunner:
    def __init__(self, data=None, returncode=0, stderr=""):
        self.data = data or {}
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.returncode != 0:
            return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)
        cmd_key = argv[1] if len(argv) > 1 and argv[0] == "pl" else argv[0]
        out = self.data.get(cmd_key, [])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")


FAKE_PANEL_DATA = {
    "pods": [{"name": "pod-alpha", "rule": "r1", "status": "active"}],
    "models": [{"name": "gemma", "status": "ok"}],
    "chat": [{"channel": "dev", "route": "auto", "status": "listening"}],
    "triggers": [{"name": "gh-bugs", "pipeline": "eng", "every": "5m", "paused": False}],
    "connectors": [{"name": "github", "status": "connected", "expires": "2026-12-31"}],
    "skills": [{"name": "pl-test", "account": "main", "kind": "skill", "description": "testing"}],
}


def Host(widget):
    return view_app(lambda: widget)


def _ran(runner, *words):
    return lambda: any(all(w in c for w in words) for c in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("panel_name", ["pods", "models", "chat", "connectors", "triggers"])
async def test_each_panel_lists_its_items(panel_name, monkeypatch):
    runner = FakeRunner(data=FAKE_PANEL_DATA)
    monkeypatch.setattr(panels, "_run", runner)

    panel = PANELS[panel_name]() if callable(PANELS[panel_name]) else PANELS[panel_name]
    app = Host(panel)
    want_rows = 2 if panel_name in ("pods", "triggers") else 1
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: panel.query_one(DataTable).row_count == want_rows, "the panel's rows")
        table = panel.query_one(DataTable)
        assert table.row_count == want_rows
        assert len(runner.calls) >= 1

    # Test unavailable command exit 2
    err_runner = FakeRunner(returncode=2, stderr="pl: error: argument {idea,...}: invalid choice: 'xyz'")
    monkeypatch.setattr(panels, "_run", err_runner)
    panel_err = PANELS[panel_name]() if callable(PANELS[panel_name]) else PANELS[panel_name]
    app_err = Host(panel_err)
    async with app_err.run_test() as pilot:
        await wait_until(pilot, lambda: "is not available in this pl" in str(panel_err.query_one("#panel-message", Static).render()),
                         "the not-available message")
        msg = panel_err.query_one("#panel-message", Static)
        assert "is not available in this pl" in str(msg.render())


@pytest.mark.asyncio
async def test_each_panel_action_runs_its_command(monkeypatch):
    runner = FakeRunner(data=FAKE_PANEL_DATA)
    monkeypatch.setattr(panels, "_run", runner)

    # Test custom CommandPanel directly
    custom_panel = CommandPanel(
        list_argv=["pl", "custom", "list", "--json"],
        columns=("name",),
        actions={"x": ("execute", ["pl", "custom", "do", "{name}"], False)},
    )
    app_c = Host(custom_panel)
    async with app_c.run_test() as pilot:
        await wait_until(pilot, lambda: runner.calls, "the panel's list command")
        custom_panel.execute_action("x", "target")
        await wait_until(pilot, _ran(runner, "custom", "do", "target"), "the custom action to run")
        assert any("custom" in c and "do" in c and "target" in c for c in runner.calls)

    # Test pods action: rule add
    runner.calls.clear()
    panel = PANELS["pods"]() if callable(PANELS["pods"]) else PANELS["pods"]
    app = Host(panel)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: runner.calls, "the panel's list command")
        # Trigger action with prompt value
        panel.execute_action("r", "rule-strict")
        await wait_until(pilot, _ran(runner, "rule", "add", "rule-strict"), "the rule add command")
        assert any("rule" in c and "add" in c and "rule-strict" in c for c in runner.calls)

    # Test models action: test
    runner.calls.clear()
    m_panel = PANELS["models"]() if callable(PANELS["models"]) else PANELS["models"]
    app_m = Host(m_panel)
    async with app_m.run_test() as pilot:
        await wait_until(pilot, lambda: runner.calls, "the panel's list command")
        m_panel.execute_action("t", "gemma")
        await wait_until(pilot, _ran(runner, "models", "test", "gemma"), "the model test command")
        assert any("models" in c and "test" in c and "gemma" in c for c in runner.calls)



def _row(table, i):
    return [str(table.get_cell_at((i, c))) for c in range(len(table.columns))]


async def _press_y_on_new_row(panel, pilot, monkeypatch):
    copied = []
    monkeypatch.setattr(panels, "copy_text", lambda app, text: copied.append(text))
    panel.query_one(DataTable).focus()
    await pilot.press("y")
    await wait_until(pilot, lambda: copied, "the copied text")
    return copied


@pytest.mark.asyncio
async def test_pods_panel_first_row_is_new_pod_and_y_copies_it(monkeypatch):
    monkeypatch.setattr(panels, "_run", FakeRunner(data=FAKE_PANEL_DATA))
    panel = PANELS["pods"]()
    app = Host(panel)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: panel.query_one(DataTable).row_count > 0, "the panel's rows")
        row = _row(panel.query_one(DataTable), 0)
        assert row[:2] == ["+ New pod", "add [pods.NAME] to config.toml"]
        notes = []
        monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
        assert await _press_y_on_new_row(panel, pilot, monkeypatch) == ["add [pods.NAME] to config.toml"]
        assert "copied" in notes


@pytest.mark.asyncio
async def test_triggers_panel_first_row_is_new_trigger_and_y_copies_it(monkeypatch):
    monkeypatch.setattr(panels, "_run", FakeRunner(data=FAKE_PANEL_DATA))
    panel = PANELS["triggers"]()
    app = Host(panel)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: panel.query_one(DataTable).row_count == 2, "the panel's rows")
        table = panel.query_one(DataTable)
        want = "pl triggers add NAME --connector C --query Q --every E --pipeline P"
        assert _row(table, 0)[:2] == ["+ New trigger", want]
        assert _row(table, 1)[:3] == ["gh-bugs", "eng", "5m"]
        notes = []
        monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
        assert await _press_y_on_new_row(panel, pilot, monkeypatch) == [want]
        assert "copied" in notes


@pytest.mark.asyncio
async def test_enter_and_r_on_new_pod_row_run_no_command(monkeypatch):
    runner = FakeRunner(data=FAKE_PANEL_DATA)
    monkeypatch.setattr(panels, "_run", runner)
    panel = PANELS["pods"]()
    app = Host(panel)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: panel.query_one(DataTable).row_count == 2, "the panel's rows")
        panel.query_one(DataTable).focus()
        await pilot.press("enter", "r")
        await settle(pilot)
        # fixed pause: asserting that no command ran; there is no state to wait for
        await pilot.pause()
        assert runner.calls == [["pl", "pods", "list", "--json"]]
        assert not any(isinstance(s, panels.InputModal) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_pods_actions_still_act_on_the_item_below_the_new_row(monkeypatch):
    monkeypatch.setattr(panels, "_run", FakeRunner(data=FAKE_PANEL_DATA))
    panel = PANELS["pods"]()
    app = Host(panel)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: panel.query_one(DataTable).row_count == 2, "the panel's rows")
        panel.query_one(DataTable).focus()
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: isinstance(app.screen, panels.InputModal), "the input prompt")
        assert isinstance(app.screen, panels.InputModal)
        assert app.screen.placeholder == "pod-alpha"


@pytest.mark.asyncio
async def test_triggers_panel_keeps_new_row_when_list_fails_or_is_empty(monkeypatch):
    monkeypatch.setattr(panels, "_run", FakeRunner(returncode=1, stderr="boom"))
    panel = PANELS["triggers"]()
    async with Host(panel).run_test() as pilot:
        await wait_until(pilot, lambda: "boom" in str(panel.query_one("#panel-message", Static).render()), "the list error")
        table = panel.query_one(DataTable)
        assert table.row_count == 1 and _row(table, 0)[0] == "+ New trigger"
        assert "boom" in str(panel.query_one("#panel-message", Static).render())

    monkeypatch.setattr(panels, "_run", FakeRunner(data={"triggers": []}))
    panel = PANELS["triggers"]()
    async with Host(panel).run_test() as pilot:
        await settle(pilot, lambda: panel.query_one(DataTable).row_count == 1, "the new-trigger row")
        table = panel.query_one(DataTable)
        assert table.row_count == 1 and _row(table, 0)[0] == "+ New trigger"


@pytest.mark.asyncio
async def test_skills_install_asks_before_running(monkeypatch):
    from pl.tui import skills as tui_skills
    runner = FakeRunner()
    monkeypatch.setattr(tui_skills, "_run", runner)

    view = SkillsView()
    app = Host(view)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: view.is_mounted, "the skills view")
        # Pressing 'I' should push PromptScreen
        await pilot.press("I")
        await wait_until(pilot, lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack), "the install prompt")
        assert any(isinstance(s, PromptScreen) for s in app.screen_stack)
        # runner must NOT have run yet before input
        assert not any("install" in c for c in runner.calls)

        # Submit the prompt screen
        screen = app.screen
        assert isinstance(screen, PromptScreen)
        screen.dismiss("github:owner/repo")
        await wait_until(pilot, _ran(runner, "install", "github:owner/repo"), "the install command")
        assert any("install" in c and "github:owner/repo" in c for c in runner.calls)

        # Pressing 'u' should run update check directly
        await pilot.press("u")
        await wait_until(pilot, _ran(runner, "update", "--check"), "the update check")
        assert any("update" in c and "--check" in c for c in runner.calls)


def test_parity_map_matches_spec():
    assert ("pods", "list") in PARITY_MAP
    assert ("pods", "rule add") in PARITY_MAP
    assert ("pods", "show") in PARITY_MAP
    assert ("triggers", "list") in PARITY_MAP
    assert ("models", "list") in PARITY_MAP
    assert ("models", "test") in PARITY_MAP
    assert ("models", "set") in PARITY_MAP
    assert ("chat", "list") in PARITY_MAP
    assert ("chat", "add") in PARITY_MAP
    assert ("chat", "test") in PARITY_MAP
    assert ("chat", "route") in PARITY_MAP
    assert ("skills", "install") in PARITY_MAP
    assert ("skills", "update") in PARITY_MAP
    for cmd in PARITY_MAP.values():
        assert cmd.startswith("pl ")
