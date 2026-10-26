"""The live view of an agent's window: `w` on a Pipeline card opens it, `W` still attaches to tmux. Fake tmux, no real HOME."""
import subprocess

from textual.containers import VerticalScroll

from pl import config as C
from pl import harnesses
from pl.tui import cards as tui_cards
from pl.tui.app import PlApp
from pl.tui.live import LiveScreen
from test_tui_views import Provider, _open_cards, fake_data, fake_home, settle  # noqa: F401  (fake_home is an autouse fixture)

CARD = "4a000001aaaa"


class FakeTmux:
    def __init__(self, screen="", cmd="claude", code=0):
        self.screen, self.cmd, self.code, self.captures, self.calls = screen, cmd, code, [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[1] == "capture-pane":
            self.captures.append(argv)
            return subprocess.CompletedProcess(argv, self.code, self.screen, "")
        return subprocess.CompletedProcess(argv, 0, self.cmd + "\n", "")


def _data(worker=None):
    data = fake_data()
    row = next(r for r in data["snapshot"]["rows"] if (r.get("card") or {}).get("id") == CARD)
    row["worker"] = {"pane": "%9", "window": "@9", "stage": "plan"} if worker is None else worker
    return data


def _app(monkeypatch, tmux, worker=None):
    monkeypatch.setattr(harnesses, "_run", tmux)
    monkeypatch.setattr(tui_cards, "card", lambda cid: {"id": cid, "title": "t", "updated_at": "x", "description": "d"})
    return PlApp(snapshot_provider=Provider(_data(worker)))


def body(screen):
    return str(screen.query_one("#live-body").content)


def head(screen):
    return str(screen.query_one("#live-head").content)


async def _open_live(pilot, app):
    await _open_cards(pilot, app, CARD)
    await pilot.press("w")
    await settle(pilot)
    assert isinstance(app.screen, LiveScreen)
    return app.screen


async def test_w_opens_the_live_view_of_the_selected_cards_agent(monkeypatch):
    tmux = FakeTmux(screen="● Reading the plan\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        assert "Reading the plan" in body(live) and "working" in head(live)
        assert "plan agent" in head(live)
        assert tmux.captures and all(a[a.index("-t") + 1] == "%9" for a in tmux.captures)


async def test_live_view_refreshes_and_shows_the_status_line(monkeypatch):
    tmux = FakeTmux(screen="● Reading the plan\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        tmux.screen = "Reading done\nDo you want to proceed?\n"
        live._poll()
        await settle(pilot)
        assert "Do you want to proceed?" in body(live)
        assert "waiting for you: Do you want to proceed?" in head(live)


async def test_live_view_scrolls_with_pgup_pgdn_and_esc_returns(monkeypatch):
    tmux = FakeTmux(screen="\n".join(f"line {i}" for i in range(300)) + "\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        scroll = live.query_one(VerticalScroll)
        await settle(pilot)
        bottom = scroll.scroll_y
        assert bottom > 0
        await pilot.press("pageup")
        await settle(pilot)
        up = scroll.scroll_y
        assert up < bottom
        await pilot.press("pagedown")
        await settle(pilot)
        assert scroll.scroll_y > up
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, LiveScreen)


async def test_live_view_keeps_the_final_screen_when_the_agent_exits(monkeypatch):
    tmux = FakeTmux(screen="All done, PR opened.\n", cmd="zsh")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        assert "finished: All done, PR opened." in head(live)
        tmux.code = 1
        live._poll()
        await settle(pilot)
        assert "All done, PR opened." in body(live) and "no agent window" in head(live)


async def test_live_view_without_any_read_says_no_agent_window(monkeypatch):
    app = _app(monkeypatch, FakeTmux(code=1))
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        assert "no agent window" in body(live) and "no agent window" in head(live)


async def test_live_view_masks_secrets_and_sends_no_keys(monkeypatch):
    tmux = FakeTmux(screen="token ghp_" + "b" * 36 + "\n")
    app = _app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        live = await _open_live(pilot, app)
        assert "ghp_" not in body(live)
        for key in ("a", "enter", "y"):
            await pilot.press(key)
    assert not any(a[1] == "send-keys" for a in tmux.calls)


async def test_W_still_attaches_to_the_tmux_window(monkeypatch):
    jumped = []
    monkeypatch.setattr(tui_cards, "jump_to_window", lambda win: jumped.append(win) or "ok")
    app = _app(monkeypatch, FakeTmux())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app, CARD)
        await pilot.press("W")
        await settle(pilot)
        assert not isinstance(app.screen, LiveScreen)
    assert jumped == ["@9"]


async def test_w_on_a_card_without_an_agent_says_so(monkeypatch):
    app = _app(monkeypatch, FakeTmux(), worker={})
    notes = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_cards(pilot, app, CARD)
        await pilot.press("w")
        await settle(pilot)
        assert not isinstance(app.screen, LiveScreen)
    assert "this card has no agent window" in notes


def _loops_app(monkeypatch, tmux, names=("invoices",), panes=True):
    from pl.tui import loops as tui_loops
    C.SERVICES = {n: {"prompt": "p", "profile": "acme", "every": "hourly", "writes": False} for n in names}
    C.LOOPS_OFF = {}
    monkeypatch.setattr(harnesses, "_run", tmux)
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    data = fake_data()
    if panes:
        for i, n in enumerate(names, 1):
            data["snapshot"]["rows"].append({"loop": n, "kind": "loop_idle", "worker": {"pane": f"%{i}", "window": f"@{i}"}})
    return PlApp(snapshot_provider=Provider(data))


async def _open_loops(pilot):
    await settle(pilot)
    await pilot.press("5")
    await settle(pilot)


async def test_w_on_a_running_loop_opens_its_live_view(monkeypatch):
    tmux = FakeTmux(screen="checking invoices\n")
    app = _loops_app(monkeypatch, tmux)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        await pilot.press("down")
        await pilot.press("w")
        await settle(pilot)
        assert isinstance(app.screen, LiveScreen) and app.screen.pane == "%1"
        assert head(app.screen).startswith("invoices loop")
        assert "checking invoices" in body(app.screen)
        assert not [c for c in tmux.calls if c[1] == "send-keys"]


async def test_esc_from_the_loop_live_view_returns_to_the_same_row(monkeypatch):
    from pl.tui.loops import LoopsView
    app = _loops_app(monkeypatch, FakeTmux(screen="x\n"), names=("alpha", "beta"))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        await pilot.press("down", "down")
        before = app.query_one(LoopsView).selected()["name"]
        await pilot.press("w")
        await settle(pilot)
        assert isinstance(app.screen, LiveScreen)
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, LiveScreen)
        assert app.query_one(LoopsView).selected()["name"] == before == "beta"


async def test_w_on_a_row_with_a_pane_and_its_own_heading_opens_it(monkeypatch):
    from pl.tui.loops import LoopsView
    app = _loops_app(monkeypatch, FakeTmux(screen="x\n"))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        view = app.query_one(LoopsView)
        view.loops = [{"name": "x", "pane": "%4", "window": "@4", "heading": "review pl#12 agent"}]
        view.query_one("DataTable").clear()
        view.query_one("DataTable").add_row("x", key="x")
        view.query_one("DataTable").move_cursor(row=0)
        await pilot.press("w")
        await settle(pilot)
        assert isinstance(app.screen, LiveScreen) and app.screen.pane == "%4"
        assert head(app.screen).startswith("review pl#12 agent")


async def test_w_on_a_stopped_loop_shows_a_notice_and_opens_nothing(monkeypatch):
    app = _loops_app(monkeypatch, FakeTmux(), panes=False)
    notices = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notices.append(msg))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        await pilot.press("down")
        await pilot.press("w")
        await settle(pilot)
        assert not isinstance(app.screen, LiveScreen)
        assert [n for n in notices if "is not running" in n]


async def test_W_jumps_to_the_loops_tmux_window(monkeypatch):
    from pl import agents
    jumps = []
    monkeypatch.setattr(agents, "jump_to_window", lambda name: jumps.append(name) or "ok")
    app = _loops_app(monkeypatch, FakeTmux())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        await pilot.press("down")
        await pilot.press("W")
        await settle(pilot)
        await settle(pilot)
    assert jumps == ["invoices"]


async def test_W_on_a_loop_with_no_window_says_so(monkeypatch):
    app = _loops_app(monkeypatch, FakeTmux(), panes=False)
    notices = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notices.append(msg))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_loops(pilot)
        await pilot.press("down")
        await pilot.press("W")
        await settle(pilot)
        assert [n for n in notices if "has no window" in n]


def test_loops_footer_reads_live_view_and_tmux_window():
    from pl.tui.loops import LoopsView
    keys = {b.key: b.description for b in LoopsView.BINDINGS}
    assert keys["w"] == "live view" and keys["W"] == "tmux window"
