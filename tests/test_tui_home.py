"""Tests for the Home tab and single-profile rule (WP2.1)."""
import json
import time
from pathlib import Path

from pl import config as C
from pl.tui.app import PlApp
from pl.tui.home import home_rows, HomeView


def _dismissed(monkeypatch, tmp_path):
    """A profile past its Get started checklist."""
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    (tmp_path / "getstarted.json").write_text(json.dumps({"dismissed": 1}))


def _fake_summaries(now=None):
    now = now or time.time()
    return [
        {
            "version": 1,
            "name": "sales",
            "pipeline": "new-client",
            "running": True,
            "paused": False,
            "at": now - 10,
            "interval": 30,
            "last_good_refresh": now - 10,
            "columns": [{"title": "Lead", "cards": 3}, {"title": "Proposal", "cards": 1}],
            "needs_you": 2,
            "live_agents": 1,
            "oldest_wait": {"card": "sales#1", "since": now - 600},
            "alerts": 0,
        },
        {
            "version": 1,
            "name": "billing",
            "pipeline": "billing",
            "running": True,
            "paused": False,
            "at": now - 5,
            "interval": 30,
            "last_good_refresh": now - 5,
            "columns": [{"title": "Invoice", "cards": 5}],
            "needs_you": 0,
            "live_agents": 0,
            "oldest_wait": None,
            "alerts": 1,
        },
        {
            "version": 1,
            "name": "eng",
            "pipeline": "engineering",
            "running": False,
            "paused": False,
            "at": now - 20,
            "interval": 30,
            "last_good_refresh": now - 20,
            "columns": [{"title": "Spec ready", "cards": 2}, {"title": "PR open", "cards": 4}],
            "needs_you": 3,
            "live_agents": 2,
            "oldest_wait": {"card": "eng#42", "since": now - 3600},
            "alerts": 2,
        },
    ]


async def test_home_shows_one_row_per_pipeline_with_bold_needs_you(monkeypatch, tmp_path):
    _dismissed(monkeypatch, tmp_path)
    home = Path.home()
    # Setup 2 profiles so Home tab is available
    (home / ".pl-sales").mkdir(parents=True, exist_ok=True)
    (home / ".pl-sales" / "config.toml").write_text("[tracker]\ntype = 'memory'\n")
    (home / ".pl-billing").mkdir(parents=True, exist_ok=True)
    (home / ".pl-billing" / "config.toml").write_text("[tracker]\ntype = 'memory'\n")

    summaries = _fake_summaries()
    rows = home_rows(summaries)
    assert len(rows) == 3
    assert rows[0]["name"] == "sales"
    assert rows[0]["running"] is True
    assert "Lead 3" in rows[0]["cards"] and "Proposal 1" in rows[0]["cards"]
    assert rows[0]["needs_you"] == 2
    assert rows[0]["live_agents"] == 1
    assert rows[0]["oldest_wait"] == "10m"
    assert rows[0]["alerts"] == 0

    data = {
        "snapshot": {"rows": [], "prof": "ok", "parked": False, "at": "12:00:00",
                     "summary": "ok", "summary2": "", "needs": {}, "disp": "running"},
        "home": summaries,
    }
    app = PlApp(snapshot_provider=lambda: data)
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert app.query(HomeView)
        home_view = app.query_one(HomeView)
        table = home_view.query_one("DataTable")
        assert len(table.ordered_rows) == 4
        assert app.active_tab == "dashboard"
        assert not any("Home" in str(t.label) for t in app.query_one("#tabs").query_one("Tabs").query("Tab") if t.display)
        await pilot.press("H")
        await pilot.pause()
        assert app.active_tab == "home"
        cell = table.get_cell_at((1, 3))
        assert getattr(cell, "style", None) and "bold" in str(cell.style)


def test_home_refresh_reads_only_summary_files(monkeypatch):
    import tomllib
    from pl.tui.app import default_provider

    home = Path.home()
    p1 = home / ".pl-alpha"
    p2 = home / ".pl-beta"
    (p1 / "state").mkdir(parents=True, exist_ok=True)
    (p2 / "state").mkdir(parents=True, exist_ok=True)
    (p1 / "state" / "summary.json").write_text(json.dumps({"name": "alpha", "running": True}))
    (p2 / "state" / "summary.json").write_text(json.dumps({"name": "beta", "running": False}))

    def forbidden(*args, **kwargs):
        raise RuntimeError("Must not load config or parse TOML during home refresh!")

    monkeypatch.setattr(tomllib, "loads", forbidden)
    monkeypatch.setattr(C, "load", forbidden)

    from pl import board, watch
    monkeypatch.setattr(board, "share", lambda *a: None)
    monkeypatch.setattr(watch, "watch_snapshot", lambda: {"rows": [], "prof": "", "summary": "", "disp": "running", "at": ""})
    monkeypatch.setattr(watch, "pr_activity", lambda: {})

    res = default_provider()
    assert "home" in res
    names = [s.get("name") for s in res["home"] if s]
    assert "alpha" in names and "beta" in names


async def test_one_profile_skips_home_and_opens_as_today(monkeypatch, tmp_path):
    _dismissed(monkeypatch, tmp_path)
    home = Path.home()
    # Only 1 profile in home
    (home / ".pl-only").mkdir(parents=True, exist_ok=True)
    (home / ".pl-only" / "config.toml").write_text("[tracker]\ntype = 'memory'\n")

    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": [], "prof": "", "parked": False, "at": "", "summary": "", "disp": "running"}})
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert not app.query(HomeView)
        assert app.active_tab == "dashboard"
        await pilot.press("H")
        await pilot.pause()
        assert app.active_tab == "dashboard"


def test_a_stale_summary_shows_stopped():
    now = time.time()
    stale = [{
        "name": "stale-pipe",
        "running": True,
        "at": now - 100,
        "interval": 30,
        "columns": [],
    }]
    rows = home_rows(stale)
    assert len(rows) == 1
    assert rows[0]["running"] is False
    assert rows[0]["status"] == "stopped"

    fresh = [{
        "name": "fresh-pipe",
        "running": True,
        "at": now - 10,
        "interval": 30,
        "columns": [],
    }]
    fresh_rows = home_rows(fresh)
    assert fresh_rows[0]["running"] is True
    assert fresh_rows[0]["status"] == "running"


def test_oldest_wait_as_written_by_summary_py_is_an_iso_time():
    # summary.py writes "since" as the card's updated_at (an ISO string), not epoch seconds
    from datetime import datetime, timezone
    since = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat(timespec="seconds")
    s = {"name": "pl", "running": True, "at": time.time(), "interval": 30, "columns": [],
         "oldest_wait": {"card": "pl#1", "since": since}}
    assert home_rows([s])[0]["oldest_wait"] == "10m"
    s["oldest_wait"] = {"card": "pl#1", "since": "not a time"}
    assert home_rows([s])[0]["oldest_wait"] == "-"


def test_enter_on_home_opens_the_pipeline(monkeypatch):
    calls = []
    view = HomeView()
    monkeypatch.setattr(C, "PROFILE_NAME", "eng")

    class _App:
        def switch_to(self, profile, card=None, tab=None):
            calls.append(("switch", profile))

        def action_tab(self, tid):
            calls.append(("tab", tid))

    monkeypatch.setattr(HomeView, "app", property(lambda self: _App()))
    for name in ("sales", "eng"):
        monkeypatch.setattr(view, "selected", lambda name=name: {"name": name})
        view.action_select()
    assert calls == [("switch", "sales"), ("tab", "cards")]


async def test_home_has_a_new_pipeline_row_above_the_pipelines(monkeypatch, tmp_path):
    _dismissed(monkeypatch, tmp_path)
    data = {"home": _fake_summaries()}
    from textual.app import App

    class _Host(App):
        def compose(self):
            yield HomeView()

    async with _Host().run_test(size=(176, 48)) as pilot:
        view = pilot.app.query_one(HomeView)
        view.show(data)
        await pilot.pause()
        table = view.query_one("DataTable")
        assert str(table.get_cell_at((0, 0))) == "+ New pipeline"
        table.move_cursor(row=0)
        assert view.selected() is None


async def test_enter_on_the_new_pipeline_row_opens_the_wizard(monkeypatch, tmp_path):
    _dismissed(monkeypatch, tmp_path)
    from textual.app import App
    from pl.tui.newpipeline import NewPipelineScreen

    class _Host(App):
        def compose(self):
            yield HomeView()

    async with _Host().run_test(size=(176, 48)) as pilot:
        view = pilot.app.query_one(HomeView)
        view.show({"home": _fake_summaries()})
        await pilot.pause()
        view.query_one("DataTable").move_cursor(row=0)
        view.action_select()
        await pilot.pause()
        assert isinstance(pilot.app.screen, NewPipelineScreen)


async def test_n_opens_the_wizard_once_home_is_reached_with_h():
    from pl.tui.newpipeline import NewPipelineScreen

    for name in ("sales", "billing"):
        (Path.home() / f".pl-{name}").mkdir(parents=True, exist_ok=True)
        (Path.home() / f".pl-{name}" / "config.toml").write_text("[tracker]\ntype = 'memory'\n")
    data = {
        "snapshot": {"rows": [], "prof": "ok", "parked": False, "at": "12:00:00",
                     "summary": "ok", "summary2": "", "needs": {}, "disp": "running"},
        "home": _fake_summaries(),
    }
    app = PlApp(snapshot_provider=lambda: data)
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NewPipelineScreen)


async def _home_app():
    home = Path.home()
    for n in ("sales", "billing"):
        (home / f".pl-{n}").mkdir(parents=True, exist_ok=True)
        (home / f".pl-{n}" / "config.toml").write_text("[tracker]\ntype = 'memory'\n")
    data = {"snapshot": {"rows": [], "prof": "ok", "parked": False, "at": "12:00:00",
                         "summary": "ok", "summary2": "", "needs": {}, "disp": "running"},
            "home": _fake_summaries()}
    return PlApp(snapshot_provider=lambda: data)


async def test_home_first_row_is_new_pipeline_and_y_copies_the_add_command(monkeypatch):
    from pl.tui import home as home_mod
    copied = []
    monkeypatch.setattr(home_mod, "copy_text", lambda app, text: copied.append(text))
    app = await _home_app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
        view = app.query_one(HomeView)
        table = view.query_one("DataTable")
        assert str(table.get_cell_at((0, 0))) == "+ New pipeline"
        assert str(table.get_cell_at((0, 1))) == home_mod.NEW_PIPELINE
        assert home_mod.NEW_PIPELINE == "pl pipelines add NAME --template T --board local"
        assert str(table.get_cell_at((1, 0))) == "sales"
        assert table.cursor_row == 1 and view.selected()["name"] == "sales"
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("y")
        await pilot.pause()
        assert copied == [home_mod.NEW_PIPELINE]


async def test_on_the_new_pipeline_row_enter_opens_the_wizard_and_space_does_nothing(monkeypatch):
    app = await _home_app()
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
        calls = []
        monkeypatch.setattr(app, "switch_to", lambda *a, **k: calls.append("switch"))
        monkeypatch.setattr(app, "action_tab", lambda *a, **k: calls.append("tab"))
        monkeypatch.setattr(app, "push_screen", lambda *a, **k: calls.append("push"))
        table = app.query_one(HomeView).query_one("DataTable")
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("space")
        await pilot.pause()
        assert calls == []
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["push"]


def _gs_profile(monkeypatch, tmp_path, tracker=None, accounts=None, stages=None, pipeline=None):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(C, "TRACKER", tracker or {})
    monkeypatch.setattr(C, "ACCOUNTS", accounts or {})
    monkeypatch.setattr(C, "STAGES", stages or {})
    monkeypatch.setattr(C, "PIPELINE", pipeline)


def _gs_data(*cols, done=(), pr=None):
    rows = [{"kind": "row", "card": {"id": f"x#{i}"}, "col": c, "pr": pr} for i, c in enumerate(cols)]
    rows.append({"kind": "loop_idle", "card": None, "col": "Loops"})
    return {"snapshot": {"rows": rows, "done": [{"kind": "row", "card": {"id": "d#1"}, "col": c} for c in done],
                         "prof": "", "parked": False, "at": "", "summary": "", "disp": "running"}}


def test_get_started_ticks_follow_profile_state(monkeypatch, tmp_path):
    from pl.tui import getstarted
    _gs_profile(monkeypatch, tmp_path)
    assert [i[0] for i in getstarted.ITEMS] == ["board", "accounts", "idea", "spec", "plan", "pr"]
    assert not any(getstarted.ticks(_gs_data()).values())
    _gs_profile(monkeypatch, tmp_path, tracker={"type": "memory"}, accounts={"a": {}})
    t = getstarted.ticks(_gs_data("Inbox"))
    assert t == {"board": True, "accounts": True, "idea": True, "spec": False, "plan": False, "pr": False}
    t = getstarted.ticks(_gs_data("Approved"))
    assert t["spec"] and t["plan"] and not t["pr"]
    assert all(getstarted.ticks(_gs_data("PR open")).values())
    assert all(getstarted.ticks(_gs_data("Approved", pr="https://x/pull/1")).values())
    t = getstarted.ticks(_gs_data(done=["Done"]))
    assert t["spec"] and t["plan"] and t["pr"]


def test_get_started_accounts_unticked_when_a_stage_names_an_unknown_account(monkeypatch, tmp_path):
    from pl.tui import getstarted
    _gs_profile(monkeypatch, tmp_path, accounts={"a": {}}, stages={"run": {"account": "ghost"}})
    assert getstarted.ticks(_gs_data())["accounts"] is False
    _gs_profile(monkeypatch, tmp_path, accounts={"a": {}}, stages={"run": {"account": "a"}})
    assert getstarted.ticks(_gs_data())["accounts"] is True


def test_get_started_custom_columns_never_crash(monkeypatch, tmp_path):
    from pl.tui import getstarted
    _gs_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(C, "STAGE_DONE_AT", {"spec": "Nope", "plan": "Nada", "run": "Never"})
    t = getstarted.ticks(_gs_data("Inbox"))
    assert t["idea"] and not (t["spec"] or t["plan"] or t["pr"])


def test_get_started_starts_only_without_cards_and_dismiss_persists(monkeypatch, tmp_path):
    from pl.tui import getstarted
    first = tmp_path / "first"
    first.mkdir()
    _gs_profile(monkeypatch, first)
    assert getstarted.active(_gs_data("Inbox")) is False
    assert not (first / "getstarted.json").exists()
    assert getstarted.active(_gs_data()) is True
    assert "started" in json.loads((first / "getstarted.json").read_text())
    assert getstarted.active(_gs_data("Inbox")) is True
    getstarted.mark(dismissed=time.time())
    assert "dismissed" in getstarted.marks()
    assert getstarted.active(_gs_data()) is False
    other = tmp_path / "other"
    other.mkdir()
    _gs_profile(monkeypatch, other)
    assert getstarted.active(_gs_data()) is True


def test_get_started_hides_for_good_when_all_ticked(monkeypatch, tmp_path):
    from pl.tui import getstarted
    _gs_profile(monkeypatch, tmp_path, tracker={"type": "memory"}, accounts={"a": {}})
    assert getstarted.active(_gs_data()) is True
    assert getstarted.active(_gs_data("PR open")) is False
    assert "done" in getstarted.marks()
    assert getstarted.active(_gs_data()) is False


def test_get_started_wanted_before_the_first_refresh(monkeypatch, tmp_path):
    from pl.tui import getstarted
    _gs_profile(monkeypatch, tmp_path)
    assert getstarted.wanted() is True
    (tmp_path / "summary.json").write_text(json.dumps({"columns": [{"title": "Inbox", "cards": 2}]}))
    assert getstarted.wanted() is False
    getstarted.mark(started=1)
    assert getstarted.wanted() is True
    getstarted.mark(dismissed=1)
    assert getstarted.wanted() is False


def test_get_started_text_lists_items_with_keys_or_collapses():
    from pl.tui import getstarted
    t = {"board": True, "accounts": False, "idea": False, "spec": False, "plan": False, "pr": False}
    full = getstarted.text(t, False).plain
    assert full.splitlines()[0] == "Get started (1/6)"
    assert "✓ board connected" in full and "press I" in full
    assert [full.index(i[1]) for i in getstarted.ITEMS] == sorted(full.index(i[1]) for i in getstarted.ITEMS)
    assert "(1/6)" in getstarted.text(t, True).plain and len(getstarted.text(t, True).plain.splitlines()) == 1


def _gs_app(monkeypatch, tmp_path, provider):
    _gs_profile(monkeypatch, tmp_path, tracker={"type": "memory"}, accounts={"a": {}})
    return PlApp(snapshot_provider=provider)


async def test_home_shows_get_started_for_a_profile_without_cards(monkeypatch, tmp_path):
    app = _gs_app(monkeypatch, tmp_path, lambda: _gs_data())
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        box = app.query_one("#get-started")
        assert app.active_tab == "home" and box.display
        text = str(box.render())
        assert [text.index(label) for _, label, _, _ in __import__("pl.tui.getstarted", fromlist=["x"]).ITEMS] == sorted(
            text.index(label) for _, label, _, _ in __import__("pl.tui.getstarted", fromlist=["x"]).ITEMS)
        assert "press I" in text
        await pilot.press("I")
        await pilot.pause()
        assert app.active_tab == "ideas"


async def test_get_started_ticks_on_refresh(monkeypatch, tmp_path):
    state = {"cols": []}
    app = _gs_app(monkeypatch, tmp_path, lambda: _gs_data(*state["cols"]))
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert "✓ its plan approved" not in str(app.query_one("#get-started").render())
        state["cols"] = ["Approved"]
        app.refresh_data()
        await pilot.pause()
        assert "✓ its plan approved" in str(app.query_one("#get-started").render())


async def test_get_started_collapses_and_dismisses(monkeypatch, tmp_path):
    app = _gs_app(monkeypatch, tmp_path, lambda: _gs_data())
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        assert str(app.query_one("#get-started").render()).startswith("Get started (2/6)")
        assert "\n" not in str(app.query_one("#get-started").render()).strip()
        await pilot.press("G")
        await pilot.pause()
        assert "its first PR opened" in str(app.query_one("#get-started").render())
        await pilot.press("Z")
        await pilot.pause()
        assert not app.query_one("#get-started").display
    assert "dismissed" in json.loads((tmp_path / "getstarted.json").read_text())
    again = PlApp(snapshot_provider=lambda: _gs_data())
    async with again.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert not again.query(HomeView) or not again.query_one("#get-started").display


async def test_home_unchanged_for_a_profile_with_cards(monkeypatch, tmp_path):
    app = _gs_app(monkeypatch, tmp_path, lambda: _gs_data("Inbox"))
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.pause()
        assert app.active_tab == "dashboard"
        assert not app.query(HomeView) or not app.query_one("#get-started").display
        assert not (tmp_path / "getstarted.json").exists()
