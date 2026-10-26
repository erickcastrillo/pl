"""Tests for the Home tab and single-profile rule (WP2.1)."""
import json
import time
from pathlib import Path

from pl import config as C
from pl.tui.app import PlApp
from pl.tui.home import home_rows, HomeView


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


async def test_home_shows_one_row_per_pipeline_with_bold_needs_you():
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
        assert len(table.ordered_rows) == 3
        assert app.active_tab == "dashboard"
        assert not any("Home" in str(t.label) for t in app.query_one("#tabs").query_one("Tabs").query("Tab") if t.display)
        await pilot.press("H")
        await pilot.pause()
        assert app.active_tab == "home"
        cell = table.get_cell_at((0, 3))
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


async def test_one_profile_skips_home_and_opens_as_today():
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
