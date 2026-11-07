import json
import runpy
import subprocess
import sys

import pytest

from pl import cli
from pl import config as C


@pytest.fixture
def profile(tmp_path):
    prof = tmp_path / ".pl-main-module"
    prof.mkdir()
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')
    return prof


def run_pl(tmp_path, profile, *args):
    return subprocess.run(
        [sys.executable, "-m", "pl", *args], capture_output=True, text=True, timeout=30, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PL_CONFIG_DIR": str(profile), "PL_NO_UPDATE_CHECK": "1"})


def test_tests_run_with_no_command_exits_2(tmp_path, profile):
    r = run_pl(tmp_path, profile, "tests", "run")
    assert r.returncode == 2 and "give the command after --" in r.stderr


def test_tests_run_passes_on_the_command_exit_code(tmp_path, profile):
    assert run_pl(tmp_path, profile, "tests", "run", "--", "sh", "-c", "exit 7").returncode == 7


def test_tests_run_true_exits_0(tmp_path, profile):
    assert run_pl(tmp_path, profile, "tests", "run", "--", "true").returncode == 0


def test_tests_nonsense_still_exits_2(tmp_path, profile):
    r = run_pl(tmp_path, profile, "tests", "nonsense")
    assert r.returncode == 2 and "invalid choice" in r.stderr


def test_missing_section_still_exits_1(tmp_path, profile):
    assert run_pl(tmp_path, profile, "board", "init").returncode == 0
    assert run_pl(tmp_path, profile, "idea", "x").returncode == 0
    listed = run_pl(tmp_path, profile, "list", "--json")
    card_id = json.loads(listed.stdout)["pipeline"][0]["id"]
    r = run_pl(tmp_path, profile, "section", card_id, "NOPE")
    assert r.returncode == 1 and "NOPE" in r.stderr


def test_dispatch_return_value_is_not_an_exit_code(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["pl", "dispatch", "--once"])
    monkeypatch.setattr(cli.config, "load", lambda *a, **k: None)
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path, raising=False)
    monkeypatch.setattr(cli, "cmd_dispatch", lambda *a, **k: calls.append(a) or 5)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("pl", run_name="__main__")
    assert exc.value.code is None and calls
