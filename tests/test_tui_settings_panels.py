import io
import json
import subprocess
import time
from rich.console import Console
from textual.app import App
from textual.widgets import DataTable, Input

from pl.tui import connectors
from pl.tui.connectors import AddConnectorScreen, ConnectorsPanel
from pl.tui.parity import PARITY


def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


class HostApp(App):
    def compose(self):
        yield ConnectorsPanel()


async def test_connectors_list_shows_status_and_expiry(monkeypatch):
    expiry_active = time.time() + 7200  # in 2h
    expiry_expired = time.time() - 3600  # 1h ago
    data = [
        {
            "name": "github-prod",
            "role": "admin",
            "teams": ["core", "infra"],
            "access": "read-write",
            "status": "connected",
            "expiry": expiry_active,
            "tier_warning": "tier limit 80%",
        },
        {
            "name": "slack-bot",
            "role": "bot",
            "teams": "notifications",
            "access": "write-only",
            "status": "expired",
            "expiry": expiry_expired,
            "tier_warning": "",
        },
    ]

    def fake_run(argv, **kw):
        if argv == ["pl", "connectors", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        txt = screen_text(app)
        assert "github-prod" in txt
        assert "admin" in txt
        assert "core, infra" in txt
        assert "read-write" in txt
        assert "connected" in txt
        assert "tier limit 80%" in txt
        assert "in 2h" in txt or "2h" in txt

        assert "slack-bot" in txt
        assert "expired" in txt

        panel = app.query_one(ConnectorsPanel)
        table = panel.query_one(DataTable)
        assert table.row_count == 3   # row 0 is "+ New connector"
        # Verify expired row rendered with red style
        row1_cells = [table.get_cell_at((2, col)) for col in range(len(table.columns))]
        assert any(getattr(c, "style", None) == "red" or "expired" in str(c).lower() for c in row1_cells)


async def test_n_runs_the_add_flow_command_with_the_answers(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv == ["pl", "connectors", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if len(argv) > 2 and argv[1] == "connectors" and argv[2] == "add":
            return subprocess.CompletedProcess(argv, 0, stdout="Authorizing in browser...\nSuccess!\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)

        assert isinstance(app.screen, AddConnectorScreen)
        app.screen.query_one("#connector-name", Input).value = "jira-work"
        app.screen.query_one("#connector-url", Input).value = "https://jira.example.test"
        app.screen.query_one("#connector-team", Input).value = "eng"
        app.screen.query_one("#connector-role", Input).value = "developer"

        await pilot.press("enter")
        await settle(pilot)

        add_calls = [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ["connectors", "add"]]
        assert len(add_calls) == 1
        argv, kw = add_calls[0]
        assert argv == [
            "pl", "connectors", "add", "jira-work", "https://jira.example.test",
            "--team", "eng", "--role", "developer", "--yes",
        ]
        assert not kw.get("shell")


async def test_no_token_text_reaches_the_screen(monkeypatch):
    secret_token = "ghp_1234567890abcdef1234567890abcdef"
    jwt_secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozG4tP"
    data = [
        {
            "name": "oauth-conn",
            "role": "user",
            "teams": ["all"],
            "access": "read",
            "status": "connected",
            "expiry": time.time() + 3600,
            "token": secret_token,
            "access_token": jwt_secret,
            "tier_warning": "",
        }
    ]

    def fake_run(argv, **kw):
        if argv == ["pl", "connectors", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        txt = screen_text(app)
        assert secret_token not in txt
        assert jwt_secret not in txt
        assert "oauth-conn" in txt


async def test_connectors_not_available_shows_notice(monkeypatch):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(
            argv, 2, stdout="", stderr="pl: error: argument {command}: invalid choice: 'connectors'\n"
        )

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        txt = screen_text(app)
        assert "pl connectors is not available in this pl" in txt


def test_parity_mapping():
    assert PARITY["connectors.add"] == "pl connectors add"
    assert PARITY["connectors.test"] == "pl connectors test"
    assert PARITY["connectors.revoke"] == "pl connectors revoke"


async def test_t_runs_test_command(monkeypatch):
    calls = []
    data = [{"name": "conn1", "role": "admin", "teams": "core", "access": "rw", "status": "ok", "expiry": ""}]

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv == ["pl", "connectors", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Connection OK", stderr="")

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("t")
        await settle(pilot)

        test_calls = [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ["connectors", "test"]]
        assert len(test_calls) == 1
        assert test_calls[0][0] == ["pl", "connectors", "test", "conn1"]


async def test_R_revokes_behind_confirm(monkeypatch):
    calls = []
    data = [{"name": "conn1", "role": "admin", "teams": "core", "access": "rw", "status": "ok", "expiry": ""}]

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv == ["pl", "connectors", "list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Revoked", stderr="")

    monkeypatch.setattr(connectors, "_run", fake_run)

    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.press("R")
        await settle(pilot)

        # Confirm screen is open; press 'y' to confirm
        await pilot.press("y")
        await settle(pilot)

        revoke_calls = [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ["connectors", "revoke"]]
        assert len(revoke_calls) == 1
        assert revoke_calls[0][0] == ["pl", "connectors", "revoke", "conn1", "--yes"]


async def test_settings_view_has_no_extend_panels():
    from pl.tui import settings

    class SettingsApp(App):
        def compose(self):
            yield settings.SettingsView()

    app = SettingsApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        view = app.query_one(settings.SettingsView)
        assert not view.query(ConnectorsPanel)
        from pl.tui.extensions import ExtensionsView
        from pl.tui.panels import CommandPanel
        from pl.tui.skills import SkillsView
        assert not view.query(SkillsView) and not view.query(ExtensionsView)
        assert len(view.query(CommandPanel)) == 2   # the Chat and Triggers panels
        titles = [p.border_title for p in view.query(".panel")]
        assert titles == ["General", "Connections", "Stages and harnesses", titles[3], "Chat", "Triggers", "Loops", "Dispatcher", "Notifications"]
        assert titles[3].startswith("Models and effort")


def _footer_keys(app):
    return {b.description for _, (_, b, enabled, _) in app.screen.active_bindings.items() if enabled}


def _fake_list(monkeypatch, data, calls=None):
    def fake_run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        out = json.dumps(data) if argv == ["pl", "connectors", "list", "--json"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    monkeypatch.setattr(connectors, "_run", fake_run)


async def test_first_row_is_new_connector_even_with_no_connectors(monkeypatch):
    _fake_list(monkeypatch, [])
    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        t = app.query_one(DataTable)
        assert t.row_count == 1
        assert t.get_row_at(0)[0].plain == "+ New connector" and "dim" in str(t.get_row_at(0)[0].style)


async def test_enter_on_new_connector_opens_the_add_form_and_runs_pl_connectors_add(monkeypatch):
    calls = []
    _fake_list(monkeypatch, [], calls)
    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, AddConnectorScreen)
        app.screen.query_one("#connector-name", Input).value = "jira-work"
        app.screen.query_one("#connector-url", Input).value = "https://jira.example.test"
        await pilot.press("enter")
        await settle(pilot)
        assert ["pl", "connectors", "add", "jira-work", "https://jira.example.test", "--yes"] in calls
        assert app.query_one(DataTable).get_row_at(0)[0].plain == "+ New connector"


async def test_t_and_R_on_the_new_row_say_nothing_selected(monkeypatch):
    calls = []
    _fake_list(monkeypatch, [{"name": "conn1", "role": "admin", "teams": "core", "access": "rw", "status": "ok"}], calls)
    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        for key in ("t", "R"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1
            assert [n.message for n in app._notifications] == ["nothing selected"]
            app.clear_notifications()
        assert not [c for c in calls if c[1:3] in (["connectors", "test"], ["connectors", "revoke"])]


async def test_footer_reads_n_new_connector(monkeypatch):
    _fake_list(monkeypatch, [])
    app = HostApp()
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(pilot)
        assert "new connector" in _footer_keys(app)
