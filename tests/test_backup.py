import argparse
import json
import signal

import pytest

import pl.update
from pl import alerts, backup, board, dispatch, events, manager, profiles, skills
from pl import config as C

NOW = 1_000_000.0


class FakeTracker:
    def __init__(self):
        self.reads = 0

    def columns(self):
        return {"Inbox": "c1", "Approved": "c2"}

    def cards(self, query=None):
        self.reads += 1
        return [
            {"id": "2", "list_id": "c2", "title": "b", "description": "d", "tags": ["x"], "assigned_to": "me",
             "metadata": {"token": "${GH_TOKEN}"}, "updated_at": "t2", "seq": 9, "board_id": "b1"},
            {"id": "1", "list_id": "c1", "title": "a", "description": "", "tags": [], "assigned_to": None,
             "metadata": {}, "updated_at": "t1", "seq": 3, "board_id": "b1"},
        ]


class Git:
    """Scripted stand-in for backup._run: records argv and answers by git subcommand."""

    def __init__(self):
        self.calls = []
        self.push_rc = 0
        self.push_err = "fatal: unable to access\nmore"
        self.diff_rc = 1
        self.commit_rc = 0

    def __call__(self, argv):
        self.calls.append(argv)
        sub = argv[3]
        if sub == "push":
            return self.push_rc, "", self.push_err if self.push_rc else ""
        if sub == "diff":
            return self.diff_rc, "", ""
        if sub == "commit":
            return self.commit_rc, "", "Author identity unknown" if self.commit_rc else ""
        return 0, "", ""

    def subs(self):
        return [c[3] for c in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(C, "TRACKER", {"type": "local"})
    monkeypatch.setattr(C, "SYNC", {"git": str(clone)})
    tracker = FakeTracker()
    monkeypatch.setattr(backup.trackers, "get", lambda kind: tracker)
    git = Git()
    monkeypatch.setattr(backup, "_run", git)
    clock = [NOW]
    monkeypatch.setattr(backup, "_clock", lambda: clock[0])
    monkeypatch.setattr(alerts, "_clock", lambda: clock[0])
    marks = [0]
    monkeypatch.setattr(board, "last_write", lambda: marks[0] or None)
    emitted = []
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, **d: kind.startswith("alert_") or emitted.append((kind, d)))
    monkeypatch.setattr(alerts, "_offer", lambda *a, **k: None)
    e = argparse.Namespace(clone=clone, tracker=tracker, git=git, clock=clock, marks=marks, events=emitted)
    backup._ok = None
    assert backup.check()
    git.calls.clear()
    yield e
    for k, v in saved.items():
        setattr(C, k, v)
    backup._ok = None


def write(env):
    env.marks[0] = env.clock[0]


def test_snapshot_is_sorted_and_stable(env):
    first, second = backup.snapshot(), backup.snapshot()
    assert first == second
    got = json.loads(first)
    assert [c["id"] for c in got["cards"]] == ["1", "2"]
    assert got["columns"] == [{"id": "c1", "title": "Inbox"}, {"id": "c2", "title": "Approved"}]
    for c in got["cards"]:
        assert set(c) == {"id", "column", "title", "description", "tags", "assigned_to", "metadata"}
    assert got["cards"][1]["column"] == "Approved"
    assert first.endswith("\n")


def test_snapshot_keeps_var_refs_and_no_config(env, monkeypatch):
    monkeypatch.setattr(C, "TRACKER", {"type": "local", "secret_key": "s3cret"})
    out = backup.snapshot()
    assert "${GH_TOKEN}" in out
    assert "s3cret" not in out and "secret_key" not in out and "updated_at" not in out and "seq" not in out


def test_change_commits_and_pushes_within_window(env):
    write(env)
    backup.tick()
    assert env.git.calls == []
    env.clock[0] += 280
    backup.tick()
    cmds = [c[3:] for c in env.git.calls]
    assert cmds[0] == ["add", "--", "board.json"]
    assert cmds[1] == ["diff", "--cached", "--quiet", "--", "board.json"]
    assert cmds[2][0] == "commit" and cmds[2][1] == "-m" and cmds[2][2].startswith("pl board backup ")
    assert cmds[2][2].endswith("Z") and cmds[2][3:] == ["--", "board.json"]
    assert cmds[3] == ["push"]
    assert json.loads((env.clone / "board.json").read_text())["cards"]
    assert all(c[:3] == ["git", "-C", str(env.clone)] for c in env.git.calls)


def test_twenty_changes_one_commit(env):
    for _ in range(20):
        env.clock[0] += 10
        write(env)
        backup.tick()
    assert "commit" not in env.git.subs()
    env.clock[0] += 300
    backup.tick()
    assert env.git.subs().count("commit") == 1


def test_no_change_no_commit(env):
    backup.tick()
    env.clock[0] += 1000
    backup.tick()
    assert env.git.calls == [] and env.tracker.reads == 0


def test_same_content_makes_no_commit(env):
    write(env)
    env.git.diff_rc = 0
    env.clock[0] += 300
    backup.tick()
    assert "commit" not in env.git.subs() and "push" not in env.git.subs()


def test_force_flushes_pending(env):
    env.clock[0] += 10
    write(env)
    backup.tick(force=True)
    assert env.git.subs()[-2:] == ["commit", "push"]


def test_push_failure_retried_next_window(env):
    write(env)
    env.git.push_rc = 1
    env.clock[0] += 300
    backup.tick()
    assert ("backup_failed", {"message": "fatal: unable to access"}) in env.events
    n = env.git.subs().count("push")
    env.clock[0] += 60
    backup.tick()
    assert env.git.subs().count("push") == n
    env.clock[0] += 300
    backup.tick()
    assert env.git.subs().count("push") == n + 1
    assert env.git.subs().count("commit") == 1


def test_third_failure_opens_alert_and_success_resolves(env):
    write(env)
    env.git.push_rc = 1
    for i in range(3):
        env.clock[0] += 300
        backup.tick()
        live = alerts.get("backup_push") is not None
        assert live == (i == 2)
    title = alerts.get("backup_push")["title"]
    assert str(env.clone) in title and "fatal: unable to access" in title
    env.git.push_rc = 0
    env.clock[0] += 300
    backup.tick()
    assert alerts.get("backup_push").get("resolved_at")


def test_failed_commit_counts_as_failure(env):
    write(env)
    env.git.commit_rc = 1
    env.clock[0] += 300
    backup.tick()
    assert "push" not in env.git.subs()
    assert ("backup_failed", {"message": "Author identity unknown"}) in env.events


def test_not_a_clone_one_error_one_alert(env, monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setattr(C, "SYNC", {"git": str(missing)})
    env.git.calls.clear()
    env.events.clear()
    assert backup.check() is False
    title = f"[sync] git: {missing} is not a git clone"
    assert env.events == [("error", {"message": title})]
    assert alerts.get("backup_clone")["title"] == title
    write(env)
    env.clock[0] += 400
    backup.tick(force=True)
    assert env.git.calls == []


def test_rev_parse_failure_is_not_a_clone(env, monkeypatch):
    monkeypatch.setattr(backup, "_run", lambda argv: (128, "", "no"))
    assert backup.check() is False
    assert alerts.get("backup_clone")


def test_no_setting_does_nothing(env, monkeypatch):
    monkeypatch.setattr(C, "SYNC", {})
    backup._ok = None
    env.git.calls.clear()
    env.events.clear()
    assert backup.check() is False
    write(env)
    backup.tick(force=True)
    assert env.git.calls == [] and env.events == [] and alerts.get("backup_clone") is None


def test_github_tracker_never_backed_up(env, monkeypatch):
    monkeypatch.setattr(C, "TRACKER", {"type": "github-project"})
    backup._ok = None
    env.git.calls.clear()
    assert backup.check() is False
    write(env)
    backup.tick(force=True)
    assert env.git.calls == [] and env.tracker.reads == 0


def test_tick_never_raises(env, monkeypatch):
    def boom(argv):
        raise RuntimeError("boom")
    monkeypatch.setattr(backup, "_run", boom)
    write(env)
    backup.tick(force=True)
    assert any(k == "error" for k, _ in env.events)


def _args(**kw):
    return argparse.Namespace(**{"once": True, "dry_run": False, "no_pull": True, "max_runs": None, "max_prep": None,
                                 "interval": None, **kw})


@pytest.fixture
def pass_env(env, monkeypatch):
    monkeypatch.setattr(manager, "manages", lambda name: False)
    monkeypatch.setattr(dispatch, "dispatch_lock", lambda: object())
    monkeypatch.setattr(profiles, "shared_warnings", lambda *a: [])
    monkeypatch.setattr(profiles, "list_profiles", lambda: [])
    monkeypatch.setattr(profiles, "current_row", lambda: None)
    monkeypatch.setattr(skills, "install_builtins", lambda: [])
    monkeypatch.setattr(pl.update, "start_background", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a: ([], []))
    seen = []
    monkeypatch.setattr(backup, "check", lambda: seen.append("check") or True)
    monkeypatch.setattr(backup, "tick", lambda force=False: seen.append(("tick", force)))
    monkeypatch.setattr(signal, "signal", lambda *a: seen.append(("signal", a[0])))
    return seen


def test_dispatcher_flushes_on_shutdown(pass_env):
    dispatch.cmd_dispatch(_args())
    assert pass_env.count("check") == 1
    assert pass_env[-1] == ("tick", True)


def test_dispatcher_flushes_on_sigterm_exit(pass_env, monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda s, h: handlers.setdefault(s, h))

    def term(*a):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(dispatch, "dispatch_once", term)
    dispatch.cmd_dispatch(_args())
    assert pass_env[-1] == ("tick", True)


def test_backup_error_does_not_stop_pass(env, monkeypatch):
    monkeypatch.setattr(manager, "manages", lambda name: False)
    monkeypatch.setattr(dispatch, "dispatch_lock", lambda: object())
    monkeypatch.setattr(profiles, "shared_warnings", lambda *a: [])
    monkeypatch.setattr(profiles, "list_profiles", lambda: [])
    monkeypatch.setattr(profiles, "current_row", lambda: None)
    monkeypatch.setattr(skills, "install_builtins", lambda: [])
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(pl.update, "start_background", lambda *a, **k: None)
    passes = []
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a: passes.append(a) or ([], []))

    def boom(argv):
        raise RuntimeError("boom")
    monkeypatch.setattr(backup, "_run", boom)
    write(env)
    dispatch.cmd_dispatch(_args())
    assert len(passes) == 1


def test_dry_run_never_backs_up(pass_env):
    dispatch.cmd_dispatch(_args(dry_run=True))
    assert pass_env == []


def test_failure_message_hides_url_credentials(env):
    write(env)
    env.git.push_rc = 1
    env.git.push_err = "fatal: unable to access 'https://user:s3cretpw@host.test/r.git/': denied"
    for _ in range(3):
        env.clock[0] += 300
        backup.tick()
    shown = repr(env.events) + alerts.get("backup_push")["title"]
    assert "s3cretpw" not in shown and "user:" not in shown
    assert "https://***@host.test/r.git/" in alerts.get("backup_push")["title"]


def test_refresh_commits_and_pushes_now(env):
    backup.refresh()
    assert env.git.subs()[-2:] == ["commit", "push"]


def test_refresh_without_sync_does_nothing(env, monkeypatch):
    monkeypatch.setattr(C, "SYNC", {})
    assert backup.refresh() is False
    assert env.git.calls == []
