import argparse
import os
import sys
import pytest

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
    assert argv == [sys.executable, "-m", "pl", "--profile", "diquis", "watch", "--open", "pl#42", "--tab", "loops"]


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
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, switcher.SwitcherScreen)
        # Give worker a moment to load remote cards
        await pilot.pause()
        # Filter to remote card and select it
        inp = app.screen.query_one("#switcher-input")
        inp.value = "Remote"
        await pilot.pause()
        ol = app.screen.query_one("#switcher-options")
        assert ol.option_count == 1
        ol.action_select()
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.pause()
        assert app.restart_into == ("profile", "other_prof", "other_c1", None)

    # Case 2: item from current profile -> CardScreen
    inbox_items = [{"card": "my_c1", "profile": "myprof"}]
    app2 = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    async with app2.run_test() as pilot:
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.pause()
        assert isinstance(app2.screen, CardScreen)
        assert app2.screen.card_id == "my_c1"

    # Case 3: empty inbox -> notifies "nothing waits on you"
    inbox_items = []
    notified = []
    app3 = PlApp(snapshot_provider=_fake_provider, autostart=False, whatsnew=False, updates=False, restarts=False)
    monkeypatch.setattr(app3, "notify", lambda msg, **kw: notified.append(msg))
    async with app3.run_test() as pilot:
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.pause()
        assert "nothing waits on you" in notified


