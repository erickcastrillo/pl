"""What's new on upgrade, and the empty states that point to a feature on a quiet board."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

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


def test_whatsnew_names_the_marketing_template():
    assert len([e for e in whatsnew.ENTRIES if "marketing template" in e["title"]]) == 1


def test_pulling_github_edits_has_an_entry():
    # not "the newest": entries from later merges land after it
    entry = next(e for e in whatsnew.ENTRIES if "GitHub edits" in e["title"] and "conflicts" in e["title"])
    assert "pl alerts" in entry["where"] and entry["try"] == "pl sync now"


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


async def test_the_palette_reopens_it(profile):
    whatsnew.mark_seen()
    app = PlApp(snapshot_provider=Provider(), whatsnew=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert not isinstance(app.screen, whatsnew_screen())
        app.action_whatsnew()
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

async def test_alerts_tab_with_no_alerts_says_so():
    app = PlApp(snapshot_provider=Provider({**fake_data(), "alerts": []}))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("alerts")
        await settle(pilot)
        assert "No open alerts. pl watches for:" in screen_text(app)


def test_health_says_counting_until_the_first_scan():
    assert dashboard.health_tokens({"usage": None}) == ("tokens", "counting…")


async def test_the_loops_table_explains_n_a(profile):
    C.SERVICES = {"triage": {"prompt": "/loop 30m /triage", "profile": None}}
    rows = loops.loop_rows({"snapshot": {"rows": [{"loop": "triage", "kind": "loop_idle", "worker": {}}]}})
    assert (rows[0]["context"], rows[0]["tokens"]) == ("n/a", "n/a")
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
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
        app.action_whatsnew()
        await settle(pilot)
        assert app.screen.query_one(Vertical).outer_size.width <= 70
    app = PlApp(snapshot_provider=Provider(), autostart=False, whatsnew=False)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_whatsnew()
        await settle(pilot)
        assert app.screen.query_one(Vertical).outer_size.width == 96


def test_brain_tab_removal_has_an_entry():
    entry = next(e for e in whatsnew.ENTRIES if "Brain tab" in e["title"])
    assert "Assistant" in entry["title"] and "b " in entry["where"] + " " and "press b" in entry["try"]


def test_whatsnew_lists_empty_states():
    entry = next(e for e in whatsnew.ENTRIES if "empty" in e["title"])
    assert entry["key"] in {e["key"] for e in whatsnew.ENTRIES}   # later entries may follow it


def test_whatsnew_names_the_marketing_pod():
    assert sum("marketing pod" in e["title"] for e in whatsnew.ENTRIES) == 1


MIGRATED = {
    "key": "2026-10-26.80",
    "title": "Customer-support pipeline template: triage and owner-approved replies",
    "where": "pl pipelines list --templates",
    "try": "pl pipelines add support --template customer-support --board local --yes",
}


def _write(d, name, **fields):
    lines = [f"{k} = {json.dumps(v)}" for k, v in fields.items()]
    (d / name).write_text("\n".join(lines) + "\n")


def _entry_file(d, name, **extra):
    _write(d, name, title="T", where="W", **{"try": "pl x"}, **extra)


def test_the_shipped_entry_files_have_no_problems():
    assert whatsnew.problems() == []


def test_entry_files_load_in_file_name_order(tmp_path):
    for name in ("2026-10-28-0900-b.toml", "2026-10-27-2310-a.toml", "2026-10-28-0130-c.toml"):
        _entry_file(tmp_path, name)
    got = whatsnew.load(tmp_path)
    assert [e["key"] for e in got] == ["2026-10-27-2310-a", "2026-10-28-0130-c", "2026-10-28-0900-b"]
    assert all(set(e) == {"key", "title", "where", "try"} for e in got)


def test_a_missing_entry_folder_loads_nothing(tmp_path):
    assert whatsnew.load(tmp_path / "nope") == []


def _bad_missing_try(d):
    _write(d, "2026-10-27-0100-x.toml", title="T", where="W")
    return "2026-10-27-0100-x.toml", "try"


def _bad_name(d):
    _entry_file(d, "foo.toml")
    return "foo.toml", "name"


def _bad_duplicate_files(d):
    _entry_file(d, "2026-10-26-0001-a.toml", id="2026-10-26.01")
    _entry_file(d, "2026-10-26-0001-b.toml", id="2026-10-26.01")
    return "2026-10-26-0001-b.toml", "duplicate"


def _bad_duplicate_legacy(d):
    _entry_file(d, "2026-10-26-0079-a.toml", id="2026-10-26.79")
    return "2026-10-26-0079-a.toml", "duplicate"


def _bad_id_on_new_file(d):
    _entry_file(d, "2026-10-28-0100-x.toml", id="2026-10-28-0100-x")
    return "2026-10-28-0100-x.toml", "id"


def _bad_old_date_without_id(d):
    _entry_file(d, "2026-10-25-1200-x.toml")
    return "2026-10-25-1200-x.toml", "id"


def _bad_time(d):
    _entry_file(d, "2026-10-28-2575-x.toml")
    return "2026-10-28-2575-x.toml", "time"


def _bad_unknown_key(d):
    _entry_file(d, "2026-10-28-0100-x.toml", after="2026-11-01")
    return "2026-10-28-0100-x.toml", "after"


def _bad_syntax(d):
    (d / "2026-10-28-0100-x.toml").write_text("title = \n")
    return "2026-10-28-0100-x.toml", "parse"


@pytest.mark.parametrize("make", [_bad_missing_try, _bad_name, _bad_duplicate_files, _bad_duplicate_legacy,
                                  _bad_id_on_new_file, _bad_old_date_without_id, _bad_time, _bad_unknown_key,
                                  _bad_syntax])
def test_a_bad_entry_file_is_named_with_its_problem(tmp_path, make):
    name, word = make(tmp_path)
    msgs = whatsnew.problems(tmp_path)
    assert any(m.startswith(name + ":") and word in m for m in msgs), msgs
    assert name.removesuffix(".toml") not in [e["key"] for e in whatsnew.load(tmp_path)]


def test_a_file_that_sorts_before_the_legacy_entries_is_named(tmp_path):
    _entry_file(tmp_path, "2026-10-26-0001-a.toml", id="2026-10-26.01")
    assert any("sorts before" in m for m in whatsnew.problems(tmp_path, legacy=[{"key": "2026-10-26.79"}]))


def test_the_migrated_entry_keeps_its_key_and_text():
    assert MIGRATED in whatsnew.ENTRIES   # later migrated entries (0081-0086, merged from main) may follow it
    assert MIGRATED not in whatsnew._LEGACY
    keys = [e["key"] for e in whatsnew.ENTRIES]
    assert keys == sorted(keys) and len(keys) == len(set(keys))


def test_seen_state_from_before_the_move_hides_old_entries_and_shows_new_files(tmp_path, monkeypatch):
    real = whatsnew.DIR / "2026-10-26-0080-customer-support-template.toml"
    (tmp_path / real.name).write_text(real.read_text())
    _entry_file(tmp_path, "2026-10-27-0310-foo.toml")
    monkeypatch.setattr(whatsnew, "ENTRIES", whatsnew._LEGACY + whatsnew.load(tmp_path))
    whatsnew.mark_seen("2026-10-26.80")
    assert [e["key"] for e in whatsnew.unseen()] == ["2026-10-27-0310-foo"]


def _git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, cwd=cwd,
                          env=env, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_two_branches_adding_entry_files_merge_cleanly_in_either_order(tmp_path, order):
    repo = tmp_path / "repo"
    (repo / "whatsnew.d").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "whatsnew.d" / ".keep").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    files = {"a": "2026-10-27-0310-a.toml", "b": "2026-10-27-0305-b.toml"}
    for branch, name in files.items():
        _git(repo, "checkout", "-q", "-b", branch, "main")
        _entry_file(repo / "whatsnew.d", name)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", branch)
    _git(repo, "checkout", "-q", "main")
    for branch in order:
        _git(repo, "merge", "-q", "--no-edit", branch)
    assert [e["key"] for e in whatsnew.load(repo / "whatsnew.d")] == ["2026-10-27-0305-b", "2026-10-27-0310-a"]


def test_entry_files_ship_in_the_wheel():
    root = Path(__file__).parents[1]
    wheel = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/pl"]
    assert not {"include", "exclude", "only-include"} & set(wheel)
    ignored = (root / ".gitignore").read_text().splitlines()
    assert not [line for line in ignored if "toml" in line]


def test_a_file_named_after_the_migrated_one_needs_no_id_even_on_the_same_day(tmp_path, profile, monkeypatch):
    _entry_file(tmp_path, "2026-10-26-1530-new-thing.toml")
    assert whatsnew.problems(tmp_path, legacy=whatsnew._LEGACY + [MIGRATED]) == []
    assert [e["key"] for e in whatsnew.load(tmp_path)] == ["2026-10-26-1530-new-thing"]
    whatsnew.mark_seen("2026-10-26.80")
    monkeypatch.setattr(whatsnew, "ENTRIES", whatsnew._LEGACY + [MIGRATED] + whatsnew.load(tmp_path))
    assert [e["key"] for e in whatsnew.unseen()] == ["2026-10-26-1530-new-thing"]
