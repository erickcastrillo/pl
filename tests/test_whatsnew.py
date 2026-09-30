"""What's new on upgrade, and the empty states that point to a feature on a quiet board."""
import json
import sys
from datetime import datetime, timezone

import pytest

from pl import cli, whatsnew
from pl import config as C
from pl.tui import dashboard, loops
from pl.tui.app import PlApp
from test_tui_views import Provider, fake_data, screen_text, settle


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-work"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load(config_dir=str(d))
    yield d
    for k, v in saved.items():
        setattr(C, k, v)


SHIPPED = ("standup", "token spend", "context", "alerts", "doing now", "move", "manager", "weekly", "memory",
           "one dispatcher per profile", "pl retry")


def test_every_feature_shipped_this_week_has_an_entry():
    text = whatsnew.text(whatsnew.ENTRIES).lower()
    for s in SHIPPED:
        assert s in text, s
    keys = [e["key"] for e in whatsnew.ENTRIES]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    for e in whatsnew.ENTRIES:
        assert e["title"] and e["where"] and e["try"] and "\n" not in "".join(e.values())


def test_unseen_is_everything_first_then_only_newer_entries(profile):
    assert whatsnew.unseen() == whatsnew.ENTRIES
    whatsnew.mark_seen(whatsnew.ENTRIES[-3]["key"])
    assert json.loads((profile / "state" / "whatsnew.json").read_text()) == {"last": whatsnew.ENTRIES[-3]["key"]}
    assert whatsnew.unseen() == whatsnew.ENTRIES[-2:]
    whatsnew.mark_seen()
    assert whatsnew.unseen() == []


def test_a_bad_whatsnew_file_counts_as_never_shown(profile):
    (profile / "state" / "whatsnew.json").write_text("[1, 2")
    assert whatsnew.unseen() == whatsnew.ENTRIES


async def test_the_popup_shows_once_per_upgrade_and_only_the_new_entries(profile, monkeypatch):
    whatsnew.mark_seen(whatsnew.ENTRIES[-2]["key"])
    app = PlApp(snapshot_provider=Provider(), whatsnew=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert isinstance(app.screen, whatsnew_screen())
        text = screen_text(app)
        assert whatsnew.ENTRIES[-1]["title"] in text and whatsnew.ENTRIES[-2]["title"] not in text
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, whatsnew_screen())
    app = PlApp(snapshot_provider=Provider(), whatsnew=True)       # the next start: nothing new, no popup
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert not isinstance(app.screen, whatsnew_screen())
    new = {"key": "2099-01-01.01", "title": "A shiny thing", "where": "Dashboard", "try": "pl shiny"}
    monkeypatch.setattr(whatsnew, "ENTRIES", [*whatsnew.ENTRIES, new])   # an upgrade adds one entry
    app = PlApp(snapshot_provider=Provider(), whatsnew=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        text = screen_text(app)
        assert "A shiny thing" in text and whatsnew.ENTRIES[-2]["title"] not in text


async def test_question_mark_and_the_palette_reopen_it(profile):
    whatsnew.mark_seen()
    app = PlApp(snapshot_provider=Provider(), whatsnew=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert not isinstance(app.screen, whatsnew_screen())
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, whatsnew_screen()) and whatsnew.ENTRIES[0]["title"] in screen_text(app)
        await pilot.press("escape")
        await settle(pilot)
        assert "What's new" in [c.title for c in app.get_system_commands(app.screen)]


async def test_test_consoles_never_pop_it_up(profile):
    async with PlApp(snapshot_provider=Provider()).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert not isinstance(app_screen := pilot.app.screen, whatsnew_screen()), app_screen


def test_pl_whatsnew_prints_every_entry_without_a_profile(monkeypatch, capsys):
    C.CONFIG_DIR = None
    monkeypatch.setattr(sys, "argv", ["pl", "whatsnew"])
    cli.main()
    out = capsys.readouterr().out
    for e in whatsnew.ENTRIES:
        assert e["title"] in out and e["try"] in out


def whatsnew_screen():
    from pl.tui.app import WhatsNewScreen
    return WhatsNewScreen


# ---- empty states ----

async def test_needs_you_with_no_alerts_says_so():
    app = PlApp(snapshot_provider=Provider({**fake_data(), "alerts": []}))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await pilot.pause()
        assert "Alerts: none open (pl alerts --all for history)" in screen_text(app)


def test_health_says_counting_until_the_first_scan():
    assert dashboard.health_tokens({"usage": None}) == ("tokens", "counting…")


async def test_the_loops_table_explains_n_a(profile):
    C.SERVICES = {"triage": {"prompt": "/loop 30m /triage", "profile": None}}
    rows = loops.loop_rows({"snapshot": {"rows": [{"loop": "triage", "kind": "loop_idle", "worker": {}}]}})
    assert (rows[0]["context"], rows[0]["tokens"]) == ("n/a", "n/a")
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("6")
        await pilot.pause()
        assert loops.HINT in screen_text(app) and "max_context" in loops.HINT and "80" in loops.HINT


# ---- fix round 1 ----

def test_the_doing_now_entry_tries_the_console_and_the_weekly_limit_points_at_the_header():
    by = {e["title"]: e for e in whatsnew.ENTRIES}
    assert next(e for t, e in by.items() if "doing now" in t)["try"] == "pl"
    assert next(e for t, e in by.items() if "weekly limit" in t)["where"] == "header: PARKED until <date>"


def test_a_park_until_another_day_shows_the_date():
    from pl import watch
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert watch.parked_until("2026-09-30T21:01:00+00:00", now) == "21:01 UTC"
    assert watch.parked_until("2026-10-04T21:01:00+00:00", now) == "Oct 4 21:01 UTC"
    assert watch.parked_until("", now) == "?"


async def test_the_popup_fits_a_narrow_terminal(profile):
    from textual.containers import Vertical
    app = PlApp(snapshot_provider=Provider(), autostart=False, whatsnew=False)
    async with app.run_test(size=(70, 30)) as pilot:
        await settle(pilot)
        await pilot.press("question_mark")
        await settle(pilot)
        assert app.screen.query_one(Vertical).outer_size.width <= 70
    app = PlApp(snapshot_provider=Provider(), autostart=False, whatsnew=False)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("question_mark")
        await settle(pilot)
        assert app.screen.query_one(Vertical).outer_size.width == 96
