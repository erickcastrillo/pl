"""The Agent section in the Pipeline details pane: the selected card's agent screen, refreshed on a timer. Fake tmux only."""
import logging

import pytest
from rich.text import Text
from textual.widgets import Markdown

from pl import harnesses
from pl.tui import cards as tui_cards
from pl.tui.app import PlApp
from pl.tui.live import LiveScreen
from test_tui_live import CARD, FakeTmux, _data
from test_tui_views import Provider, _open_cards, fake_data, fake_home, settle  # noqa: F401  (fake_home is an autouse fixture)

OTHER = "60000002aaaa"


def _app(monkeypatch, tmux, worker=None, data=None):
    monkeypatch.setattr(harnesses, "_run", tmux)
    reads = []

    def read(cid):
        reads.append(cid)
        return {"id": cid, "title": "t", "updated_at": "x", "description": "d"}

    monkeypatch.setattr(tui_cards, "card", read)
    app = PlApp(snapshot_provider=Provider(data or _data(worker)))
    app.card_reads = reads
    return app


def section(app):
    content = app.query_one("#cards-agent").content
    return content if isinstance(content, Text) else Text(str(content))


async def _tick(pilot, app):
    app.query_one(tui_cards.CardsView)._agent_tick()
    await settle(pilot)


async def test_ac1_selected_card_shows_status_and_last_20_lines_in_color(monkeypatch):
    tmux = FakeTmux(screen="\x1b[31mred line\x1b[0m\nplain line\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        text = section(app)
        assert "Agent" in text.plain and "working" in text.plain
        assert "red line" in text.plain and "plain line" in text.plain and "\x1b" not in text.plain
        assert any(getattr(s.style, "color", None) and s.style.color.number == 1 for s in text.spans)
    mine = [a for a in tmux.captures if a[a.index("-t") + 1] == "%9"]
    assert mine and all(a[a.index("-S") + 1] == "-20" for a in mine)


async def test_ac2_token_on_screen_is_masked(monkeypatch, caplog):
    token = "ghp_" + "c" * 36
    app = _app(monkeypatch, FakeTmux(screen=f"token {token}\n"))
    with caplog.at_level(logging.DEBUG):
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_cards(pilot, app, CARD)
            await _tick(pilot, app)
            assert token not in section(app).plain and "ghp_" not in section(app).plain
    assert token not in caplog.text


@pytest.mark.parametrize("screen, cmd, expected", [
    ("● Reading the plan\n", "claude", "working"),
    ("GATE: not-approved\n", "claude", "waiting for you: GATE: not-approved"),
    ("All done, PR opened.\n", "zsh", "finished: All done, PR opened."),
    ("You've hit your usage limit. Resets at 5pm\n", "claude", "stopped: usage limit"),
    ("bash: foo: command not found\n", "claude", "stopped: missing prerequisite: bash: foo: command not found"),
    ("Error: request failed\n", "claude", "stopped: error: Error: request failed"),
])
async def test_ac3_status_lines(monkeypatch, screen, cmd, expected):
    app = _app(monkeypatch, FakeTmux(screen=screen, cmd=cmd))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        assert expected in section(app).plain.splitlines()[0]


async def test_ac4_tick_shows_new_text_without_reloading_head_or_body(monkeypatch):
    tmux = FakeTmux(screen="first screen\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        assert "first screen" in section(app).plain
        md, updates = view.query_one("#cards-md", Markdown), []
        monkeypatch.setattr(md, "update", lambda *a, **k: updates.append(a))
        scroll, reads = view.query_one("#cards-body").scroll_y, len(app.card_reads)
        tmux.screen = "second screen\n"
        await _tick(pilot, app)
        assert "second screen" in section(app).plain and "first screen" not in section(app).plain
        assert not updates and view.query_one("#cards-body").scroll_y == scroll
        assert len(app.card_reads) == reads


async def test_ac5_moving_the_selection_reads_only_the_new_card(monkeypatch):
    data = _data()
    row = next(r for r in data["snapshot"]["rows"] if (r.get("card") or {}).get("id") == OTHER)
    row["worker"] = {"pane": "%8", "window": "@8", "stage": "plan"}
    tmux = FakeTmux(screen="screen\n")
    app = _app(monkeypatch, tmux, data=data)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        t = view.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index(OTHER))
        await settle(pilot)
        tmux.captures.clear()
        await _tick(pilot, app)
        assert tmux.captures and all(a[a.index("-t") + 1] == "%8" for a in tmux.captures)


async def test_ac6_no_capture_on_another_tab_or_screen_and_refresh_on_return(monkeypatch):
    tmux = FakeTmux(screen="screen\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open_cards(pilot, app, CARD)
        app.action_tab("prs")
        await settle(pilot)
        tmux.captures.clear()
        await _tick(pilot, app)
        assert not tmux.captures
        app.action_tab("cards")
        await settle(pilot)
        assert any(a[a.index("-S") + 1] == "-20" for a in tmux.captures)
        await pilot.press("w")
        await settle(pilot)
        assert isinstance(app.screen, LiveScreen)
        tmux.captures.clear()
        view._agent_tick()
        await settle(pilot)
        assert not any(a[a.index("-S") + 1] == "-20" for a in tmux.captures)


class Raises(FakeTmux):
    def __call__(self, argv, **kw):
        raise FileNotFoundError("tmux")


@pytest.mark.parametrize("tmux, worker", [
    (FakeTmux(), {}),
    (Raises(), None),
    (FakeTmux(code=1), None),
])
async def test_ac7_no_pane_missing_tmux_or_error_code_says_no_agent_window(monkeypatch, tmux, worker):
    app = _app(monkeypatch, tmux, worker=worker)
    notes = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        assert "no agent window" in section(app).plain
        assert "Add a salesforce keyword" in str(app.query_one("#cards-detail").content)
        assert app.query_one("#cards-body").display
        assert app.is_running
    assert not any("render failed" in n for n in notes)


async def test_ac8_heading_row_or_empty_list_has_no_agent_section_and_no_tmux(monkeypatch):
    tmux = FakeTmux(screen="screen\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open_cards(pilot, app, CARD)
        await _tick(pilot, app)
        assert app.query_one("#cards-agent").display
        monkeypatch.setattr(view, "_current", lambda: ("group:Plan for review", None))   # the cursor skips headings; a move can pass over one
        tmux.calls.clear()
        await _tick(pilot, app)
        assert not app.query_one("#cards-agent").display and not tmux.calls

    empty = fake_data()
    empty["snapshot"]["rows"] = []
    tmux = FakeTmux(screen="screen\n")
    app = _app(monkeypatch, tmux, data=empty)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app)
        await _tick(pilot, app)
        assert not app.query_one("#cards-agent").display and not tmux.calls
