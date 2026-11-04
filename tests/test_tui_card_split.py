"""The card screen beside its agent's live output. Fake tmux, no real HOME."""
import subprocess

import pytest

from pl import agent_screen, harnesses
from pl.tui import cards as tui_cards
from pl.tui.app import PlApp
from pl.tui.live import AgentPane, agent_head
from test_tui_views import Provider, fake_data, fake_home, settle  # noqa: F401  (fake_home is an autouse fixture)

CARD = "4a000001aaaa"
WORKER = {"pane": "%9", "window": "@9", "stage": "plan", "profile": "claude", "model": "opus", "started_at": "2026-11-03T00:00:00+00:00"}


class FakeTmux:
    def __init__(self, screen="", alive=True):
        self.screen, self.alive, self.calls = screen, alive, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, 0, self.screen, "")
        if argv[1] == "display-message":
            return subprocess.CompletedProcess(argv, 0, "%9\n" if self.alive else "\n", "")
        return subprocess.CompletedProcess(argv, 0, "claude\n", "")


def _app(monkeypatch, tmux, worker=WORKER):
    monkeypatch.setattr(harnesses, "_run", tmux)
    monkeypatch.setattr(tui_cards.trackers, "get", lambda name: (_ for _ in ()).throw(RuntimeError("no tracker")))
    monkeypatch.setattr(tui_cards.heldwrites, "queued_moves", dict)
    meta = {"worker": worker} if worker is not None else {}
    monkeypatch.setattr(tui_cards, "card", lambda cid: {"id": cid, "title": "t", "updated_at": "x", "description": "# Plan\n\n" + "line\n\n" * 80, "metadata": meta})
    return PlApp(snapshot_provider=Provider(fake_data()))


async def _open(pilot, app):
    await settle(pilot)
    await app.push_screen(tui_cards.CardScreen(CARD, "Plan for review"))
    for _ in range(40):
        if getattr(app.screen, "card", None) is not None:
            break
        await pilot.pause(0.05)
    await settle(pilot)
    return app.screen


def _body(screen):
    return str(screen.query_one("#agent-body").content)


def _run(out="", code=0):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, "")


def test_pane_alive_true_only_when_tmux_names_the_pane(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", _run("%9\n"))
    assert agent_screen.pane_alive("%9") is True
    assert agent_screen.pane_alive("%8") is False


def test_pane_alive_false_when_tmux_prints_nothing_and_exits_0(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", _run("\n"))
    assert agent_screen.pane_alive("%9") is False
    assert agent_screen.pane_alive("") is False


def test_pane_alive_unknown_on_oserror(monkeypatch):
    def boom(argv, **kw):
        raise OSError("no tmux")
    monkeypatch.setattr(harnesses, "_run", boom)
    assert agent_screen.pane_alive("%9") is None


async def test_ac1_live_pane_gives_two_full_height_columns_with_a_divider(monkeypatch):
    app = _app(monkeypatch, FakeTmux("● Reading\n"))
    async with app.run_test(size=(120, 40)) as pilot:
        s = await _open(pilot, app)
        left, right = s.query_one("#card-left").region, s.query_one(AgentPane).region
        assert left.right <= right.x and left.y == right.y and left.height == right.height > 0
        assert s.query_one(AgentPane).styles.border_left[0] == "vkey"


@pytest.mark.parametrize("kind", ["none", "gone", "other-host"])
async def test_ac2_no_live_agent_gives_full_width_and_no_right_column(monkeypatch, kind):
    worker = {"none": None, "gone": WORKER, "other-host": {**WORKER, "host": "elsewhere"}}[kind]
    app = _app(monkeypatch, FakeTmux(alive=kind != "gone"), worker)
    async with app.run_test(size=(120, 40)) as pilot:
        s = await _open(pilot, app)
        assert not s.query_one(AgentPane).display and not s.has_class("-split")
        assert s.query_one("#card-left").region.width == s.region.width


async def test_ac3_narrow_terminal_stays_side_by_side(monkeypatch):
    app = _app(monkeypatch, FakeTmux("x\n"))
    async with app.run_test(size=(50, 30)) as pilot:
        s = await _open(pilot, app)
        assert s.query_one("#card-left").region.width > 0 and s.query_one(AgentPane).region.width > 0
        assert s.query_one("#card-doc").region.height > 0


async def test_ac4_new_pane_text_with_colours_appears_within_about_a_second(monkeypatch):
    tmux = FakeTmux("one\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        s = await _open(pilot, app)
        tmux.screen = "\x1b[31mred text\x1b[0m\n"
        for _ in range(15):
            await pilot.pause(0.1)
        body = s.query_one("#agent-body").content
        assert "red text" in body.plain and body.spans


async def test_ac5_secret_is_masked_in_body_and_header(monkeypatch):
    token = "ghp_" + "a" * 36
    app = _app(monkeypatch, FakeTmux(f"token {token}\nUse {token}?\n"))
    async with app.run_test(size=(120, 40)) as pilot:
        s = await _open(pilot, app)
        assert token not in _body(s) and token not in str(s.query_one("#agent-head").content)


async def test_ac6_follows_at_bottom_and_scrolled_up_position_holds(monkeypatch):
    tmux = FakeTmux("\n".join(f"l{i}" for i in range(200)) + "\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 30)) as pilot:
        s = await _open(pilot, app)
        sc = s.query_one("#agent-scroll")
        assert sc.scroll_y >= sc.max_scroll_y - 1 > 0
        sc.scroll_to(y=10, animate=False)
        tmux.screen += "new line\n"
        for _ in range(15):
            await pilot.pause(0.1)
        assert sc.scroll_y == 10 and "new line" not in _body(s)
        await pilot.press("G")
        for _ in range(15):
            await pilot.pause(0.1)
        assert sc.scroll_y >= sc.max_scroll_y - 1 and "new line" in _body(s)


def test_ac7_header_shows_stage_account_model_status_elapsed_and_leaves_out_missing():
    h = agent_head(WORKER, "working", "")
    assert h.plain.startswith("plan agent · claude (opus) · working · ") and h.plain.endswith("d")
    assert agent_head({"stage": "run"}, "working", "").plain == "run agent · working"


async def test_ac9_keys_and_clicks_send_nothing_to_the_agent(monkeypatch):
    tmux = FakeTmux("x\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app)
        for k in ("tab", "down", "pagedown", "end", "G", "enter"):
            await pilot.press(k)
        await pilot.click("#agent-body")
        await settle(pilot)
        assert not any("send-keys" in a for a in tmux.calls)


async def test_ac14_tab_moves_focus_and_keys_scroll_only_the_focused_side(monkeypatch):
    app = _app(monkeypatch, FakeTmux("\n".join(f"l{i}" for i in range(200)) + "\n"))
    async with app.run_test(size=(120, 30)) as pilot:
        s = await _open(pilot, app)
        doc, agent = s.query_one("#card-doc"), s.query_one("#agent-scroll")
        assert doc.has_focus
        await pilot.press("tab")
        assert agent.has_focus
        before = doc.scroll_y
        await pilot.press("pageup")
        await settle(pilot)
        assert doc.scroll_y == before
        await pilot.press("tab")
        assert doc.has_focus


@pytest.mark.parametrize("key", ["a", "x", "p", "R", "d", "y", "O"])
async def test_ac15_card_actions_work_from_the_split(monkeypatch, key):
    async def enabled(worker):
        app = _app(monkeypatch, FakeTmux("x\n"), worker)
        async with app.run_test(size=(120, 40)) as pilot:
            s = await _open(pilot, app)
            return s.check_action({"a": "approve", "x": "send_back", "p": "hold", "R": "restart", "d": "drop",
                                   "y": "copy", "O": "open"}[key], ())
    assert await enabled(WORKER) == await enabled({"stage": "plan"})
