import io
import json
import subprocess
import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import Checkbox, DataTable, Input, OptionList, Static

from pl import chat, manager
from pl.tui import connectors
from pl.tui.chatform import ChatAdapterPicker, ChatConnectorForm, save
from pl.tui.connectors import AddConnectorScreen, ConnectorsPanel

SECRET = "sekret-123456"


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    return tmp_path / "machine"


def machine_text(machine):
    return "".join(p.read_text() for p in machine.rglob("*") if p.is_file()) if machine.exists() else ""


def test_save_default_routes_writes_connector_and_cli_command(machine):
    ok, msg = save("telegram", "", "TG_TOKEN", "12345", ["gates"], env={"TG_TOKEN": "x"})
    assert ok
    assert chat.load()["telegram"] == {"role": "messaging", "adapter": "telegram", "token": "${TG_TOKEN}",
                                       "allow": ["12345"], "routes": ["gates"]}
    assert "pl chat add telegram --token-env TG_TOKEN --allow 12345 --name telegram --yes" in msg
    assert "pl chat route" not in msg


def test_save_extra_routes_names_every_holder():
    chat.add("slack", "${OPS_TOKEN}", ["C1"], "ops", routes=["gates", "alerts"])
    ok, msg = save("telegram", "tg", "TG_TOKEN", "1, 2", ["gates", "alerts", "digest"], env={"TG_TOKEN": "x"})
    assert ok
    assert chat.load()["tg"]["routes"] == ["gates", "alerts", "digest"]
    assert chat.load()["tg"]["allow"] == ["1", "2"]
    assert chat.load()["ops"]["routes"] == ["gates", "alerts"]
    assert "pl chat route alerts ops tg" in msg
    assert "pl chat route digest tg" in msg


def test_save_without_gates_and_no_other_holder_says_no_cli_equivalent():
    ok, msg = save("telegram", "tg", "TG_TOKEN", "1", ["digest"], env={"TG_TOKEN": "x"})
    assert ok
    assert "pl chat route gates: no CLI equivalent" in msg


def test_save_unset_variable_writes_nothing(machine):
    ok, msg = save("telegram", "", "TG_TOKEN", "1", ["gates"], env={})
    assert not ok
    assert msg == "TG_TOKEN is not set: export TG_TOKEN=… in the shell that starts pl, then restart pl"
    assert chat.load() == {}


@pytest.mark.parametrize("var", ["1TOKEN", "TG-TOKEN"])
def test_save_invalid_variable_name_shows_chat_add_message(var):
    ok, msg = save("telegram", "", var, "1", ["gates"], env={var: "x"})
    assert not ok
    assert msg == chat.add("telegram", "${" + var + "}", [], "telegram")[1]
    assert chat.load() == {}


def test_save_no_allowed_ids():
    ok, msg = save("telegram", "", "TG_TOKEN", " , ", ["gates"], env={"TG_TOKEN": "x"})
    assert not ok
    assert "at least one --allow id is required" in msg
    assert chat.load() == {}


def test_save_no_route_checked():
    ok, msg = save("telegram", "", "TG_TOKEN", "1", [], env={"TG_TOKEN": "x"})
    assert not ok and msg == "pick at least one route"
    assert chat.load() == {}


def test_save_taken_name():
    def other(doc):
        import tomlkit
        t = tomlkit.table()
        t.update({"role": "tracker", "token": "${T}"})
        doc.setdefault("connectors", tomlkit.table())["telegram"] = t
    manager.update_machine_toml(other)
    ok, msg = save("telegram", "", "TG_TOKEN", "1", ["gates"], env={"TG_TOKEN": "x"})
    assert not ok and "already exists with different settings" in msg
    assert manager._machine_toml()["connectors"]["telegram"]["role"] == "tracker"


def test_save_identical_settings_is_unchanged():
    chat.add("telegram", "${TG_TOKEN}", ["1"])
    ok, msg = save("telegram", "", "TG_TOKEN", "1", ["gates"], env={"TG_TOKEN": "x"})
    assert ok and "already exists, unchanged" in msg


def test_save_never_contains_token_value(machine):
    env = {"TG_TOKEN": SECRET}
    results = [
        save("telegram", "", "TG_TOKEN", "1", ["gates", "alerts"], env=env),
        save("telegram", "", "TG_TOKEN", "2", ["gates"], env=env),
        save("telegram", "", "TG_TOKEN", "", ["gates"], env=env),
        save("telegram", "", "1TOKEN", "1", ["gates"], env={"1TOKEN": SECRET}),
        save("telegram", "", "NOPE", "1", ["gates"], env=env),
    ]
    assert all(SECRET not in msg for _, msg in results)
    assert SECRET not in machine_text(machine)


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


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        out = json.dumps({"connectors": chat.load()}) if argv == ["pl", "chat", "list", "--json"] else "[]"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    monkeypatch.setattr(connectors, "_run", fake_run)
    return seen


def capture_notices(app):
    notices = []
    real = app.notify
    app.notify = lambda msg, **kw: (notices.append(str(msg)), real(msg, **kw))[1]
    return notices


async def open_picker(app, pilot):
    app.query_one("#chat-table", DataTable).focus()
    await pilot.press("n")
    await settle(pilot)


async def pick(app, pilot, index):
    await open_picker(app, pilot)
    for _ in range(index):
        await pilot.press("down")
    await pilot.press("enter")
    await settle(pilot)


async def test_new_chat_row_opens_picker_with_three_adapters(calls):
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        chat_table = app.query_one("#chat-table", DataTable)
        chat_table.focus()
        chat_table.move_cursor(row=0)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, ChatAdapterPicker)
        ol = app.screen.query_one(OptionList)
        assert [ol.get_option_at_index(i).id for i in range(ol.option_count)] == ["telegram", "slack", "discord"]


async def test_n_on_chat_table_opens_picker_and_n_on_oauth_table_does_not(calls):
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await open_picker(app, pilot)
        assert isinstance(app.screen, ChatAdapterPicker)
        await pilot.press("escape")
        await settle(pilot)
        app.query_one("#connectors-table", DataTable).focus()
        await pilot.press("n")
        await settle(pilot)
        assert isinstance(app.screen, AddConnectorScreen)


@pytest.mark.parametrize("index,needle", [(0, "@BotFather"), (1, "chat:write"), (2, "Copy ID")])
async def test_form_fields_defaults_and_setup_text(calls, index, needle):
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await pick(app, pilot, index)
        form = app.screen
        assert isinstance(form, ChatConnectorForm)
        assert form.query_one("#chat-name", Input).value == ["telegram", "slack", "discord"][index]
        assert form.query_one("#chat-var", Input) and form.query_one("#chat-allow", Input)
        assert len(form.query(Input)) == 3
        assert form.query_one("#route-gates", Checkbox).value
        assert not form.query_one("#route-alerts", Checkbox).value
        assert not form.query_one("#route-digest", Checkbox).value
        assert needle in str(form.query_one("#chat-setup", Static).render())


async def fill(app, pilot, var, allow):
    app.screen.query_one("#chat-var", Input).value = var
    app.screen.query_one("#chat-allow", Input).value = allow
    await pilot.press("ctrl+s")
    await settle(pilot)


async def test_form_save_writes_refreshes_and_notifies(monkeypatch, calls):
    monkeypatch.setenv("TG_TOKEN", SECRET)
    app = HostApp()
    notices = capture_notices(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await pick(app, pilot, 0)
        await fill(app, pilot, "TG_TOKEN", "12345")
        assert not isinstance(app.screen, ChatConnectorForm)
        assert chat.load()["telegram"]["token"] == "${TG_TOKEN}"
        assert calls.count(["pl", "chat", "list", "--json"]) == 2
        assert any("pl chat add telegram --token-env TG_TOKEN" in n for n in notices)
        assert all(SECRET not in n for n in notices)
        assert SECRET not in screen_text(app)


async def test_form_error_keeps_form_open(monkeypatch, calls, machine):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    app = HostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        await pick(app, pilot, 0)
        await fill(app, pilot, "TG_TOKEN", "12345")
        assert isinstance(app.screen, ChatConnectorForm)
        assert "TG_TOKEN is not set" in str(app.screen.query_one("#chat-error", Static).render())
        assert chat.load() == {}
