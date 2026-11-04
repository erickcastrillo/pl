"""Plan 33 slice 2: the Review tab's Waiting list shows everything needs_you() returns, oldest first."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console
from textual.app import App

from pl import config as C
from pl.tui import cards as tui_cards
from pl.tui import prs as tui_prs
from pl.tui import review as tui_review
from pl.tui.needs import needs_you_count
from pl.tui.prs import MergeScreen, PrsView
from pl.tui.review import ConfirmScreen, ReviewScreen
from pl.tui.waiting import WaitingView, kind_of, row_text, waiting_rows
from tui_wait import wait_until

NOW = datetime.now(timezone.utc)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _card(cid, title, col, kind="row", hours=5, **extra):
    c = {"id": cid, "title": title, "tags": [], "updated_at": _iso(hours) if hours is not None else None,
         "metadata": {"pipeline_mode": "auto"}, "list_id": col}
    return {"kind": kind, "card": c, "worker": {}, "win": "", "col": col, "text": title, **extra}


def _pr(repo, number, title, state, hours=5):
    pr = {"repo": repo, "number": number, "title": title, "state": state, "updatedAt": _iso(hours),
          "url": f"https://github.com/o/{repo}/pull/{number}"}
    return {"kind": "review", "card": None, "pr": pr, "col": "PRs", "worker": {}, "win": "", "text": title}


def _data(*rows):
    return {"snapshot": {"rows": list(rows)}}


def _board():
    return _data(
        _card("spec0001aaaa", "a spec", "Spec ready", hours=2),
        _card("plan0002aaaa", "a plan", "Plan for review", kind="review", hours=96),
        _pr("frontend", 1117, "decide me", "decide", hours=10),
        _pr("frontend", 1200, "merge me", "merge", hours=20),
        _card("manl0003aaaa", "do it yourself", "Manual", hours=30),
        _card("need0004aaaa", "the agent asks", "In progress", kind="needs", hours=50))


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    monkeypatch.setattr(C, "GATES", {"spec": True})
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_six_kinds_one_row_each():
    rows = waiting_rows(_board())
    assert sorted(kind_of(r) for r in rows) == sorted(["spec", "plan", "PR", "PR", "manual", "agent"])


def test_oldest_first_and_no_time_last():
    data = _data(_card("a0000001aaaa", "newest", "Manual", hours=1), _card("b0000002aaaa", "no time", "Manual", hours=None),
                 _card("c0000003aaaa", "oldest", "Manual", hours=90), _card("d0000004aaaa", "middle", "Manual", hours=40))
    assert [r["card"]["title"] for r in waiting_rows(data)] == ["oldest", "middle", "newest", "no time"]


@pytest.mark.parametrize("data", [
    _data(), {}, _data(_pr("api", 1, "fixing", "rework"), _pr("api", 2, "also fixing", "rework")),
    _board(), _data(*_board()["snapshot"]["rows"], _card("w0000005aaaa", "busy", "In progress", kind="working"))])
def test_list_length_equals_needs_you_count(data):
    assert len(waiting_rows(data)) == needs_you_count(data)


def test_rework_pr_and_working_card_are_left_out():
    data = _data(_pr("api", 1, "fixing", "rework"), _card("w0000005aaaa", "busy", "In progress", kind="working"))
    assert waiting_rows(data) == []


def test_row_fits_80_columns():
    row = _pr("frontend", 1117, "x" * 200, "decide", hours=3)
    out = io.StringIO()
    Console(width=80, file=out, force_terminal=False).print(row_text(row), no_wrap=True)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1 and len(lines[0]) <= 80 and "3h" in lines[0]


class Host(App):
    """A console reduced to the two views the Waiting list talks to."""
    active_tab = "waiting"

    def __init__(self, data):
        super().__init__()
        self.data, self.tabs = data, []

    def compose(self):
        yield WaitingView()
        yield PrsView()

    def on_mount(self):
        self.query_one(WaitingView).show(self.data)
        self.query_one(PrsView).show(self.data)

    def action_tab(self, name):
        self.tabs.append(name)

    def refresh_data(self):
        pass


class Calls:
    def __init__(self):
        self.made = []

    def __call__(self, *a, **kw):
        self.made.append((a, kw))
        raise AssertionError("no command should run")


@pytest.fixture
def commands(monkeypatch):
    approved = []
    monkeypatch.setattr(tui_review, "run_command", lambda app, fn, after=None, **kw: approved.append(kw["id"]))
    runner = Calls()
    monkeypatch.setattr(tui_prs, "_run", runner)
    monkeypatch.setattr(tui_cards, "card", lambda cid: {"id": cid, "title": "t", "updated_at": "x", "description": "body"})
    monkeypatch.setattr(tui_review, "load_review", lambda kind, cid: (_ for _ in ()).throw(SystemExit("no board")))
    return approved, runner


async def _select(pilot, app, cid):
    await wait_until(pilot, lambda: app.query_one("#waiting-table"), "the waiting list")
    table = app.query_one("#waiting-table")
    await wait_until(pilot, lambda: cid in [k.value for k in table.rows], "the row to be listed")
    table.focus()
    table.move_cursor(row=table.get_row_index(cid))
    await wait_until(pilot, lambda: table.cursor_row == table.get_row_index(cid), "the cursor to reach the row")


async def test_a_on_a_plan_opens_the_confirm_and_approves(commands):
    approved, _ = commands
    app = Host(_board())
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, "plan0002aaaa")
        await pilot.press("a")
        await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the confirm screen")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_until(pilot, lambda: approved, "the plan to be approved")
        assert approved == ["plan0002aaaa"]


async def test_x_on_a_spec_opens_review_notes_first(commands):
    app = Host(_board())
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, "spec0001aaaa")
        await pilot.press("x")
        await wait_until(pilot, lambda: isinstance(app.screen, ReviewScreen), "the review screen")
        assert isinstance(app.screen, ReviewScreen) and app.screen.notes_first


async def test_a_on_a_merge_pr_opens_the_merge_confirm(commands):
    app = Host(_board())
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, "https://github.com/o/frontend/pull/1200")
        await pilot.press("a")
        await wait_until(pilot, lambda: isinstance(app.screen, MergeScreen), "the merge confirm")
        assert isinstance(app.screen, MergeScreen) and "frontend#1200" in app.screen.message


async def test_enter_opens_the_right_screen(commands):
    app = Host(_board())
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, "plan0002aaaa")
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, ReviewScreen), "the review screen")
        assert isinstance(app.screen, ReviewScreen) and not app.screen.notes_first
        await pilot.press("escape")
        for cid in ("manl0003aaaa", "need0004aaaa"):
            await _select(pilot, app, cid)
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(app.screen, tui_cards.CardScreen), "the card screen")
            assert isinstance(app.screen, tui_cards.CardScreen) and app.screen.card_id == cid
            await pilot.press("escape")
            await wait_until(pilot, lambda: not isinstance(app.screen, tui_cards.CardScreen), "the card screen to close")
        url = "https://github.com/o/frontend/pull/1117"
        await _select(pilot, app, url)
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.tabs == ["prs"], "the PRs tab")
        assert app.tabs == ["prs"] and app.query_one(PrsView)._current()[0] == url


async def test_enter_on_a_failed_agent_shows_the_retry_hint(commands):
    dead = _card("dead0005aaaa", "it died", "In progress", kind="needs", failed=True, approved="died 3× · t to retry")
    app = Host(_data(dead))
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, "dead0005aaaa")
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, tui_cards.CardScreen), "the card screen")
        assert isinstance(app.screen, tui_cards.CardScreen) and app.screen.approved == "died 3× · t to retry"


@pytest.mark.parametrize("cid,key", [("https://github.com/o/frontend/pull/1117", "a"), ("manl0003aaaa", "a"),
                                     ("need0004aaaa", "a"), ("https://github.com/o/frontend/pull/1200", "x"),
                                     ("manl0003aaaa", "x")])
async def test_keys_that_do_not_apply_only_notify(commands, cid, key):
    approved, runner = commands
    app = Host(_board())
    async with app.run_test(size=(120, 30)) as pilot:
        await _select(pilot, app, cid)
        before = list(app.screen_stack)
        await pilot.press(key)
        await wait_until(pilot, lambda: app._notifications, "the notification")
        assert app.screen_stack == before and not approved and not runner.made
        assert len(app._notifications) == 1


async def test_empty_list_says_nothing_waits(commands):
    app = Host(_data())
    async with app.run_test(size=(120, 30)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#waiting-table").display, "the empty list")
        assert not app.query_one("#waiting-table").display
        assert "Nothing waits on you" in str(app.query_one("#waiting-empty").render())
