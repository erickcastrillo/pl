"""Shared account parking: an account folder parked by one profile is parked for every profile that uses it."""
import argparse
import json

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
