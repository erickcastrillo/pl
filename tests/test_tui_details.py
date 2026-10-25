import io
import json
import subprocess
import sys

from rich.console import Console
from textual.app import App

from pl.tui import details
from pl.tui.details import DetailsScreen
from pl.tui.home import HomeView
from pl.tui.parity import PARITY_MAP
from pl.tui.review import ConfirmScreen


class DetailsApp(App):
    def __init__(self, screen):
        super().__init__()
        self._initial_screen = screen

    def on_mount(self):
        self.push_screen(self._initial_screen)


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def test_details_show_columns_stages_gates_board_loops_agents(monkeypatch):
    show_payload = {
        "name": "sales",
        "columns": ["Lead", "Qualify review", "Proposal", "Done"],
        "stages": {
            "qualify": {"skill": "pl-stage", "account": "claude", "section": "PROPOSAL", "gate": "owner"},
            "proposal": {"skill": "pl-stage", "account": "claude", "section": "PROPOSAL", "gate": "owner"},
        },
        "gates": ["owner"],
        "board": {"type": "github-project", "id": "your-org/4"},
        "connectors": None,
        "loops": ["auto-review", "standup"],
    }

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[-3:] == ["pipelines", "show", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(show_payload), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(details, "_run", fake_run)

    summary = {
        "disp": "running",
        "live_agents": ["#146 (run, agy-fast)"],
    }

    screen = DetailsScreen("sales", summary=summary)
    app = DetailsApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)

        # Check call was made with expected args
        assert any(
            c[:5] == [sys.executable, "-m", "pl", "--profile", "sales"] and c[-3:] == ["pipelines", "show", "--json"]
            for c in calls
        )

        rendered = screen_text(app)
        # Columns in order
        lead_pos = rendered.find("Lead")
        qualify_pos = rendered.find("Qualify review")
        proposal_pos = rendered.find("Proposal")
        done_pos = rendered.find("Done")
        assert lead_pos != -1 and qualify_pos != -1 and proposal_pos != -1 and done_pos != -1
        assert lead_pos < qualify_pos < proposal_pos < done_pos

        # Stages: skill, account, section, gate
        assert "pl-stage" in rendered
        assert "claude" in rendered
        assert "PROPOSAL" in rendered
        assert "owner" in rendered

        # Board: tracker type and id
        assert "github-project" in rendered
        assert "your-org/4" in rendered

        # Connectors: none
        assert "none" in rendered

        # Loops
        assert "auto-review" in rendered
        assert "standup" in rendered

        # Live agents & running state from summary
        assert "#146" in rendered
        assert "running" in rendered

        # Dispatcher switch
        assert "D" in rendered


async def test_d_asks_then_runs_the_dispatcher_command(monkeypatch):
    show_payload = {
        "name": "sales",
        "columns": ["Lead", "Done"],
        "stages": {},
        "gates": [],
        "board": {"type": "local", "id": "local"},
        "connectors": [],
        "loops": [],
    }

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[-3:] == ["pipelines", "show", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(show_payload), "")
        return subprocess.CompletedProcess(argv, 0, "dispatcher restarted", "")

    monkeypatch.setattr(details, "_run", fake_run)

    summary = {"disp": "running", "live_agents": []}
    screen = DetailsScreen("sales", summary=summary)
    app = DetailsApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await pilot.press("D")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmScreen)
        expected_msg = (
            'Restart the dispatcher of profile "sales"? Restart stops and starts it again; stop '
            'stops it until you press D again. Running agents keep running, and so do the manager and other '
            "profiles' dispatchers (pl manager stop stops the manager)."
        )
        assert app.screen.message == expected_msg

        await pilot.press("y")
        await settle(pilot)

        assert [sys.executable, "-m", "pl", "manager", "restart", "sales"] in calls


async def test_d_cancelled_does_not_run_command(monkeypatch):
    show_payload = {
        "name": "sales",
        "columns": ["Lead", "Done"],
        "stages": [],
        "board": {"type": "local", "id": "local"},
        "connectors": ["slack", "github"],
        "loops": [],
    }

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[-3:] == ["pipelines", "show", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(show_payload), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(details, "_run", fake_run)

    summary = {"disp": "running", "live_agents": []}
    screen = DetailsScreen("sales", summary=summary)
    app = DetailsApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        rendered = screen_text(app)
        assert "slack, github" in rendered

        await pilot.press("D")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await settle(pilot)

        assert not any(c[1:3] == ["manager", "restart"] for c in calls)


async def test_q_closes_screen(monkeypatch):
    show_payload = {"name": "sales", "columns": [], "stages": {}, "board": None}
    monkeypatch.setattr(details, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps(show_payload), ""))

    screen = DetailsScreen("sales", summary={})
    app = DetailsApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        assert app.screen is screen
        await pilot.press("q")
        await settle(pilot)
        assert app.screen is not screen


def test_parity_map_matches_spec():
    assert PARITY_MAP.get("details") == "pl pipelines show NAME --json" or \
           PARITY_MAP.get(("details", "show")) == "pl pipelines show NAME --json" or \
           PARITY_MAP.get(("pipelines", "details")) == "pl pipelines show NAME --json"
    assert PARITY_MAP.get("dispatcher") == "pl manager restart NAME" or \
           PARITY_MAP.get(("details", "dispatcher")) == "pl manager restart NAME"


async def test_home_view_binding_space():
    view = HomeView(selected_profile="sales")
    binding = next((b for b in view.BINDINGS if b.key == "space"), None)
    assert binding is not None
    assert binding.action == "details"
