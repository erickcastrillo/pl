"""pl accounts --check-trust: read-only report of folder trust in each Claude account's .claude.json."""
import argparse
import builtins
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pl import commands, harnesses
from pl import config as C

CHECK = argparse.Namespace(reset=None, check_trust=True)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    C.HOME = tmp_path
    C.WORK_DIR = tmp_path / "code" / "pl"
    C.WORKTREES_DIR = tmp_path / "code" / "pl-pl-worktrees"
    C.WORK_DIR.mkdir(parents=True)
    C.WORKTREES_DIR.mkdir(parents=True)
    C.PROFILES = {"claude": str(tmp_path / ".claude")}
    C.ACCOUNTS = {"claude": {"config_dir": str(tmp_path / ".claude")}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def write_trust(path, *folders):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": {str(f): {"hasTrustDialogAccepted": True} for f in folders}}))


def run_check(capsys):
    try:
        commands.cmd_profiles(CHECK)
        code = 0
    except SystemExit as e:
        code = e.code
    return capsys.readouterr().out, code


def test_exact_folder_listed_is_trusted(home):
    write_trust(home / ".claude" / ".claude.json", C.WORK_DIR)
    assert harnesses.folder_trust("claude", C.WORK_DIR) == (True, None)
    assert harnesses.folder_trust("claude", C.WORKTREES_DIR) == (False, None)


def test_folder_inside_a_trusted_parent_is_trusted(home):
    write_trust(home / ".claude" / ".claude.json", home / "code")
    assert harnesses.folder_trust("claude", C.WORKTREES_DIR / "pl-pl-x") == (True, None)


def test_a_folder_reached_through_a_link_matches_either_spelling(home):
    link = home / "link"
    link.symlink_to(C.WORK_DIR)
    write_trust(home / ".claude" / ".claude.json", link)
    assert harnesses.folder_trust("claude", link)[0] is True
    write_trust(home / ".claude" / ".claude.json", C.WORK_DIR)
    assert harnesses.folder_trust("claude", link)[0] is True


def test_missing_file_is_untrusted_with_reason(home):
    assert harnesses.folder_trust("claude", C.WORK_DIR) == (False, "file missing")


@pytest.mark.parametrize("body", ["{not json", "[]", '{"projects": []}'])
def test_bad_json_is_untrusted_with_reason(home, body):
    f = home / ".claude" / ".claude.json"
    f.parent.mkdir()
    f.write_text(body)
    assert harnesses.folder_trust("claude", C.WORK_DIR) == (False, "unreadable")


def test_no_config_dir_reads_home_claude_json(home):
    C.PROFILES["solo"] = None
    assert harnesses.claude_json("solo") == home / ".claude.json"
    write_trust(home / ".claude.json", C.WORK_DIR)
    assert harnesses.folder_trust("solo", C.WORK_DIR) == (True, None)


def test_all_trusted_prints_one_line_per_folder_and_exits_0(home, capsys):
    write_trust(home / ".claude" / ".claude.json", C.WORK_DIR, C.WORKTREES_DIR)
    out, code = run_check(capsys)
    assert code == 0
    lines = out.strip().splitlines()
    assert len(lines) == 2 and all("NOT" not in ln and "trusted" in ln for ln in lines)
    assert "work" in lines[0] and "worktrees" in lines[1]


def test_untrusted_worktrees_prints_fix_command_and_exits_1(home, capsys):
    C.PROFILES["claude2"] = str(home / ".claude-2")
    write_trust(home / ".claude-2" / ".claude.json", C.WORK_DIR)
    write_trust(home / ".claude" / ".claude.json", C.WORK_DIR, C.WORKTREES_DIR)
    out, code = run_check(capsys)
    assert code == 1
    assert "cd ~/code/pl-pl-worktrees && CLAUDE_CONFIG_DIR=~/.claude-2 claude" in out
    assert '"Yes, I trust this folder"' in out
    assert out.count("NOT trusted") == 1


def test_no_config_dir_fix_has_no_env_part(home, capsys):
    C.PROFILES.clear()
    C.PROFILES["solo"] = None
    out, code = run_check(capsys)
    assert code == 1
    assert "fix: cd ~/code/pl && claude," in out
    assert "CLAUDE_CONFIG_DIR" not in out


def test_bad_file_reports_both_folders_and_checks_the_next_account(home, capsys):
    C.PROFILES["claude2"] = str(home / ".claude-2")
    (home / ".claude").mkdir()
    (home / ".claude" / ".claude.json").write_text("{nope")
    write_trust(home / ".claude-2" / ".claude.json", C.WORK_DIR, C.WORKTREES_DIR)
    out, code = run_check(capsys)
    assert code == 1
    assert out.count("(unreadable)") == 2
    assert out.count("NOT trusted") == 2
    assert "claude2" in out and "Traceback" not in out


def test_non_claude_accounts_are_skipped(home, capsys):
    C.PROFILES["cx"] = str(home / ".codex-x")
    C.ACCOUNTS["cx"] = {"harness": "codex", "config_dir": str(home / ".codex-x")}
    write_trust(home / ".claude" / ".claude.json", C.WORK_DIR, C.WORKTREES_DIR)
    out, code = run_check(capsys)
    assert code == 0 and "cx" not in out


def test_no_claude_accounts_says_so_and_exits_0(home, capsys):
    C.PROFILES.clear()
    C.ACCOUNTS.clear()
    C.PROFILES["cx"] = str(home / ".codex-x")
    C.ACCOUNTS["cx"] = {"harness": "codex", "config_dir": str(home / ".codex-x")}
    out, code = run_check(capsys)
    assert code == 0 and out.strip() == "no Claude accounts in this profile"


def test_check_leaves_config_unchanged_and_never_opens_credentials(home, capsys, monkeypatch):
    cfg = home / ".claude" / ".claude.json"
    write_trust(cfg, C.WORK_DIR)
    (home / ".claude" / ".credentials.json").write_text("secret")
    before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
    real_open, real_path_open = builtins.open, Path.open

    def guard(name):
        if harnesses.SECRET_RE.fullmatch(Path(str(name)).name):
            raise AssertionError(f"opened {name}")

    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (guard(f), real_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (guard(self), real_path_open(self, *a, **k))[1])
    run_check(capsys)
    assert (cfg.read_bytes(), cfg.stat().st_mtime_ns) == before


def test_plain_accounts_output_is_unchanged(home, capsys):
    commands.cmd_profiles(argparse.Namespace(reset=None))
    want = capsys.readouterr().out
    commands.cmd_profiles(argparse.Namespace(reset=None, check_trust=False))
    assert capsys.readouterr().out == want


def test_accounts_help_lists_check_trust(home):
    env = {"HOME": str(home), "PL_NO_UPDATE_CHECK": "1", "PATH": "/usr/bin:/bin"}
    prof = home / ".pl-t"
    prof.mkdir()
    (prof / "config.toml").write_text('[accounts.a]\nconfig_dir = "~/a"\n')
    env["PL_CONFIG_DIR"] = str(prof)
    for args in (["accounts", "--help"], ["--help"]):
        out = subprocess.run([sys.executable, "-m", "pl", *args], env=env, capture_output=True, text=True).stdout
        assert "--check-trust" in out
