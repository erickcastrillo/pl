"""[harnesses.antigravity] trust_worktrees = true: pl adds the worktree it starts an agy agent in to agy's trusted
workspaces, so the agent does not stop at "Do you trust the contents of this project?". Off by default; only folders
inside the profile's worktrees folder are ever added."""
import json

import pytest

from pl import config as C
from pl import harnesses


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"agy": tmp_path / ".gemini"}
    C.ACCOUNTS = {"agy": {"harness": "antigravity", "config_dir": str(tmp_path / ".gemini")}}
    C.WORKTREES_DIR = str(tmp_path / "repo-pl-worktrees")
    harnesses._CACHE.clear()
    yield tmp_path
    harnesses._CACHE.clear()
    for k, v in saved.items():
        setattr(C, k, v)


def _config(tmp_path, text):
    (tmp_path / ".pl-t" / "config.toml").write_text(text)
    harnesses._CACHE.clear()


def _settings(tmp_path, data):
    f = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data))
    return f


def _worktree(tmp_path, name="repo-pl-card"):
    w = tmp_path / "repo-pl-worktrees" / name
    w.mkdir(parents=True)
    return w


def test_enabled_adds_the_worktree_once_and_keeps_the_rest(tmp_path):
    _config(tmp_path, "[harnesses.antigravity]\ntrust_worktrees = true\n")
    f = _settings(tmp_path, {"model": "X", "trustedWorkspaces": ["/home/u/code"]})
    w = _worktree(tmp_path)
    h = harnesses.get("antigravity")
    assert harnesses.trust_worktree(h, "agy", w) is True
    assert harnesses.trust_worktree(h, "agy", w) is False   # already there: no second entry
    data = json.loads(f.read_text())
    assert data["trustedWorkspaces"] == ["/home/u/code", str(w.resolve())]
    assert data["model"] == "X"


def test_off_by_default(tmp_path):
    f = _settings(tmp_path, {"trustedWorkspaces": []})
    w = _worktree(tmp_path)
    assert harnesses.trust_worktree(harnesses.get("antigravity"), "agy", w) is False
    assert json.loads(f.read_text())["trustedWorkspaces"] == []


def test_never_adds_a_folder_outside_the_worktrees_folder(tmp_path):
    _config(tmp_path, "[harnesses.antigravity]\ntrust_worktrees = true\n")
    f = _settings(tmp_path, {"trustedWorkspaces": []})
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    h = harnesses.get("antigravity")
    assert harnesses.trust_worktree(h, "agy", outside) is False
    assert harnesses.trust_worktree(h, "agy", tmp_path / "repo-pl-worktrees") is False   # the folder itself either
    assert json.loads(f.read_text())["trustedWorkspaces"] == []


def test_an_unreadable_settings_file_is_left_alone(tmp_path, capsys):
    _config(tmp_path, "[harnesses.antigravity]\ntrust_worktrees = true\n")
    f = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json")
    w = _worktree(tmp_path)
    assert harnesses.trust_worktree(harnesses.get("antigravity"), "agy", w) is False
    assert f.read_text() == "{not json"
    assert "trust_worktrees" in capsys.readouterr().out


def test_other_harnesses_are_never_touched(tmp_path):
    _config(tmp_path, "[harnesses.claude]\ntrust_worktrees = true\n")
    with pytest.raises(SystemExit, match="trust_worktrees"):
        harnesses.get("claude")


def test_trust_worktrees_must_be_true_or_false(tmp_path):
    _config(tmp_path, '[harnesses.antigravity]\ntrust_worktrees = "yes"\n')
    with pytest.raises(SystemExit, match="trust_worktrees must be true or false"):
        harnesses.get("antigravity")
