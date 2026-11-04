import io
import json
import subprocess
from rich.console import Console
from textual.app import App
from textual.widgets import DataTable, SelectionList

from pl.trackers import mcp
from pl.tui import connectors
from pl.tui.connectors import ChatRoutesScreen, ConnectorsPanel
from pl.tui.parity import COMMANDS
from pl.tui.review import ConfirmScreen

SECRET = "sekret-123456"


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


def conn(routes, name="x"):
    return {"role": "messaging", "adapter": "telegram", "token": "${TG_TOKEN}", "allow": ["1"], "routes": routes}


class FakeChat:
    """Serves `pl chat list --json` from a dict and records every argv."""

    def __init__(self, monkeypatch, conns, route_rc=0, route_err=""):
        self.conns, self.calls, self.route_rc, self.route_err = conns, [], route_rc, route_err
        monkeypatch.setattr(connectors, "_run", self.run)

    def run(self, argv, **kw):
        self.calls.append(argv)
        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

        if argv == ["pl", "chat", "list", "--json"]:
            return done(out=json.dumps({"connectors": self.conns}))
        if argv[:3] == ["pl", "chat", "route"]:
            if self.route_rc:
                return done(self.route_rc, err=self.route_err)
            kind, names = argv[3], argv[4:]
            for n, c in self.conns.items():
                r = [k for k in c.get("routes", ["gates"]) if k != kind]
                c["routes"] = r + [kind] if n in names else r
            return done(out=f"routed {kind} to {', '.join(names) or 'none'}")
        if argv[:3] == ["pl", "chat", "remove"]:
            self.conns.pop(argv[3], None)
            return done(out=f"removed chat connector {argv[3]}")
        return done(out="[]" if argv == ["pl", "connectors", "list", "--json"] else "")

    def commands(self, verb):
        return [c for c in self.calls if c[:3] == ["pl", "chat", verb]]


def notices(app):
    seen = []
    real = app.notify
    app.notify = lambda msg, **kw: (seen.append(str(msg)), real(msg, **kw))[1]
    return seen


async def focus_chat_row(app, pilot, row=0):
    table = app.query_one("#chat-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.pause()
    return table


async def toggle(pilot, *kinds):
    for kind in kinds:
        for _ in range(["gates", "alerts", "digest"].index(kind)):
            await pilot.press("down")
        await pilot.press("space")
        for _ in range(["gates", "alerts", "digest"].index(kind)):
            await pilot.press("up")


async def test_r_opens_picker_with_current_routes(monkeypatch):
    FakeChat(monkeypatch, {"A": conn(["gates", "digest"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        assert isinstance(app.screen, ChatRoutesScreen)
        assert set(app.screen.query_one("#chat-routes", SelectionList).selected) == {"gates", "digest"}


async def test_uncheck_alerts_keeps_other_connector(monkeypatch):
    fake = FakeChat(monkeypatch, {"A": conn(["alerts", "gates"]), "B": conn(["alerts"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        seen = notices(app)
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        await toggle(pilot, "alerts")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert fake.commands("route") == [["pl", "chat", "route", "alerts", "B"]]
        assert any("pl chat route alerts B" in n for n in seen)
        assert fake.calls[-1] == ["pl", "chat", "list", "--json"]
        assert app.query_one("#chat-table", DataTable).get_row_at(0)[3].plain == "gates"


async def test_two_kinds_changed_one_call_each(monkeypatch):
    fake = FakeChat(monkeypatch, {"A": conn(["gates", "digest"]), "B": conn(["alerts"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        seen = notices(app)
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        await toggle(pilot, "alerts", "digest")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert fake.commands("route") == [["pl", "chat", "route", "alerts", "B", "A"],
                                          ["pl", "chat", "route", "digest"]]
        assert any("pl chat route alerts B A" in n and "pl chat route digest" in n for n in seen)


async def test_save_unchanged_or_escape_runs_nothing(monkeypatch):
    fake = FakeChat(monkeypatch, {"A": conn(["gates"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert len(app.screen_stack) == 1
        await pilot.press("r")
        await settle(pilot)
        await toggle(pilot, "alerts")
        await pilot.press("escape")
        await settle(pilot)
        assert len(app.screen_stack) == 1
        assert not fake.commands("route")


async def test_failed_route_shows_error_row_unchanged(monkeypatch):
    err = "pl chat route: connector 'A' not found"
    fake = FakeChat(monkeypatch, {"A": conn(["gates"])}, route_rc=1, route_err=err)
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        seen = notices(app)
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        await toggle(pilot, "alerts")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert any(err in n for n in seen)
        assert len(fake.commands("route")) == 1
        assert app.query_one("#chat-table", DataTable).get_row_at(0)[3].plain == "gates"


async def test_r_and_d_ignored_off_chat_rows(monkeypatch):
    fake = FakeChat(monkeypatch, {})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await focus_chat_row(app, pilot)
        for key in ("r", "d"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1
        oauth = app.query_one("#connectors-table", DataTable)
        oauth.focus()
        await pilot.pause()
        for key in ("r", "d"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1
        assert not fake.commands("route") and not fake.commands("remove")


async def test_d_confirm_names_connector_and_removes(monkeypatch):
    fake = FakeChat(monkeypatch, {"telegram": conn(["gates"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        seen = notices(app)
        await settle(pilot)
        table = await focus_chat_row(app, pilot)
        await pilot.press("d")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen) and "telegram" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
        assert fake.commands("remove") == [["pl", "chat", "remove", "telegram", "--yes"]]
        assert "telegram" not in app.query_one("#chat-table", DataTable).get_row_at(0)[0].plain
        assert any("pl chat remove telegram --yes" in n for n in seen)
        assert table.row_count == 1


async def test_d_no_does_nothing(monkeypatch):
    fake = FakeChat(monkeypatch, {"telegram": conn(["gates"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("d")
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        assert not fake.commands("remove")
        assert app.query_one("#chat-table", DataTable).get_row_at(0)[0].plain == "telegram"


async def test_token_value_never_shown(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", SECRET)
    monkeypatch.setattr(mcp, "_secrets", {SECRET})
    fake = FakeChat(monkeypatch, {"A": conn(["gates"])})
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        seen = notices(app)
        await settle(pilot)
        await focus_chat_row(app, pilot)
        await pilot.press("r")
        await settle(pilot)
        assert SECRET not in screen_text(app)
        await toggle(pilot, "alerts")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert SECRET not in screen_text(app)
        fake.route_rc, fake.route_err = 1, f"failed with {SECRET}"
        await pilot.press("r")
        await settle(pilot)
        await toggle(pilot, "digest")
        await pilot.press("ctrl+s")
        await settle(pilot)
        await pilot.press("d")
        await settle(pilot)
        assert SECRET not in screen_text(app)
        await pilot.press("y")
        await settle(pilot)
        assert seen and all(SECRET not in n for n in seen)
        assert SECRET not in screen_text(app)


def test_parity_has_route_and_remove():
    assert COMMANDS["ConnectorsPanel.route"] == "pl chat route KIND NAME..."
    assert COMMANDS["ConnectorsPanel.remove"] == "pl chat remove NAME --yes"
