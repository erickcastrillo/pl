"""A stray change in the main checkout raises one alert and one notification, and pl never touches it (Plan 55 slice 2)."""
import subprocess

import pytest

from pl import alerts, checkout_watch, dispatch, events
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.WORK_DIR = tmp_path / "repo"
    C.WORK_DIR.mkdir()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class Git:
    def __init__(self):
        self.porcelain, self.rc, self.argvs = "", 0, []

    def __call__(self, argv):
        self.argvs.append(list(argv))
        return self.rc, self.porcelain


def _z(*entries):
    return "".join(e + "\0" for e in entries)


@pytest.fixture
def git(monkeypatch):
    g = Git()
    monkeypatch.setattr(checkout_watch, "_git", g)
    return g


@pytest.fixture
def seen(monkeypatch):
    out = {"events": [], "sent": []}
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, level=None, **d:
                        out["events"].append((kind, card_id, d)) if kind == "main_checkout_dirty" else None)
    return out


def _run_card(cid="o/r#1", title="Build the thing", stage="run"):
    return {"id": cid, "title": title, "list_id": "In progress", "metadata": {"worker": {"stage": stage}}}


def _check(seen, st, cards=(), dry=False):
    checkout_watch.check(list(cards), {}, st, dry, lambda t, f="": seen["sent"].append((t, f)))


def _live(monkeypatch, status="alive"):
    monkeypatch.setattr(checkout_watch, "worker_status", lambda w, reg: (status, None))


def test_a_new_change_logs_one_event_opens_one_high_alert_and_notifies_once(git, seen):
    git.porcelain = _z(" M src/a.py")
    _check(seen, {})
    assert seen["events"] == [("main_checkout_dirty", None, {"paths": ["src/a.py"], "agents": []})]
    a = alerts.get("main_checkout_dirty")
    assert alerts._live(a) and a["severity"] == "high"
    assert len(seen["sent"]) == 1 and "src/a.py" in seen["sent"][0][0]


def test_the_alert_and_notification_name_live_run_agents(git, seen, monkeypatch):
    _live(monkeypatch)
    git.porcelain = _z(" M a.py")
    _check(seen, {}, [_run_card(), _run_card("o/r#2", "A spec card", stage="spec")])
    title = alerts.get("main_checkout_dirty")["title"]
    assert "Build the thing" in title and "A spec card" not in title
    assert "Build the thing" in seen["sent"][0][1]
    assert seen["events"][0][2]["agents"] == ["o/r#1"]


def test_with_no_live_run_agent_the_alert_says_none_is_known(git, seen, monkeypatch):
    _live(monkeypatch, "dead")
    git.porcelain = _z("?? new.txt")
    _check(seen, {}, [_run_card()])
    assert "no live agent is known" in alerts.get("main_checkout_dirty")["title"]
    assert "no live agent is known" in seen["sent"][0][1]


def test_the_same_dirty_paths_on_the_next_pass_log_and_notify_nothing(git, seen):
    git.porcelain = _z(" M a.py")
    st = {}
    _check(seen, st)
    _check(seen, st)
    assert len(seen["events"]) == 1 and len(seen["sent"]) == 1


def test_a_second_path_logs_and_notifies_only_the_new_one(git, seen):
    st = {}
    git.porcelain = _z(" M a.py")
    _check(seen, st)
    git.porcelain = _z(" M a.py", " M b.py")
    _check(seen, st)
    assert seen["events"][1][2]["paths"] == ["b.py"]
    assert len(seen["sent"]) == 2 and "b.py" in seen["sent"][1][0] and "a.py" not in seen["sent"][1][0]
    title = alerts.get("main_checkout_dirty")["title"]
    assert "a.py" in title and "b.py" in title


def test_changes_only_under_dot_pl_are_ignored(git, seen):
    git.porcelain = _z("?? .pl/", "?? .pl/launch/x.md", " M .pl/e2e.yaml")
    _check(seen, {})
    assert seen == {"events": [], "sent": []} and alerts.get("main_checkout_dirty") is None


def test_the_check_runs_only_git_status(git, seen):
    st = {}
    for porcelain in (_z(" M a.py"), _z(" M a.py"), "", _z("?? b")):
        git.porcelain = porcelain
        _check(seen, st)
    assert git.argvs and all(a == ["-C", str(C.WORK_DIR), "--no-optional-locks", "status", "--porcelain", "-z"] for a in git.argvs)


def test_a_clean_checkout_resolves_the_alert_and_a_later_change_alerts_again(git, seen):
    st = {}
    git.porcelain = _z(" M a.py")
    _check(seen, st)
    git.porcelain = ""
    _check(seen, st)
    assert not alerts._live(alerts.get("main_checkout_dirty")) and st["main_dirty"] == []
    git.porcelain = _z(" M a.py")
    _check(seen, st)
    assert alerts._live(alerts.get("main_checkout_dirty")) and len(seen["events"]) == 2 and len(seen["sent"]) == 2


@pytest.mark.parametrize("how", ["rc", "oserror", "no_work_dir", "timeout"])
def test_git_failing_or_no_work_dir_leaves_state_and_opens_nothing(how, seen, monkeypatch):
    if how == "rc":
        g = Git()
        g.rc = 128
        monkeypatch.setattr(checkout_watch, "_git", g)
    elif how == "oserror":
        def boom(*a, **k):
            raise OSError("git missing")
        monkeypatch.setattr(subprocess, "run", boom)
    elif how == "timeout":
        def hang(argv, **k):
            raise subprocess.TimeoutExpired(argv, k["timeout"])
        monkeypatch.setattr(subprocess, "run", hang)
    else:
        C.WORK_DIR = None
    st = {"main_dirty": ["a.py"]}
    _check(seen, st)
    assert st == {"main_dirty": ["a.py"]} and seen == {"events": [], "sent": []}
    assert alerts.get("main_checkout_dirty") is None


def test_a_dry_pass_prints_and_changes_nothing(git, seen, capsys):
    git.porcelain = _z(" M a.py")
    st = {}
    _check(seen, st, dry=True)
    assert "would alert:" in capsys.readouterr().out
    assert st == {} and seen == {"events": [], "sent": []} and alerts.get("main_checkout_dirty") is None


def test_renamed_paths_report_the_new_name(git, seen):
    git.porcelain = _z("R  new.py", "old.py", " M b.py")
    _check(seen, {})
    assert seen["events"][0][2]["paths"] == ["b.py", "new.py"]


def test_git_runs_with_a_timeout_and_no_terminal_prompt(monkeypatch):
    seen = {}

    def run(argv, **k):
        seen.update(k)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert checkout_watch._git(["status"]) == (0, "")
    assert seen["timeout"] == 30 and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("exc", [RuntimeError("nope"), SystemExit("nope")])
def test_dispatch_goes_on_when_the_check_raises(monkeypatch, capsys, exc):
    def boom(*a):
        raise exc
    monkeypatch.setattr(checkout_watch, "check", boom)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    dispatch.dispatch_once(1, True, pull=False)
    assert "main checkout check skipped this pass: nope" in capsys.readouterr().err
