"""Automatic worktree cleanup on dispatcher passes and [worktrees] cleanup setting."""
import json
import subprocess
import types
from pathlib import Path
import pytest
import tomlkit

from pl import config as C
from pl import alerts


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


# --- WP2: Automatic worktree sweep in dispatcher passes ---


class FakeGitRunner:
    def __init__(self, wt_dir, repo_dir):
        self.wt_dir = wt_dir
        self.repo_dir = repo_dir
        self.commands = []
        self.worktrees = []  # list of dict(path=Path, branch=str)
        self.pr_views = {}   # url -> dict
        self.dirty_paths = set()
        self.unpushed_branches = set()
        self.fail_removals = False

    def __call__(self, argv, capture_output=True, text=True, cwd=None, check=False, **kw):
        self.commands.append((list(argv), cwd))
        cmd = argv[0]

        if cmd == "git":
            sub = argv[1]
            if sub == "worktree":
                action = argv[2]
                if action == "list":
                    out = []
                    for wt in self.worktrees:
                        out.append(f"worktree {wt['path']}")
                        out.append("HEAD 1111111111111111111111111111111111111111")
                        if wt.get("branch"):
                            out.append(f"branch refs/heads/{wt['branch']}")
                        out.append("")
                    return types.SimpleNamespace(returncode=0, stdout="\n".join(out), stderr="")
                elif action == "remove":
                    if self.fail_removals:
                        if check:
                            raise subprocess.CalledProcessError(1, argv, "fatal: locked")
                        return types.SimpleNamespace(returncode=1, stdout="", stderr="fatal: locked")
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
                elif action == "prune":
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            elif sub == "status":
                p = Path(cwd) if cwd else None
                if p and p in self.dirty_paths:
                    return types.SimpleNamespace(returncode=0, stdout=" M dirty.txt\n", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            elif sub == "rev-parse":
                return types.SimpleNamespace(returncode=0, stdout="hash\n", stderr="")

            elif sub == "rev-list":
                spec = argv[-1]
                # spec e.g. origin/pl/branch..HEAD
                branch = spec.split("..")[0].removeprefix("origin/")
                if branch in self.unpushed_branches:
                    return types.SimpleNamespace(returncode=0, stdout="2\n", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="0\n", stderr="")

        elif cmd == "gh":
            if argv[1] == "pr" and argv[2] == "view":
                url = argv[3]
                data = self.pr_views.get(url, {"state": "OPEN", "headRefName": "some-branch"})
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        elif cmd == "tmux":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def wt_env(tmp_path, monkeypatch):
    _profile(tmp_path)
    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    C.WORKTREES_DIR = wt_dir
    C.WORK_DIR = repo_dir
    C.WORKTREES = {"cleanup": True}

    runner = FakeGitRunner(wt_dir, repo_dir)
    from pl import worktrees
    monkeypatch.setattr(worktrees, "_run", runner)
    monkeypatch.setattr(worktrees, "col_name", lambda lid: lid)
    return runner


def test_sweep_removes_clean_merged_done_card(wt_env):
    from pl import worktrees

    wt_path = C.WORKTREES_DIR / "repo-pl-c1"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/c1"}]
    pr_url = "https://github.com/org/repo/pull/1"
    wt_env.pr_views[pr_url] = {"state": "MERGED", "mergedAt": "2026-10-23T00:00:00Z", "headRefName": "pl/c1"}

    card = {
        "id": "c1",
        "title": "Clean slice 1",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "pr_urls": [pr_url]},
    }

    worktrees.sweep([card])

    remove_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]
    prune_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "prune"]]
    branch_del_cmds = [cmd for cmd, cwd in wt_env.commands if "branch" in cmd and ("-d" in cmd or "-D" in cmd)]

    assert len(remove_cmds) == 1
    assert str(wt_path) in remove_cmds[0]
    assert len(prune_cmds) == 1
    assert len(branch_del_cmds) == 0  # branch preserved


def test_sweep_keeps_dirty_or_unpushed_dropped_card(wt_env):
    from pl import worktrees

    dirty_path = C.WORKTREES_DIR / "repo-pl-dirty"
    dirty_path.mkdir()
    unpushed_path = C.WORKTREES_DIR / "repo-pl-unpushed"
    unpushed_path.mkdir()

    wt_env.worktrees = [
        {"path": dirty_path, "branch": "pl/dirty"},
        {"path": unpushed_path, "branch": "pl/unpushed"},
    ]
    wt_env.dirty_paths.add(dirty_path)
    wt_env.unpushed_branches.add("pl/unpushed")

    card_dirty = {
        "id": "c_dirty",
        "title": "Dirty dropped",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }
    card_unpushed = {
        "id": "c_unpushed",
        "title": "Unpushed dropped",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }

    worktrees.sweep([card_dirty, card_unpushed])

    remove_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]
    assert len(remove_cmds) == 0


def test_sweep_removes_clean_dropped_card(wt_env):
    from pl import worktrees

    clean_path = C.WORKTREES_DIR / "repo-pl-clean-drop"
    clean_path.mkdir()
    wt_env.worktrees = [{"path": clean_path, "branch": "pl/clean-drop"}]

    card = {
        "id": "c_drop",
        "title": "Clean drop",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }

    worktrees.sweep([card])

    remove_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]
    assert len(remove_cmds) == 1
    assert str(clean_path) in remove_cmds[0]


def test_sweep_never_removes_active_card_or_external_worktree(wt_env):
    from pl import worktrees

    active_path = C.WORKTREES_DIR / "repo-pl-active"
    active_path.mkdir()
    external_path = C.WORK_DIR.parent / "external-wt"
    external_path.mkdir()

    wt_env.worktrees = [
        {"path": active_path, "branch": "pl/active"},
        {"path": external_path, "branch": "pl/ext"},
    ]

    card_active = {
        "id": "c_active",
        "title": "In progress slice",
        "list_id": "In progress",
        "metadata": {"pipeline_mode": "auto"},
    }
    card_ext = {
        "id": "c_ext",
        "title": "External worktree card",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }

    worktrees.sweep([card_active, card_ext])

    remove_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]
    assert len(remove_cmds) == 0


def test_sweep_skipped_when_cleanup_disabled(wt_env):
    from pl import worktrees

    C.WORKTREES["cleanup"] = False

    wt_path = C.WORKTREES_DIR / "repo-pl-c1"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/c1"}]
    card = {
        "id": "c1",
        "title": "Clean slice 1",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }

    worktrees.sweep([card])

    remove_cmds = [cmd for cmd, cwd in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]
    assert len(remove_cmds) == 0


def test_sweep_handles_removal_failure_with_warning(wt_env, capsys, monkeypatch):
    from pl import worktrees

    wt_env.fail_removals = True
    alert_calls = []
    monkeypatch.setattr(alerts, "open", lambda *a, **kw: alert_calls.append((a, kw)))

    wt_path = C.WORKTREES_DIR / "repo-pl-c1"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/c1"}]
    card = {
        "id": "c1",
        "title": "Clean slice 1",
        "list_id": "Done",
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z"},
    }

    # Should not raise exception
    worktrees.sweep([card])

    err = capsys.readouterr().err
    assert "cleanup failed" in err
    assert "repo-pl-c1" in err
    assert len(alert_calls) == 0  # no alerts opened


def test_dispatch_once_calls_worktrees_sweep(monkeypatch):
    from pl import dispatch, worktrees
    swept = []
    monkeypatch.setattr(worktrees, "sweep", lambda cards, dry: swept.append((cards, dry)))
    monkeypatch.setattr(dispatch, "limited", lambda: None)
    monkeypatch.setattr(dispatch, "github_board", lambda: False)
    monkeypatch.setattr(dispatch, "load_state", lambda: {})
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    test_cards = [{"id": "1", "list_id": "Done", "metadata": {"pipeline_mode": "auto"}}]
    monkeypatch.setattr(dispatch, "cards", lambda: test_cards)
    monkeypatch.setattr(dispatch, "paused", lambda: False)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "mirror_to_product", lambda *a, **k: None)

    dispatch.dispatch_once(max_runs=1, dry=False, max_prep=1, pull=False)

    assert len(swept) == 1
    assert swept[0] == (test_cards, False)


def _gh_views(runner):
    return [cmd for cmd, _ in runner.commands if cmd[:3] == ["gh", "pr", "view"]]


def test_sweep_matches_by_name_before_asking_github_about_other_cards(wt_env):
    # 2026-10-26: one sweep asked GitHub about every Done card's PR to find each worktree's card (hundreds of calls a pass)
    from pl import worktrees
    wt_path = C.WORKTREES_DIR / "repo-pl-c1"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/c1"}]
    others = [{"id": f"o{i}", "title": f"Other {i}", "list_id": "Done",
               "metadata": {"pr_urls": [f"https://github.com/org/repo/pull/{100 + i}"]}} for i in range(50)]
    mine = {"id": "c1", "title": "C1", "list_id": "In progress", "metadata": {"branch": "pl/c1"}}
    worktrees.sweep(others + [mine])
    assert _gh_views(wt_env) == []


def test_a_merged_pr_is_asked_about_once_across_passes(wt_env, monkeypatch):
    from pl import worktrees
    wt_path = C.WORKTREES_DIR / "repo-pl-c1"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/c1"}]
    url = "https://github.com/org/repo/pull/1"
    wt_env.pr_views[url] = {"state": "MERGED", "mergedAt": "2026-10-23T00:00:00Z", "headRefName": "pl/c1"}
    monkeypatch.setattr(worktrees, "_is_dirty", lambda p: True)   # kept, so the next pass looks at it again
    card = {"id": "c1", "title": "C1", "list_id": "Done", "metadata": {"branch": "pl/c1", "pr_urls": [url]}}
    worktrees.sweep([card])
    worktrees.sweep([card])
    assert len(_gh_views(wt_env)) == 1
