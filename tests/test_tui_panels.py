"""The Pods, Skills, Local models and Chat panels of the Settings tab."""
import json
import subprocess

import pytest
from textual.app import App
from textual.widgets import DataTable, Static

from pl.tui import panels
from pl.tui.panels import CommandPanel, PANELS
from pl.tui.skills import SkillsView, PromptScreen
from pl.tui.parity import PARITY_MAP


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
    "connectors": [{"name": "github", "status": "connected", "expires": "2026-12-31"}],
    "skills": [{"name": "pl-test", "account": "main", "kind": "skill", "description": "testing"}],
}


class Host(App):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def compose(self):
        yield self.widget


@pytest.mark.asyncio
@pytest.mark.parametrize("panel_name", ["pods", "models", "chat", "connectors"])
async def test_each_panel_lists_its_items(panel_name, monkeypatch):
    runner = FakeRunner(data=FAKE_PANEL_DATA)
    monkeypatch.setattr(panels, "_run", runner)

    panel = PANELS[panel_name]() if callable(PANELS[panel_name]) else PANELS[panel_name]
    app = Host(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = panel.query_one(DataTable)
        assert table.row_count == 1
        assert len(runner.calls) >= 1

    # Test unavailable command exit 2
    err_runner = FakeRunner(returncode=2, stderr="pl: error: argument {idea,...}: invalid choice: 'xyz'")
    monkeypatch.setattr(panels, "_run", err_runner)
    panel_err = PANELS[panel_name]() if callable(PANELS[panel_name]) else PANELS[panel_name]
    app_err = Host(panel_err)
    async with app_err.run_test() as pilot:
        await pilot.pause()
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
        await pilot.pause()
        custom_panel.execute_action("x", "target")
        await pilot.pause()
        assert any("custom" in c and "do" in c and "target" in c for c in runner.calls)

    # Test pods action: rule add
    runner.calls.clear()
    panel = PANELS["pods"]() if callable(PANELS["pods"]) else PANELS["pods"]
    app = Host(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Trigger action with prompt value
        panel.execute_action("r", "rule-strict")
        await pilot.pause()
        assert any("rule" in c and "add" in c and "rule-strict" in c for c in runner.calls)

    # Test models action: test
    runner.calls.clear()
    m_panel = PANELS["models"]() if callable(PANELS["models"]) else PANELS["models"]
    app_m = Host(m_panel)
    async with app_m.run_test() as pilot:
        await pilot.pause()
        m_panel.execute_action("t", "gemma")
        await pilot.pause()
        assert any("models" in c and "test" in c and "gemma" in c for c in runner.calls)



@pytest.mark.asyncio
async def test_skills_install_asks_before_running(monkeypatch):
    from pl.tui import skills as tui_skills
    runner = FakeRunner()
    monkeypatch.setattr(tui_skills, "_run", runner)

    view = SkillsView()
    app = Host(view)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Pressing 'I' should push PromptScreen
        await pilot.press("I")
        await pilot.pause()
        assert any(isinstance(s, PromptScreen) for s in app.screen_stack)
        # runner must NOT have run yet before input
        assert not any("install" in c for c in runner.calls)

        # Submit the prompt screen
        screen = app.screen
        assert isinstance(screen, PromptScreen)
        screen.dismiss("github:owner/repo")
        await pilot.pause()
        assert any("install" in c and "github:owner/repo" in c for c in runner.calls)

        # Pressing 'u' should run update check directly
        await pilot.press("u")
        await pilot.pause()
        assert any("update" in c and "--check" in c for c in runner.calls)


def test_parity_map_matches_spec():
    assert ("pods", "list") in PARITY_MAP
    assert ("pods", "rule add") in PARITY_MAP
    assert ("pods", "show") in PARITY_MAP
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
