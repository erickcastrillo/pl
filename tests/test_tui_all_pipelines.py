"""Plan 43 slice 5: Dashboard A lists every pipeline's waiting items; a approves, x sends back, enter opens."""
import json
import subprocess

import pytest
from textual.widgets import DataTable

from pl import config as C
from pl.tui import dashboard
from pl.tui.cards import CardScreen
from pl.tui.dashboard import inbox_rows
from pl.tui.panels import InputModal
from pl.tui.review import ConfirmScreen
from pl.tui.app import PlApp
from test_tui_views import Provider, screen_text, settle

MIN, HOUR, DAY = 60, 3600, 86400


def _item(pipeline, card, age_seconds, gate="spec", summary="a summary", **extra):
    return {"pipeline": pipeline, "card": card, "gate": gate, "summary": summary, "age_seconds": age_seconds,
            "command": f"pl --profile {pipeline} approve {card}", **extra}


ITEMS = [_item("eng", "eng#1", 5 * MIN, summary="newest eng"), _item("sales", "sales#7", 3 * DAY, summary="old sales"),
         _item("support", "support#3", 2 * HOUR, gate="plan", summary="support plan"), _item("eng", "eng#2", DAY, summary="older eng")]


class FakePl:
    """Stands in for dashboard._run: serves `pl inbox --json` and records every other call."""

    def __init__(self, items=ITEMS, inbox=None, gate=None):
        self.items, self.inbox, self.gate, self.calls = list(items), inbox, gate, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[3:] == ["inbox", "--json"]:
            if self.inbox:
                return self.inbox
            return subprocess.CompletedProcess(argv, 0, json.dumps({"items": self.items}), "")
        if self.gate:
            return self.gate
        self.items = [i for i in self.items if i["card"] != argv[-1] and i["card"] != argv[-2]]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def gates(self):
        return [a for a, _ in self.calls if a[3:] != ["inbox", "--json"]]


def _app():
    return PlApp(snapshot_provider=Provider(), autostart=False, whatsnew=False, updates=False, restarts=False)


def _rows(app):
    t = app.query_one("#decide", DataTable)
    return [(str(t.get_row_at(i)[0]), str(t.get_row_at(i)[1])) for i in range(t.row_count)]


async def _all_on(pilot, app):
    await settle(pilot)
    app.query_one("#decide", DataTable).focus()
    await pilot.press("A")
    await settle(pilot)


async def test_A_shows_all_pipelines_with_labels_and_restores_cursor(monkeypatch):
    fake = FakePl()
    monkeypatch.setattr(dashboard, "_run", fake)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        table = app.query_one("#decide", DataTable)
        table.focus()
        table.move_cursor(row=2)
        before = _rows(app)
        await pilot.press("A")
        await settle(pilot)
        assert table.border_title == "Needs you · all pipelines"
        assert "A all pipelines" in screen_text(app)
        rows = _rows(app)
        assert len(rows) == 4
        assert rows[0] == ("› sales  sales#7  spec", "old sales · 3d")
        assert rows[2] == ("› support  support#3  plan", "support plan · 2h")
        await pilot.press("A")
        await settle(pilot)
        assert table.border_title == "Decide next"
        assert _rows(app) == before
        assert table.cursor_row == 2


def test_rows_are_oldest_first_across_pipelines():
    rows = inbox_rows(ITEMS, set())
    assert [str(a).split()[1] for a, _ in rows] == ["sales", "eng", "support", "eng"]
    assert [str(a).split()[2] for a, _ in rows] == ["sales#7", "eng#2", "support#3", "eng#1"]


def test_stale_item_is_dimmed_and_marked():
    (a, b), = inbox_rows([_item("eng", "eng#1", HOUR, stale=True)], set())
    assert "(stale)" in str(b)
    assert "dim" in str(a.style)
    (_, b2), = inbox_rows([_item("eng", "eng#2", MIN)], set())
    assert "(stale)" not in str(b2)


def test_rows_skip_items_already_acted_on():
    assert len(inbox_rows(ITEMS, {("sales", "sales#7")})) == 3


async def test_empty_inbox_says_nothing_needs_you(monkeypatch):
    monkeypatch.setattr(dashboard, "_run", FakePl(items=[]))
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        assert _rows(app) == [("› Nothing needs you in any pipeline", "")]


@pytest.mark.parametrize("result", [subprocess.CompletedProcess([], 1, "", "\nboom\nmore"),
                                    subprocess.CompletedProcess([], 0, "not json", "")])
async def test_inbox_failure_shows_first_error_line(monkeypatch, result):
    monkeypatch.setattr(dashboard, "_run", FakePl(inbox=result))
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        (head, _), = _rows(app)
        assert head.startswith("› Could not read the inbox: ")
        if result.returncode:
            assert head.endswith("boom")
        await pilot.press("A")
        await settle(pilot)
        assert app.query_one("#decide", DataTable).border_title == "Decide next"
        assert len(_rows(app)) > 1


async def test_a_approves_in_the_items_pipeline_without_switching(monkeypatch):
    fake = FakePl()
    monkeypatch.setattr(dashboard, "_run", fake)
    monkeypatch.setattr(C, "PROFILE_NAME", "eng")
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        tab = app.active_tab
        await pilot.press("a")   # row 0 is the oldest: sales#7
        await pilot.pause()
        assert fake.gates() == []
        await pilot.press("y")
        await settle(pilot)
        assert fake.gates()[0][1:] == ["-m", "pl", "--profile", "sales", "approve", "sales#7"]
        assert C.PROFILE_NAME == "eng" and app.active_tab == tab and app.restart_into is None
        assert len(_rows(app)) == 3 and not any("sales#7" in h for h, _ in _rows(app))


async def test_a_asks_first_and_declining_runs_nothing(monkeypatch):
    fake = FakePl()
    monkeypatch.setattr(dashboard, "_run", fake)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await settle(pilot)
        assert fake.gates() == [] and len(_rows(app)) == 4


async def test_x_sends_back_with_notes_as_one_argument(monkeypatch):
    fake = FakePl()
    monkeypatch.setattr(dashboard, "_run", fake)
    app = _app()
    notes = 'it\'s "wrong"; rm -rf ~'
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        app.query_one("#decide", DataTable).move_cursor(row=2)   # support#3
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, InputModal)
        app.screen.query_one("Input").value = notes
        await pilot.press("enter")
        await settle(pilot)
        argv, kw = fake.calls[-2] if fake.calls[-1][0][3:] == ["inbox", "--json"] else fake.calls[-1]
        assert argv[3:] == ["--profile", "support", "reject", "support#3", notes]
        assert not kw.get("shell")


async def test_x_with_empty_notes_runs_nothing(monkeypatch):
    fake = FakePl()
    monkeypatch.setattr(dashboard, "_run", fake)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        assert fake.gates() == [] and len(_rows(app)) == 4


async def test_failed_approve_shows_error_and_keeps_row(monkeypatch):
    fake = FakePl(gate=subprocess.CompletedProcess([], 1, "", "\npl: card moved to Done\n"))
    monkeypatch.setattr(dashboard, "_run", fake)
    app = _app()
    told = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: told.append((msg, kw)))
    refreshed = []
    monkeypatch.setattr(app, "refresh_data", lambda *a, **k: refreshed.append(1))
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        refreshed.clear()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert any(m == "pl: card moved to Done" and kw.get("severity") == "error" for m, kw in told)
        assert len(_rows(app)) == 4
        assert refreshed == []


async def test_enter_opens_card_switching_pipeline_when_needed(monkeypatch):
    monkeypatch.setattr(dashboard, "_run", FakePl())
    monkeypatch.setattr(C, "PROFILE_NAME", "eng")
    monkeypatch.setattr("pl.board.card", lambda cid: {"id": cid, "title": "T", "description": "d"})
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        await pilot.press("enter")   # sales#7
        await pilot.pause()
        assert app.restart_into == ("profile", "sales", "sales#7", None)
    app = _app()
    async with app.run_test(size=(176, 48)) as pilot:
        await _all_on(pilot, app)
        app.query_one("#decide", DataTable).move_cursor(row=1)   # eng#2
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CardScreen) and app.screen.card_id == "eng#2"
