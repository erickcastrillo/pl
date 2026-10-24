"""Automatic worktree cleanup on dispatcher passes and [worktrees] cleanup setting."""
import pytest
import tomlkit

from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    yield home
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, text=""):
    d = home / ".pl-test"
    d.mkdir(parents=True, exist_ok=True)
    w = home / "repo"
    w.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(f'[paths]\nwork_dir = "{w}"\n{text}')
    C.load(config_dir=str(d))


def test_config_worktrees_cleanup_default(fake_home):
    _profile(fake_home)
    assert C.WORKTREES["cleanup"] is True


def test_config_worktrees_cleanup_override(fake_home):
    _profile(fake_home, "[worktrees]\ncleanup = false\n")
    assert C.WORKTREES["cleanup"] is False


def test_config_worktrees_cleanup_validation_error():
    assert "worktrees.cleanup must be true or false" in C.validate(
        tomlkit.parse('[worktrees]\ncleanup = "no"\n')
    )
    assert "worktrees.cleanup must be true or false" in C.validate(
        tomlkit.parse("[worktrees]\ncleanup = 1\n")
    )
    assert C.validate(tomlkit.parse("[worktrees]\ncleanup = true\n")) == []
    assert C.validate(tomlkit.parse("[worktrees]\ncleanup = false\n")) == []
