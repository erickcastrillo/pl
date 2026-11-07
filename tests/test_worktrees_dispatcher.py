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
        self.heads = {}       # path -> HEAD sha
        self.pr_lists = {}    # branch -> merged PR dict found by `gh pr list --head`
        self.gh_fails = False
        self.status_fails = set()

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
                if p in self.status_fails:
                    return types.SimpleNamespace(returncode=128, stdout="", stderr="fatal")
                if p and p in self.dirty_paths:
                    return types.SimpleNamespace(returncode=0, stdout=" M dirty.txt\n", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            elif sub == "rev-parse":
                return types.SimpleNamespace(returncode=0, stdout=self.heads.get(Path(cwd), "hash") + "\n", stderr="")

            elif sub == "rev-list" and argv[-1] == "--remotes":
                branch = next((w.get("branch") for w in self.worktrees if w["path"] == Path(cwd)), None)
                n = 2 if branch in self.unpushed_branches else 0
                return types.SimpleNamespace(returncode=0, stdout=f"{n}\n", stderr="")

            elif sub == "rev-list":
                spec = argv[-1]
                # spec e.g. origin/pl/branch..HEAD
                branch = spec.split("..")[0].removeprefix("origin/")
                if branch in self.unpushed_branches:
                    return types.SimpleNamespace(returncode=0, stdout="2\n", stderr="")
                return types.SimpleNamespace(returncode=0, stdout="0\n", stderr="")

        elif cmd == "gh":
            if self.gh_fails:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            if argv[1] == "pr" and argv[2] == "list":
                hit = self.pr_lists.get(argv[argv.index("--head") + 1])
                return types.SimpleNamespace(returncode=0, stdout=json.dumps([hit] if hit else []), stderr="")
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
        "metadata": {"pipeline_mode": "auto", "pr_urls": [pr_url], "branch": "pl/c1"},
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
        "metadata": {"pipeline_mode": "auto", "dropped_at": "2026-10-23T00:00:00Z", "branch": "pl/c1"},
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
    wt_env.unpushed_branches.add("pl/c1")   # kept, so the next pass looks at it again
    card = {"id": "c1", "title": "C1", "list_id": "Done", "metadata": {"branch": "pl/c1", "pr_urls": [url]}}
    worktrees.sweep([card])
    worktrees.sweep([card])
    assert len(_gh_views(wt_env)) == 1


# --- Plan 54 slice 5: worktrees are never removed under a live card ---


def _merged_done_card(wt_env, name="c1", metadata=None):
    wt_path = C.WORKTREES_DIR / f"repo-pl-{name}"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": f"pl/{name}"}]
    pr_url = f"https://github.com/org/repo/pull/{name}"
    wt_env.pr_views[pr_url] = {"state": "MERGED", "mergedAt": "2026-10-23T00:00:00Z", "headRefName": f"pl/{name}"}
    return wt_path, {"id": name, "title": name, "list_id": "Done", "metadata": {"pr_urls": [pr_url], **(metadata or {})}}


def _removals(wt_env):
    return [cmd for cmd, _ in wt_env.commands if cmd[:3] == ["git", "worktree", "remove"]]


def test_sweep_keeps_worktree_of_live_agent_and_says_so(wt_env, monkeypatch, capsys):
    from pl import worktrees
    monkeypatch.setattr(worktrees, "registry", lambda: {})
    monkeypatch.setattr(worktrees, "worker_status", lambda w, reg: ("alive", None))
    wt_path, card = _merged_done_card(wt_env, metadata={"worker": {"pane": "%1"}})
    worktrees.sweep([card])
    assert _removals(wt_env) == []
    out = capsys.readouterr().out
    assert str(wt_path) in out and "c1" in out and "skipped: agent running" in out


def test_sweep_keeps_worktree_of_trusting_agent_with_reason(wt_env, monkeypatch, capsys):
    from pl import agents, worktrees
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    trusting_path, trusting = _merged_done_card(wt_env, "trusting", {"worker": {"trust_wait": "/x"}})
    monkeypatch.setattr(worktrees, "registry", lambda: {})
    monkeypatch.setattr(worktrees, "worker_status", lambda w, reg: ("alive", None))
    worktrees.sweep([trusting])
    assert _removals(wt_env) == []
    lines = capsys.readouterr().out.splitlines()
    assert any("trusting" in ln and "skipped: agent held" in ln and "trust the folder" in ln for ln in lines)


def test_sweep_is_quiet_about_cards_without_an_agent(wt_env, capsys):
    from pl import worktrees
    path = C.WORKTREES_DIR / "repo-pl-idle"
    path.mkdir()
    wt_env.worktrees = [{"path": path, "branch": "pl/idle"}]
    worktrees.sweep([{"id": "idle", "title": "idle", "list_id": "In progress", "metadata": {}}])
    assert "skipped" not in capsys.readouterr().out


def test_sweep_writes_worktree_cleanup_event(wt_env, tmp_path):
    from pl import events, worktrees
    C.STATE_DIR = tmp_path / "state"
    wt_path, card = _merged_done_card(wt_env)
    worktrees.sweep([card])
    found = [e for e in events._read() if e["kind"] == "worktree_cleanup"]
    assert len(found) == 1
    assert (found[0]["card"], found[0]["path"], found[0]["by"]) == ("c1", str(wt_path), "sweep")


def _missing_card(tmp_path, column="In progress"):
    return {"id": "c9", "title": "c9", "list_id": column, "metadata": {"worktree": str(tmp_path / "gone")}}


def _removed_events():
    from pl import events
    return [e for e in events._read() if e["kind"] == "worktree_removed"]


def test_check_missing_reports_removed_outside_pl_once(tmp_path, monkeypatch):
    from pl import worktrees
    C.STATE_DIR = tmp_path / "state"
    monkeypatch.setattr(worktrees, "col_name", lambda lid: lid)
    cards = [_missing_card(tmp_path)]
    worktrees.check_missing(cards)
    worktrees.check_missing(cards)
    found = _removed_events()
    assert len(found) == 1 and found[0]["card"] == "c9" and found[0]["path"] == str(tmp_path / "gone")
    assert found[0]["message"] == "removed outside pl"


def test_check_missing_names_pl_cleanup_when_it_removed_it(tmp_path, monkeypatch):
    from pl import events, worktrees
    C.STATE_DIR = tmp_path / "state"
    monkeypatch.setattr(worktrees, "col_name", lambda lid: lid)
    events.emit("worktree_cleanup", "c9", path=str(tmp_path / "gone"), by="gc")
    worktrees.check_missing([_missing_card(tmp_path)])
    assert _removed_events()[0]["message"].startswith("removed by pl cleanup at ")


def test_check_missing_ignores_existing_and_done_worktrees(tmp_path, monkeypatch):
    from pl import worktrees
    C.STATE_DIR = tmp_path / "state"
    monkeypatch.setattr(worktrees, "col_name", lambda lid: lid)
    here = tmp_path / "here"
    here.mkdir()
    worktrees.check_missing([{"id": "c8", "list_id": "In progress", "metadata": {"worktree": str(here)}},
                             _missing_card(tmp_path, "Done"),
                             {"id": "c7", "list_id": "In progress", "metadata": {}}])
    assert _removed_events() == []


def test_dispatch_once_calls_check_missing(monkeypatch):
    from pl import dispatch, worktrees
    checked = []
    monkeypatch.setattr(worktrees, "sweep", lambda cards, dry: None)
    monkeypatch.setattr(worktrees, "check_missing", lambda cards: checked.append(cards))
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
    assert checked == [test_cards]


def test_sweep_runs_only_worktree_commands_in_the_main_checkout(wt_env):
    from pl import worktrees

    cards = []
    for name, meta in (("merged", {"pr_urls": ["https://github.com/org/repo/pull/9"]}), ("dropped", {"dropped_at": "2026-10-23T00:00:00Z"})):
        path = C.WORKTREES_DIR / f"repo-pl-{name}"
        path.mkdir()
        wt_env.worktrees.append({"path": path, "branch": f"pl/{name}"})
        cards.append({"id": name, "title": name, "list_id": "Done", "metadata": {"pipeline_mode": "auto", **meta}})
    wt_env.pr_views["https://github.com/org/repo/pull/9"] = {"state": "MERGED", "mergedAt": "2026-10-23T00:00:00Z", "headRefName": "pl/merged"}

    worktrees.sweep(cards)

    allowed = (["git", "worktree", "list"], ["git", "worktree", "remove"], ["git", "worktree", "prune"])
    in_main = [cmd for cmd, cwd in wt_env.commands if str(cwd) == str(C.WORK_DIR)]
    assert any(cmd[:3] == ["git", "worktree", "remove"] for cmd in in_main)
    assert all(cmd[:3] in allowed for cmd in in_main), in_main
    assert not [cmd for cmd, _ in wt_env.commands if {"reset", "checkout", "stash", "clean"} & set(cmd)]


def test_sweep_deletes_leftover_folder_after_remove(wt_env):
    from pl import worktrees

    wt_path, card = _merged_done_card(wt_env)
    (wt_path / ".venv").mkdir()
    (wt_path / ".venv" / "x").write_text("x")
    worktrees.sweep([card])
    assert not wt_path.exists()


def test_sweep_names_a_folder_it_could_not_delete(wt_env, capsys, monkeypatch):
    from pl import worktrees

    wt_path, card = _merged_done_card(wt_env)
    monkeypatch.setattr(worktrees.shutil, "rmtree", lambda *a, **kw: None)
    worktrees.sweep([card])
    lines = [l for l in capsys.readouterr().out.splitlines() if str(wt_path) in l]
    assert len(lines) == 1


# --- Slice 2: each worktree belongs to its own card ---


def _c(id, title, column="Done", **meta):
    return {"id": id, "title": title, "list_id": column, "metadata": meta}


def test_match_ignores_a_card_sharing_the_first_20_characters(wt_env):
    from pl import worktrees
    cards = [_c("a", "add the export button to the report page"), _c("b", "add the export button to the board")]
    p = C.WORKTREES_DIR / "repo-pl-add-the-export-button-to-the-board"
    assert worktrees._match_card(p, "", cards)["id"] == "b"
    p2 = C.WORKTREES_DIR / "repo-pl-add-the-export-button-to-something-else"
    assert worktrees._match_card(p2, "", cards) is None


def test_match_ignores_a_card_number_inside_the_name(wt_env):
    from pl import worktrees
    p = C.WORKTREES_DIR / "repo-pl-slice-2-of-the-thing"
    assert worktrees._match_card(p, "pl/slice-2-of-the-thing", [_c("o/r#2", "other work")]) is None


def test_match_uses_recorded_worktree_then_branch_then_slug(wt_env):
    from pl import worktrees
    p = C.WORKTREES_DIR / "repo-pl-zzz"
    by_path = _c("p", "one", worktree=str(p))
    by_branch = _c("b", "two", branch="pl/zzz")
    by_slug = _c("s", "zzz")
    assert worktrees._match_card(p, "pl/zzz", [by_slug, by_branch, by_path])["id"] == "p"
    assert worktrees._match_card(p, "pl/zzz", [by_slug, by_branch])["id"] == "b"
    assert worktrees._match_card(p, "pl/zzz", [by_slug])["id"] == "s"


def test_match_accepts_slice_branch(wt_env):
    from pl import worktrees
    p = C.WORKTREES_DIR / "repo-pl-other-name"
    assert worktrees._match_card(p, "pl/zzz-s3", [_c("s", "zzz")])["id"] == "s"
    assert worktrees._match_card(p, "pl/zzz-sx", [_c("s", "zzz")]) is None


def test_two_cards_same_slug_prefers_the_card_not_done(wt_env):
    from pl import worktrees
    p = C.WORKTREES_DIR / "repo-pl-zzz"
    cards = [_c("old", "zzz", "Done"), _c("live", "zzz", "In progress")]
    assert worktrees._match_card(p, "pl/zzz", cards)["id"] == "live"


def test_no_match_asks_github_nothing(wt_env):
    from pl import worktrees
    wt_path = C.WORKTREES_DIR / "repo-pl-nobody"
    wt_path.mkdir()
    wt_env.worktrees = [{"path": wt_path, "branch": "pl/nobody"}]
    others = [_c(f"o{i}", f"Other {i}", pr_urls=[f"https://github.com/org/repo/pull/{i}"]) for i in range(5)]
    worktrees.sweep(others)
    assert _gh_views(wt_env) == []


# --- Slice 3: one decision for report, gc and the dispatcher ---

MERGED = {"state": "MERGED", "mergedAt": "2026-10-23T00:00:00Z"}


def _gh_lists(runner):
    return [cmd for cmd, _ in runner.commands if cmd[:3] == ["gh", "pr", "list"]]


def _verdict_of(card):
    from pl import worktrees
    rows = worktrees.inventory([card], sizes=False)
    return rows[0]["action"], rows[0]["reason"]


def test_commit_after_pr_last_commit_is_unpushed(wt_env):
    path, card = _merged_done_card(wt_env)
    wt_env.unpushed_branches.add("pl/c1")
    assert _verdict_of(card) == ("keep", "unpushed commits")


def test_squash_merged_deleted_branch_head_equals_pr_is_removed(wt_env):
    path, card = _merged_done_card(wt_env)
    wt_env.unpushed_branches.add("pl/c1")   # the branch was deleted, so every commit looks unpushed
    wt_env.heads[path] = "abc123"
    url = card["metadata"]["pr_urls"][0]
    wt_env.pr_views[url]["headRefOid"] = "abc123"
    assert _verdict_of(card) == ("remove", "finished card")


def test_old_cache_entry_without_oid_is_asked_once_more(wt_env, tmp_path):
    from pl import worktrees
    C.STATE_DIR = tmp_path / "state"
    path, card = _merged_done_card(wt_env)
    url = card["metadata"]["pr_urls"][0]
    worktrees._settled_path().parent.mkdir(parents=True)
    worktrees._settled_path().write_text(json.dumps({url: {"state": "MERGED", "headRefName": "pl/c1"}}))
    worktrees.inventory([card], sizes=False)
    worktrees.inventory([card], sizes=False)
    assert len(_gh_views(wt_env)) == 1


def test_done_card_without_pr_finds_merged_pr_by_branch(wt_env):
    path, card = _merged_done_card(wt_env)
    card["metadata"]["pr_urls"] = []
    wt_env.pr_lists["pl/c1"] = {**MERGED, "headRefName": "pl/c1", "headRefOid": "x"}
    assert _verdict_of(card) == ("remove", "finished card")


def test_done_card_with_no_pr_anywhere_keeps_with_reason(wt_env):
    path, card = _merged_done_card(wt_env)
    card["metadata"]["pr_urls"] = []
    assert _verdict_of(card) == ("keep", "no merged PR on the card")


def test_branch_miss_is_not_asked_again_within_a_day(wt_env, tmp_path):
    from pl import worktrees
    C.STATE_DIR = tmp_path / "state"
    path, card = _merged_done_card(wt_env)
    card["metadata"]["pr_urls"] = []
    worktrees.inventory([card], sizes=False)
    worktrees.inventory([card], sizes=False)
    assert len(_gh_lists(wt_env)) == 1


def test_gh_failure_keeps_the_worktree(wt_env):
    path, card = _merged_done_card(wt_env)
    wt_env.gh_fails = True
    assert _verdict_of(card)[0] == "keep"


def test_several_repos_keeps_every_row(wt_env):
    path, card = _merged_done_card(wt_env)
    (C.WORK_DIR / "a").mkdir()
    (C.WORK_DIR / "a" / ".git").write_text("x")
    assert _verdict_of(card) == ("keep", "several repositories in the work folder")


def test_sweep_removes_merged_clean_worktree_and_names_card_and_path(wt_env, tmp_path):
    from pl import events, worktrees
    C.STATE_DIR = tmp_path / "state"
    path, card = _merged_done_card(wt_env)
    worktrees.sweep([card])
    assert len(_removals(wt_env)) == 1 and str(path) in _removals(wt_env)[0]
    ev = [e for e in events._read() if e["kind"] == "worktree_cleanup"]
    assert (ev[0]["card"], ev[0]["path"]) == ("c1", str(path))


def test_sweep_leaves_dirty_and_untracked(wt_env):
    from pl import worktrees
    path, card = _merged_done_card(wt_env)
    wt_env.dirty_paths.add(path)
    worktrees.sweep([card])
    assert _removals(wt_env) == []


def test_sweep_leaves_card_not_done_and_live_agent(wt_env, monkeypatch):
    from pl import worktrees
    path, card = _merged_done_card(wt_env)
    card["list_id"] = "PR open"
    worktrees.sweep([card])
    assert _removals(wt_env) == []


def test_sweep_leaves_uninspectable(wt_env):
    from pl import worktrees
    path, card = _merged_done_card(wt_env)
    wt_env.status_fails.add(path)
    worktrees.sweep([card])
    assert _removals(wt_env) == []


def test_cleanup_false_removes_nothing_and_report_still_shows(wt_env):
    from pl import worktrees
    path, card = _merged_done_card(wt_env)
    C.WORKTREES = {"cleanup": False}
    worktrees.sweep([card])
    assert _removals(wt_env) == []
    assert worktrees.inventory([card], sizes=False)[0]["action"] == "remove"


def test_report_and_sweep_agree_on_every_case(wt_env):
    from pl import worktrees
    cases = {"fin": {}, "dirty": {"dirty": 1}, "ahead": {"ahead": 1}, "live": {"col": "In progress"},
             "nopr": {"nopr": 1}, "broken": {"broken": 1}, "dropped": {"dropped": 1}}
    cards, registered = [], []
    for name, how in cases.items():
        path, card = _merged_done_card(wt_env, name)
        registered += wt_env.worktrees
        if how.get("dirty"):
            wt_env.dirty_paths.add(path)
        if how.get("ahead"):
            wt_env.unpushed_branches.add(f"pl/{name}")
        if how.get("col"):
            card["list_id"] = how["col"]
        if how.get("nopr"):
            card["metadata"]["pr_urls"] = []
        if how.get("broken"):
            wt_env.status_fails.add(path)
        if how.get("dropped"):
            card["metadata"] = {"dropped_at": "2026-10-23T00:00:00Z"}
        cards.append(card)
    wt_env.worktrees = registered
    expected = {r["name"] for r in worktrees.inventory(cards, sizes=False) if r["action"] == "remove"}
    worktrees.sweep(cards)
    removed = {Path(cmd[-1]).name for cmd in _removals(wt_env)}
    assert removed == expected == {"repo-pl-fin", "repo-pl-dropped"}


def test_sweep_does_not_walk_sizes(wt_env, monkeypatch):
    from pl import worktrees
    path, card = _merged_done_card(wt_env)
    monkeypatch.setattr(worktrees, "_size", lambda p: pytest.fail("walked"))
    worktrees.sweep([card])
    assert len(_removals(wt_env)) == 1


# --- Slice 4: stale_days and the stale verdicts ---


def test_stale_days_default_is_14(fake_home):
    _profile(fake_home)
    assert C.WORKTREES["stale_days"] == 14
    _profile(fake_home, "[worktrees]\nstale_days = 3\n")
    assert C.WORKTREES["stale_days"] == 3


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", '"7"', "true"])
def test_stale_days_validation_error(bad):
    assert "worktrees.stale_days must be a positive whole number" in C.validate(tomlkit.parse(f"[worktrees]\nstale_days = {bad}\n"))


def test_stale_days_validation_accepts_positive_whole_number():
    assert C.validate(tomlkit.parse("[worktrees]\nstale_days = 7\n")) == []


# --- Slice 5: stale retention and gc --stale ---


def _stale_folder(name="repo-pl-c1", age_days=20, record=None):
    import datetime as dt
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
    d = C.WORKTREES_DIR / f"{name}.stale-{when:%Y%m%d-%H%M%S}"
    d.mkdir()
    (d / "f").write_text("x")
    if record is not None:
        (d / ".pl-set-aside.json").write_text(json.dumps(record))
    return d


SAVED = {"uncommitted": False, "unpushed": 0}


def test_sweep_deletes_expired_saved_stale_and_emits_event(wt_env, tmp_path):
    from pl import events, worktrees
    C.STATE_DIR = tmp_path / "state"
    d = _stale_folder(record=SAVED)
    worktrees.sweep([_c("c1", "c1", "Done")])
    assert not d.exists()
    [ev] = [e for e in events._read() if e["kind"] == "worktree_cleanup"]
    assert (ev["card"], ev["path"], ev["by"]) == ("c1", str(d), "sweep")


def test_sweep_keeps_young_unsaved_and_unknown_stale(wt_env):
    from pl import worktrees
    young = _stale_folder("repo-pl-a", 2, SAVED)
    unsaved = _stale_folder("repo-pl-b", 90, {"uncommitted": True, "unpushed": 0})
    unknown = _stale_folder("repo-pl-c", 90)
    worktrees.sweep([])
    assert young.exists() and unsaved.exists() and unknown.exists()


def test_cleanup_false_keeps_stale(wt_env):
    from pl import worktrees
    d = _stale_folder(record=SAVED)
    C.WORKTREES = {"cleanup": False, "stale_days": 14}
    worktrees.sweep([])
    assert d.exists()


def test_remove_stale_refuses_path_outside_worktrees_dir(wt_env, tmp_path, capsys):
    from pl import worktrees
    outside = tmp_path / "elsewhere.stale-20260101-000000"
    outside.mkdir()
    row = {"name": outside.name, "path": str(outside), "card": None, "action": "remove", "stale": True}
    assert worktrees._remove([row]) == (0, 1, 0)
    assert outside.exists()


def test_remove_stale_never_follows_a_symlink(wt_env, tmp_path):
    from pl import worktrees
    target = tmp_path / "precious"
    target.mkdir()
    (target / "f").write_text("x")
    link = C.WORKTREES_DIR / "repo-pl-c1.stale-20260101-000000"
    link.symlink_to(target)
    worktrees._remove([{"name": link.name, "path": str(link), "card": None, "action": "remove", "stale": True}])
    assert (target / "f").exists() and link.is_symlink()


def test_rmtree_failure_is_reported_and_emits_nothing(wt_env, tmp_path, monkeypatch, capsys):
    from pl import events, worktrees
    C.STATE_DIR = tmp_path / "state"
    d = _stale_folder(record=SAVED)
    monkeypatch.setattr(worktrees.shutil, "rmtree", lambda *a, **k: None)
    worktrees.sweep([_c("c1", "c1", "Done")])
    assert d.exists() and str(d) in capsys.readouterr().out
    assert [e for e in events._read() if e["kind"] == "worktree_cleanup"] == []


def test_report_and_sweep_agree_with_stale_folders(wt_env):
    from pl import worktrees
    gone = _stale_folder("repo-pl-a", 20, SAVED)
    _stale_folder("repo-pl-b", 20)
    _stale_folder("repo-pl-c", 1, SAVED)
    expected = {r["name"] for r in worktrees.inventory([], sizes=False) if r["action"] == "remove"}
    worktrees.sweep([])
    left = {p.name for p in C.WORKTREES_DIR.iterdir()}
    assert expected == {gone.name} and gone.name not in left and len(left) == 2
