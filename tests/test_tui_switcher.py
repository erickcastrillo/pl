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
