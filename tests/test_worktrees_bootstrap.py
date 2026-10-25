import argparse
import os
import subprocess

import pytest

from pl import commands, worktrees


def _manifest(repo, text):
    (repo / ".pl").mkdir(exist_ok=True)
    (repo / ".pl" / "worktree.yaml").write_text(text)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "main"
    r.mkdir()
    return r


def test_load_manifest_parses_yaml_copy_and_link(repo):
    _manifest(repo, 'copy: [".env", "certs/dev.pem"]\nlink:\n  - node_modules\nsetup: "npm ci"\ntimeout: 600\n')
    m = worktrees.load_manifest(repo)
    assert m["copy"] == [".env", "certs/dev.pem"]
    assert m["link"] == ["node_modules"]
    assert m["setup"] == "npm ci" and m["timeout"] == 600


def test_load_manifest_falls_back_to_worktreeinclude(repo):
    (repo / ".worktreeinclude").write_text("# local files\n.env\n\ncerts/dev.pem\n")
    assert worktrees.load_manifest(repo)["copy"] == [".env", "certs/dev.pem"]


def test_manifest_rejects_absolute_paths(repo):
    _manifest(repo, "copy: [/etc/passwd]\n")
    with pytest.raises(ValueError, match="/etc/passwd"):
        worktrees.load_manifest(repo)


def test_manifest_rejects_parent_traversal(repo):
    _manifest(repo, "copy: [.env, ../secret]\n")
    with pytest.raises(ValueError, match=r"\.\./secret"):
        worktrees.load_manifest(repo)


def test_manifest_rejects_symlink_escaping_repo(repo, tmp_path):
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", repo / "escape")
    _manifest(repo, "copy: [escape/file]\n")
    with pytest.raises(ValueError, match="escape/file"):
        worktrees.load_manifest(repo)


def test_copy_preserves_content_and_file_mode(repo, tmp_path):
    (repo / "certs").mkdir()
    (repo / "certs" / "dev.pem").write_text("KEY")
    (repo / "certs" / "dev.pem").chmod(0o600)
    _manifest(repo, "copy: [certs/dev.pem]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    worktrees.bootstrap_worktree(repo, wt)
    assert (wt / "certs" / "dev.pem").read_text() == "KEY"
    assert (wt / "certs" / "dev.pem").stat().st_mode & 0o777 == 0o600


def test_link_creates_symlink_to_main_checkout(repo, tmp_path):
    (repo / "node_modules").mkdir()
    _manifest(repo, "link: [node_modules]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    worktrees.bootstrap_worktree(repo, wt)
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules").resolve() == (repo / "node_modules").resolve()


def test_missing_source_files_skipped_with_note(repo, tmp_path):
    (repo / "b").write_text("x")
    _manifest(repo, "copy: [a, b]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    notes = worktrees.bootstrap_worktree(repo, wt)
    assert "skip a: not found in main checkout" in notes
    assert (wt / "b").read_text() == "x" and not (wt / "a").exists()


def test_copied_files_remain_untracked_in_git_worktree(repo, tmp_path):
    def git(*a, cwd=repo):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    (repo / ".env").write_text("SECRET")
    _manifest(repo, "copy: [.env]\n")
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt))
    worktrees.bootstrap_worktree(repo, wt)
    assert git("diff", "--cached", "--name-only", cwd=wt) == ""
    assert ".env" in git("status", "--porcelain", cwd=wt)


def test_dry_run_reports_actions_without_touching_disk(repo, tmp_path):
    (repo / ".env").write_text("x")
    (repo / "node_modules").mkdir()
    _manifest(repo, "copy: [.env]\nlink: [node_modules]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    notes = worktrees.bootstrap_worktree(repo, wt, dry_run=True)
    assert "copy .env" in notes and any(n.startswith("link node_modules -> ") for n in notes)
    assert list(wt.iterdir()) == []


def test_logging_contains_file_names_only_not_contents(repo, tmp_path):
    (repo / ".env").write_text("TOPSECRET")
    _manifest(repo, "copy: [.env]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    notes = worktrees.bootstrap_worktree(repo, wt)
    assert notes == ["copy .env"] and "TOPSECRET" not in "".join(notes)


def _cli(monkeypatch, repo, wt, dry_run=False):
    monkeypatch.chdir(repo)
    return commands.cmd_worktrees_bootstrap(argparse.Namespace(path=str(wt), dry_run=dry_run))


def test_cli_bootstrap_applies_manifest(repo, tmp_path, monkeypatch, capsys):
    (repo / ".env").write_text("x")
    _manifest(repo, "copy: [.env]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    _cli(monkeypatch, repo, wt)
    assert (wt / ".env").exists() and "copy .env" in capsys.readouterr().out


def test_cli_bootstrap_dry_run_prints_actions(repo, tmp_path, monkeypatch, capsys):
    (repo / ".env").write_text("x")
    _manifest(repo, "copy: [.env]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    _cli(monkeypatch, repo, wt, dry_run=True)
    assert not (wt / ".env").exists() and "copy .env" in capsys.readouterr().out


def test_cli_bootstrap_fails_on_invalid_manifest(repo, tmp_path, monkeypatch):
    _manifest(repo, "copy: [../x]\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(SystemExit) as e:
        _cli(monkeypatch, repo, wt)
    assert e.value.code not in (0, None)
