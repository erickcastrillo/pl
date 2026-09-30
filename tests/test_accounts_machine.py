"""Shared account parking: an account folder parked by one profile is parked for every profile that uses it."""
import argparse
import json
from datetime import datetime

import pytest

from pl import accounts, commands
from pl import config as C

LIMIT = "You've hit your limit"


@pytest.fixture(autouse=True)
def two_profiles(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    (tmp_path / "acct-f").mkdir()
    (tmp_path / "acct-g").mkdir()
    (tmp_path / "link-f").symlink_to(tmp_path / "acct-f")   # same folder, another spelling
    for name, body in (("a", f'[accounts.main]\nconfig_dir = "{tmp_path}/acct-f"\n'),
                       ("b", f'[accounts.work]\nconfig_dir = "{tmp_path}/link-f"\n'
                             f'[accounts.solo]\nconfig_dir = "{tmp_path}/acct-g"\n')):
        (tmp_path / f".pl-{name}").mkdir()
        (tmp_path / f".pl-{name}" / "config.toml").write_text(body)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def as_profile(name):
    C.load(name)


def test_a_folder_parked_by_one_profile_is_parked_for_the_other(two_profiles):
    as_profile("a")
    accounts.mark_exhausted("main", LIMIT)
    as_profile("b")
    assert accounts.exhausted_profiles() == {"work"}                  # solo's folder is not affected
    entry = json.loads((two_profiles / "machine" / "accounts.json").read_text())
    folder = str((two_profiles / "acct-f").resolve())
    assert list(entry) == [folder] and entry[folder]["by_profile"] == "a" and entry[folder]["until"]


def test_an_expired_entry_is_ignored_and_reset_clears_it_for_both(two_profiles):
    as_profile("a")
    accounts.mark_exhausted("main", LIMIT)
    f = two_profiles / "machine" / "accounts.json"
    data = json.loads(f.read_text())
    key = next(iter(data))
    data[key]["until"] = "2000-01-01T00:00:00+00:00"
    f.write_text(json.dumps(data))
    as_profile("b")
    assert accounts.exhausted_profiles() == set()
    as_profile("a")
    accounts.mark_exhausted("main", LIMIT)
    commands.cmd_profiles(argparse.Namespace(reset="main"))
    assert accounts.exhausted_profiles() == set()
    as_profile("b")
    assert accounts.exhausted_profiles() == set()


def test_an_unreadable_machine_file_behaves_as_today(two_profiles):
    (two_profiles / "machine").mkdir()
    (two_profiles / "machine" / "accounts.json").write_text("{not json")
    as_profile("b")
    assert accounts.exhausted_profiles() == set()
    accounts.mark_exhausted("solo", LIMIT)
    assert accounts.exhausted_profiles() == {"solo"}


def test_a_reset_from_the_other_profile_clears_the_account_everywhere(two_profiles):
    as_profile("a")
    accounts.mark_exhausted("main", LIMIT)
    as_profile("b")
    commands.cmd_profiles(argparse.Namespace(reset="work"))   # same folder as a's main
    assert accounts.exhausted_profiles() == set()
    as_profile("a")
    assert accounts.exhausted_profiles() == set()


def test_pl_accounts_shows_until_and_reason_from_the_machine_entry(two_profiles, capsys):
    as_profile("a")
    until = accounts.mark_exhausted("main", LIMIT)
    as_profile("b")
    capsys.readouterr()
    commands.cmd_profiles(argparse.Namespace(reset=None))
    line = next(x for x in capsys.readouterr().out.splitlines() if x.strip().startswith("work"))
    assert f"PARKED until {until[11:16]} UTC (" in line and "(," not in line


def test_reset_rewrites_another_profiles_file_by_rename_under_its_lock(two_profiles, monkeypatch):
    import os
    as_profile("b")
    accounts.mark_exhausted("work", LIMIT)   # b's own file parks work (the folder of a's main)
    as_profile("a")
    real, replaced = os.replace, []
    monkeypatch.setattr(accounts.os, "replace", lambda s, d: replaced.append(str(d)) or real(s, d))
    commands.cmd_profiles(argparse.Namespace(reset="main"))
    f = two_profiles / ".pl-b" / "state" / "pl-profiles.json"
    assert str(f) in replaced and "work" not in json.loads(f.read_text())
    assert (f.parent / "pl-profiles.json.lock").exists()


# ---------- weekly limit: reset dates and the diagnostic copy ----------

WEEKLY = """You've hit your weekly limit, resets Oct 4 at 9pm
   ❯ 1. Stop and wait for limit to reset
     2. Wait here, then continue automatically at Oct 4 at 9pm
     3. Ask your admin for more usage
   Enter to confirm · Esc to cancel"""


@pytest.mark.parametrize("text, want", [
    (WEEKLY, datetime(2026, 10, 4, 21, 0)),
    ("resets October 4 at 21:30", datetime(2026, 10, 4, 21, 30)),
    ("resets 4 Oct, 9:15pm", datetime(2026, 10, 4, 21, 15)),
    ("resets Sep 2 at 9am", datetime(2027, 9, 2, 9, 0)),          # already past this year: next year
])
def test_a_reset_with_a_date_parks_until_that_date(text, want):
    assert accounts.reset_at(text, datetime(2026, 9, 30, 10, 0)) == want


@pytest.mark.parametrize("now, want", [(datetime(2026, 9, 30, 10, 0), datetime(2026, 9, 30, 21, 0)),
                                       (datetime(2026, 9, 30, 22, 0), datetime(2026, 10, 1, 21, 0))])
def test_a_reset_without_a_date_is_the_next_time_of_day(now, want):
    assert accounts.reset_at("You've hit your limit · resets 9pm (America/Los_Angeles)", now) == want
    assert accounts.reset_at("nothing about a reset", now) is None


def test_mark_exhausted_parks_until_the_date_on_the_screen(two_profiles):
    as_profile("a")
    until = datetime.fromisoformat(accounts.mark_exhausted("main", WEEKLY)).astimezone()
    assert (until - accounts.reset_at(WEEKLY, datetime.now()).astimezone()).total_seconds() == 60


def test_the_diagnostic_screen_copy_stays_in_the_profiles_state_folder(two_profiles):
    as_profile("a")
    accounts.mark_exhausted("main", WEEKLY)
    copies = list((two_profiles / ".pl-a" / "state" / "limits").glob("pl-limit-main-*.txt"))
    assert len(copies) == 1 and copies[0].read_text() == WEEKLY
    assert not (two_profiles / ".claude-attention").exists()
    assert not list((two_profiles / ".pl-a" / "state").glob("pl-limit-*"))   # no longer loose in state/
