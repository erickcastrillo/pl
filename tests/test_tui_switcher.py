import argparse
import json
import os
import sys
import pytest
from test_tui_views import Provider, _fake_card_read, fake_data, fake_home  # noqa: F401  (fake_home is an autouse fixture)
from tui_wait import settle, wait_until

from pl import config as C
from pl import watch
from pl.tui import app as tui_app
from pl.tui.app import PlApp


def _fake_provider():
    return {
        "snapshot": {"rows": [], "disp": "running", "prof": "ok", "summary": "", "summary2": "", "at": "00:00:00", "needs": {}},
        "home": [],
        "alerts": [],
        "machine": {},
    }


def test_switch_to_another_profile_execs_pl_watch_with_profile_and_card(monkeypatch):
    execs = []

    def fake_execv(path, argv):
        execs.append((path, argv))
        raise OSError("stop")

    class FakeApp:
        runs = 0

        def __init__(self, **kw):
            self.restart_into = None

        def run(self):
            FakeApp.runs += 1
            if FakeApp.runs == 1:
                self.restart_into = ("profile", "diquis", "pl#42", "loops")
            else:
                self.restart_into = None

    monkeypatch.setattr(tui_app, "PlApp", FakeApp)
    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    watch.cmd_watch(argparse.Namespace(interval=None, once=False, plain=False, open=None, tab=None))
    assert len(execs) == 1
    path, argv = execs[0]
    assert path == sys.executable
    assert argv == [sys.executable, "-m", "pl", "--profile", "diquis", "watch", "--back", "--open", "pl#42", "--tab", "loops"]


def test_switch_to_sets_restart_into():
    app = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    app.switch_to("diquis", card="c42", tab="loops")
    assert app.restart_into == ("profile", "diquis", "c42", "loops")


@pytest.mark.asyncio
async def test_open_flag_opens_the_card_screen(monkeypatch):
    from pl.tui.cards import CardScreen
    monkeypatch.setattr("pl.board.card", lambda cid: {"id": cid, "title": "Test Card", "description": "desc"})

    app = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False, open_card="c123", tab="alerts")
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: isinstance(app.screen, CardScreen), "the card screen")
        assert isinstance(app.screen, CardScreen)
        assert app.screen.card_id == "c123"
        assert app.active_tab == "alerts"


def test_matches_pipelines_tabs_and_cards_by_part_of_a_name():
    from pl.tui.switcher import matches

    items = [
        {"kind": "pipeline", "name": "diquisfootball", "title": "diquisfootball", "label": "Pipeline: diquisfootball"},
        {"kind": "tab", "tab_id": "dashboard", "title": "Dashboard", "label": "Tab: Dashboard"},
        {"kind": "tab", "tab_id": "prs", "title": "Pull requests", "label": "Tab: Pull requests"},
        {"kind": "card", "card_id": "c123", "title": "Add switcher screen", "label": "Card: c123 Add switcher screen"},
    ]
    assert [x["kind"] for x in matches("diquis", items)] == ["pipeline"]
    assert [x["tab_id"] for x in matches("board", items)] == ["dashboard"]
    assert [x["card_id"] for x in matches("switch", items)] == ["c123"]
    assert [x["tab_id"] for x in matches("prs", items)] == ["prs"]
    assert [x["card_id"] for x in matches("asw", items)] == ["c123"]
    assert [x["kind"] for x in matches("BOARD", items)] == ["tab"]
    assert len(matches("", items)) == 4
    assert matches("zzz_not_found", items) == []
    assert matches("dash", ["Dashboard", "Pipeline", "Cards"]) == ["Dashboard"]


@pytest.mark.asyncio
async def test_choosing_a_card_in_another_pipeline_restarts_on_that_profile_with_the_card_open(monkeypatch):
    import json
    import subprocess
    from pl import config as C
    from pl.tui import switcher

    def fake_run(argv, **kw):
        if "--profile" in argv and "other" in argv and "list" in argv:
            cards = [{"id": "remote_c1", "title": "Remote Card 1"}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(cards), "")
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(switcher, "_run", fake_run)
    monkeypatch.setattr(C, "PROFILE_NAME", "myprof")

    data = {
        "snapshot": {"rows": [{"kind": "row", "card": {"id": "local_c1", "title": "Local Card"}, "col": "Spec ready"}],
                     "disp": "running", "prof": "ok", "summary": "", "summary2": "", "at": "00:00:00", "needs": {}},
        "home": [{"name": "other", "running": True}],
        "alerts": [],
        "machine": {},
    }

    app = PlApp(snapshot_provider=lambda: data, autostart=False, whatsnew=False, updates=False, restarts=False)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+k")
        await wait_until(pilot, lambda: isinstance(app.screen, switcher.SwitcherScreen), "the switcher screen")
        assert isinstance(app.screen, switcher.SwitcherScreen)
        await settle(pilot)   # the worker that loads the remote cards
        inp = app.screen.query_one("#switcher-input")
        inp.value = "Remote"
        ol = app.screen.query_one("#switcher-options")
        await wait_until(pilot, lambda: ol.option_count == 1, "the filter to leave the remote card")
        assert ol.option_count == 1
        ol.action_select()
        await wait_until(pilot, lambda: app.restart_into is not None, "the restart into the other pipeline")
        assert app.restart_into == ("profile", "other", "remote_c1", None)


@pytest.mark.asyncio
async def test_bang_opens_the_oldest_gate_switching_pipelines_if_needed(monkeypatch):
    import json
    import subprocess
    from pl import config as C
    from pl.tui.cards import CardScreen

    inbox_items = [{"card": "other_c1", "profile": "other_prof"}]

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, json.dumps(inbox_items), "")

    monkeypatch.setattr(tui_app, "_run", fake_run, raising=False)
    monkeypatch.setattr(C, "PROFILE_NAME", "myprof")
    monkeypatch.setattr("pl.board.card", lambda cid: {"id": cid, "title": "Test Card", "description": "desc"})

    # Case 1: item from another profile -> switch_to
    app = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("exclamation_mark")
        await wait_until(pilot, lambda: app.restart_into is not None, "the restart into the other pipeline")
        assert app.restart_into == ("profile", "other_prof", "other_c1", None)

    # Case 2: item from current profile -> CardScreen
    inbox_items = [{"card": "my_c1", "profile": "myprof"}]
    app2 = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    async with app2.run_test() as pilot:
        await settle(pilot)
        await pilot.press("exclamation_mark")
        await wait_until(pilot, lambda: isinstance(app2.screen, CardScreen), "the card screen")
        assert isinstance(app2.screen, CardScreen)
        assert app2.screen.card_id == "my_c1"

    # Case 3: empty inbox -> notifies "nothing waits on you"
    inbox_items = []
    notified = []
    app3 = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    monkeypatch.setattr(app3, "notify", lambda msg, **kw: notified.append(msg))
    async with app3.run_test() as pilot:
        await settle(pilot)
        await pilot.press("exclamation_mark")
        await wait_until(pilot, lambda: notified, "the notification")
        assert "nothing waits on you" in notified




def _profile_state(monkeypatch, tmp_path, name):
    state = tmp_path / name / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(C, "STATE_DIR", state)
    return state


def _console(data=None, **kw):
    return PlApp(snapshot_provider=Provider(data), autostart=False, whatsnew=False, updates=False, restarts=False, **kw)


def _record(state, tab, row):
    (state / "console-place.json").write_text(json.dumps({"tab": tab, "row": row}))


def _card_ids(data):
    return [r["card"]["id"] for r in data["snapshot"]["rows"] if r.get("card")]


async def test_switching_away_saves_the_tab_and_selected_row_in_this_profile_only(monkeypatch, tmp_path):
    eng = _profile_state(monkeypatch, tmp_path, "eng")
    sales = tmp_path / "sales" / "state"
    sales.mkdir(parents=True)
    async with _console().run_test(size=(176, 48)) as pilot:
        app = pilot.app
        await settle(pilot)
        app.action_tab("prs")
        await settle(pilot)
        view = app.query_one(tui_app.PrsView)
        table = view.query_one(tui_app.DataTable)
        while view._current()[1] is None or view._current()[0] != "https://github.com/o/api/pull/1576":
            table.action_cursor_down()
        app.switch_to("sales", tab=app.active_tab)
    assert json.loads((eng / "console-place.json").read_text()) == {"tab": "prs", "row": "https://github.com/o/api/pull/1576"}
    assert list(sales.iterdir()) == []


async def test_back_opens_on_the_remembered_tab_and_row(monkeypatch, tmp_path):
    state = _profile_state(monkeypatch, tmp_path, "eng")
    _fake_card_read(monkeypatch, [])
    wanted = _card_ids(fake_data())[2]
    _record(state, "cards", wanted)
    async with _console(tab="dashboard", back=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app = pilot.app
        assert app.active_tab == "cards"
        assert app.query_one(tui_app.CardsView)._current()[0] == wanted
        assert len(app.screen_stack) == 1


async def test_the_remembered_row_is_matched_by_key_not_position(monkeypatch, tmp_path):
    state = _profile_state(monkeypatch, tmp_path, "eng")
    _fake_card_read(monkeypatch, [])
    data = fake_data()
    wanted = _card_ids(data)[2]
    _record(state, "cards", wanted)
    data["snapshot"]["rows"].reverse()
    async with _console(data, back=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert pilot.app.query_one(tui_app.CardsView)._current()[0] == wanted


async def test_back_without_a_record_opens_on_the_tab_passed(monkeypatch, tmp_path):
    _profile_state(monkeypatch, tmp_path, "eng")
    async with _console(tab="prs", back=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert pilot.app.active_tab == "prs"


async def test_a_remembered_row_that_is_gone_selects_the_first_row(monkeypatch, tmp_path):
    state = _profile_state(monkeypatch, tmp_path, "eng")
    _fake_card_read(monkeypatch, [])
    _record(state, "cards", "merged-away")
    async with _console(back=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app = pilot.app
        view = app.query_one(tui_app.CardsView)
        assert app.active_tab == "cards"
        assert view._current()[0] in _card_ids(fake_data())


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"tab": "nope", "row": null}'])
async def test_a_missing_or_bad_record_falls_back_to_the_tab_passed(monkeypatch, tmp_path, content):
    state = _profile_state(monkeypatch, tmp_path, "eng")
    if content is not None:
        (state / "console-place.json").write_text(content)
    async with _console(tab="prs", back=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert pilot.app.active_tab == "prs"


async def test_plain_watch_ignores_the_record(monkeypatch, tmp_path):
    state = _profile_state(monkeypatch, tmp_path, "eng")
    _record(state, "cards", _card_ids(fake_data())[0])
    async with _console(tab="prs").run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert pilot.app.active_tab == "prs"


def test_cmd_watch_passes_back_to_the_console(monkeypatch):
    seen = []

    class FakeApp:
        restart_into = None

        def __init__(self, **kw):
            seen.append(kw)

        def run(self):
            pass

    monkeypatch.setattr(tui_app, "PlApp", FakeApp)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    watch.cmd_watch(argparse.Namespace(interval=None, once=False, plain=False, open=None, tab=None, back=False))
    watch.cmd_watch(argparse.Namespace(interval=None, once=False, plain=False, open=None, tab=None, back=True))
    assert [kw["back"] for kw in seen] == [False, True]
