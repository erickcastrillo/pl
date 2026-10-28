"""Checklist items 5, 16j, 16l (alerts), 11 (notifications) and 4 (a console move during a GitHub budget hold)."""
import sqlite3
import time

import pytest
from textual.widgets import DataTable, TabbedContent

from conftest import press
from pl import board, commands, dispatch, heldwrites, notifications, trackers, watch
from pl import config as C
from pl.trackers.github import OverBudget
from pl.trackers.local import Local
from pl.util import short_id
from test_tui_views import screen_text
from tui_wait import settle, wait_until

RUN_WORKER = {"stage": "run", "pane": "%1", "session_id": "s1"}


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    monkeypatch.setattr(watch, "_gh", lambda args: [])


@pytest.fixture
def short_stall(monkeypatch):
    monkeypatch.setitem(C.DISPATCH, "stall_minutes", 1)


def seed(column, title, worker=None, description="", **meta):
    metadata = {"pipeline_mode": "auto", **meta, **({"worker": worker} if worker else {})}
    return trackers.get("tracker").create(column, title=title, description=description, metadata=metadata)


def column_of(card):
    trackers.reset("tracker")   # the local store is bound to the thread that opened it; the console's workers opened theirs
    fresh = trackers.get("tracker")
    return board.col_name(fresh.card(card["id"])["list_id"])


def idle(*cards, minutes=2):
    return {c["id"]: {"since": time.time() - minutes * 60, "session": "s1"} for c in cards}


def end_of_pass(st):
    cards = board.cards()
    dispatch.check_alerts(cards, set(), st)


async def refresh(pilot):
    board.fresh_next()
    pilot.app.run_worker(pilot.app.refresh_data, thread=True, group="keys")
    await settle(pilot)


async def alert_rows(pilot):
    app = pilot.app
    await refresh(pilot)
    await press(pilot, "7")
    app.query_one("#activity-views", TabbedContent).active = "alerts"
    await wait_until(pilot, lambda: app.query("#alerts-table"), "the alerts table", timeout=20.0)
    table = app.query_one("#alerts-table", DataTable)
    return [" ".join(str(cell) for cell in table.get_row_at(i)) for i in range(table.row_count)]


def named(rows, card):
    return [r for r in rows if short_id(card["id"]) in r]


async def test_item_5_stalled_run_agent_alert(console, short_stall):
    async with console() as pilot:
        stalled = seed("In progress", "stalled run", RUN_WORKER)
        control = seed("PR open", "run agent whose card moved on", RUN_WORKER)
        end_of_pass({"run_waits": idle(stalled, control)})
        rows = await alert_rows(pilot)
        assert any("idle" in r for r in named(rows, stalled)), rows
        assert not named(rows, control), rows


async def test_item_16j_spec_agent_finished_without_moving_alert(console, short_stall):
    spec_column = ((C.PIPELINE or {}).get("stages") or {}).get("spec", {}).get("column") or C.COLUMNS[0]
    worker = {"stage": "spec", "finished_turn": True, "pane": "%1", "session_id": "s1"}
    async with console() as pilot:
        unwritten = seed(spec_column, "spec agent finished, no SPEC", worker)
        written = seed(spec_column, "spec agent finished, SPEC written", worker, description=board.render({"SPEC": "The spec."}))
        end_of_pass({"stalls": {unwritten["id"]: {"missing": "SPEC"}, written["id"]: {"missing": "SPEC"}}})
        rows = await alert_rows(pilot)
        assert any("finished" in r for r in named(rows, unwritten)), rows
        assert not named(rows, written), rows


async def test_item_16l_run_agent_card_left_in_approved_alert(console, short_stall):
    async with console() as pilot:
        left = seed("Approved", "run agent idle, card still approved", RUN_WORKER)
        control = seed("PR open", "run agent whose card moved on", RUN_WORKER)
        end_of_pass({"run_waits": idle(left, control)})
        rows = await alert_rows(pilot)
        assert named(rows, left), rows
        assert not named(rows, control), rows


@pytest.fixture
def attention(tmp_path, monkeypatch):
    script = tmp_path / "attention.sh"
    script.write_text("#!/bin/sh\nprintf '%s\\t%s\\t%s\\n' \"$1\" \"$2\" \"$3\" >> \"$(dirname \"$0\")/calls.txt\"\n")
    script.chmod(0o755)
    monkeypatch.setattr(C, "ATTENTION", script)
    monkeypatch.setitem(C.NOTIFY, "events", list(notifications.C.NOTIFY_EVENTS))
    C.NOTIFY.pop("quiet", None)
    return tmp_path / "calls.txt"


async def test_item_11_pr_opened_merged_done_call_attention_cmd(console, attention):
    async with console() as pilot:
        await press(pilot, "7")
        opened = seed("Approved", "pr opens")
        merged = seed("PR open", "pr merges", pr_urls=["https://github.com/o/r/pull/7"])
        done = seed("PR open", "card done", None)
        moves = [(opened, "PR open"), (merged, "Done"), (done, "Done")]
        before = {c["id"]: board.col_name(c["list_id"]) for c, _ in moves}
        for c, column in moves:
            commands.move_to(c["id"], column)
        moved = [(commands.card(c["id"]), before[c["id"]], column) for c, column in moves]
        notifications.moves(moved)
        calls = attention.read_text().splitlines()
        titles = sorted(line.split("\t")[1] for line in calls)
        assert len(calls) == 3 and all(line.startswith("notify\t") for line in calls), calls
        assert titles == sorted([f"PR opened: {short_id(opened['id'])}", f"Merged: {short_id(merged['id'])} #7",
                                 f"Done: {short_id(done['id'])}"]), calls


@pytest.fixture
def budget(monkeypatch):
    # Local keeps one sqlite connection per tracker object, so a console worker thread fails on a board opened in another thread (product bug, follow-up).
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **{**kw, "check_same_thread": False}))
    state = {"spent": True}
    real = Local.update

    def update(self, item_id, **fields):
        if state["spent"] and "column" in fields:
            raise OverBudget(time.time() + 3600, "test budget spent")
        return real(self, item_id, **fields)
    monkeypatch.setattr(Local, "update", update)
    return state


async def move_selected(pilot, card, column):
    app = pilot.app
    await press(pilot, "3")
    await refresh(pilot)
    app.query_one("#work-views", TabbedContent).active = "cards"
    table = app.query_one("#cards-table", DataTable)
    await wait_until(pilot, lambda: card["id"] in {k.value for k in table.rows}, "the card row")
    table.focus()
    table.move_cursor(row=table.get_row_index(card["id"]))
    await press(pilot, "v")
    await wait_until(pilot, lambda: type(app.screen).__name__ == "ColumnPicker", "the column picker")
    options = app.screen.columns
    app.screen.dismiss(options[options.index(column)])
    await wait_until(pilot, lambda: type(app.screen).__name__ == "ConfirmScreen", "the confirm")
    await press(pilot, "y")
    await settle(pilot)


def row_text(pilot, card):
    table = pilot.app.query_one("#cards-table", DataTable)
    return " ".join(str(cell) for cell in table.get_row(card["id"]))


async def test_item_4_move_during_budget_hold_is_queued_not_done(console, budget, monkeypatch):
    async with console() as pilot:
        app = pilot.app
        card = seed("Spec ready", "moves during the hold")
        await move_selected(pilot, card, "Approved")
        await refresh(pilot)
        assert column_of(card) == "Spec ready"
        assert "move to Approved queued" in row_text(pilot, card), ([n.message for n in app._notifications], heldwrites.entries())
        assert not any(n.message.startswith("moved ") for n in app._notifications), list(app._notifications)
        await press(pilot, "enter")
        await wait_until(pilot, lambda: "move to Approved queued" in screen_text(app), "the held note on the card screen")
        await press(pilot, "escape")

        budget["spent"] = False
        monkeypatch.setattr(heldwrites, "_now", lambda: time.time() + 7200)
        heldwrites.replay(lambda *a: None)
        await refresh(pilot)
        assert column_of(card) == "Approved"
        assert "move to Approved queued" not in row_text(pilot, card)
