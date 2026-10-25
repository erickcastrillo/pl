"""Plan 29 slice 2: the built-in review loop alerts once per start when its repository has auto-merge off."""
import subprocess
import types

import pytest

from pl import accounts, alerts, dispatch
from pl import config as C
from pl.trackers import github

KEY = "auto_merge_off:o/r"


def _gh_says(monkeypatch, *outcomes):
    calls = []

    def run(args):
        calls.append(args)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return types.SimpleNamespace(**out)

    monkeypatch.setattr(github, "_run", run)
    monkeypatch.setattr(github, "limited", lambda resource: None)
    monkeypatch.setattr(github, "_budget_wait", lambda kind: None)
    return calls


def _ok(text):
    return {"returncode": 0, "stdout": text, "stderr": ""}


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    C.ACCOUNTS = {"cl": {"harness": "claude", "config_dir": str(tmp_path / ".claude-cl")}}
    C.PROFILES = {"cl": tmp_path / ".claude-cl"}
    C.TRACKER = {"type": "github-project", "repo": "o/r"}
    C.CODE_HOST = {"owner": "o", "labels": {"review": "pl:auto-review", "ready": "pl:ready", "merge_ready": "pl:merge-ready"}}
    C.SERVICES = {"auto-review": {"prompt": "/loop 30m /pl-review x", "profile": "cl", "builtin": True}}
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def loop(monkeypatch):
    seen = {"launches": 0, "notes": [], "panes": ""}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=seen["panes"], stderr=""))
    monkeypatch.setattr(dispatch, "_launch", lambda *a, **kw: seen.update(launches=seen["launches"] + 1))
    monkeypatch.setattr(dispatch, "_loop_pane", lambda name: "%9")
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h: None)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **kw: seen["notes"].append(a))
    return seen


def _tick(dry=False):
    dispatch.ensure_services({}, [], {}, dry, None)


def test_auto_merge_allowed_reads_the_repo_setting(monkeypatch):
    calls = _gh_says(monkeypatch, _ok("false\n"), _ok("true\n"))
    assert github.auto_merge_allowed("o/r") is False
    assert github.auto_merge_allowed("o/r") is True
    assert calls[0] == ["api", "repos/o/r", "--jq", ".allow_auto_merge"]


@pytest.mark.parametrize("outcome", [_ok("null\n"), {"returncode": 1, "stdout": "", "stderr": "HTTP 404"}, SystemExit("no gh")])
def test_auto_merge_allowed_is_none_when_unreadable(monkeypatch, outcome):
    _gh_says(monkeypatch, outcome)
    assert github.auto_merge_allowed("o/r") is None


def test_auto_merge_off_opens_one_warn_alert(monkeypatch, loop):
    _gh_says(monkeypatch, _ok("false\n"))
    _tick()
    a = alerts.get(KEY)
    assert a["severity"] == "warn" and a["title"].startswith("Auto-merge is off for o/r")
    assert "Allow auto-merge" in a["fix"] and len(loop["notes"]) == 1


def test_the_setting_is_read_once_per_loop_start_and_alerts_once(monkeypatch, loop):
    calls = _gh_says(monkeypatch, _ok("false\n"))
    _tick()
    loop["panes"] = "@7 %9 claude auto-review\n"
    _tick()
    assert len(calls) == 1
    loop["panes"] = ""
    _tick()
    assert len(calls) == 2
    assert [a["key"] for a in alerts.listing() if a["key"].startswith("auto_merge_off:")] == [KEY]


def test_auto_merge_on_resolves_the_open_alert(monkeypatch, loop):
    alerts.open(KEY, "warn", "Auto-merge is off for o/r", "fix")
    _gh_says(monkeypatch, _ok("true\n"))
    _tick()
    assert alerts.get(KEY)["resolved_at"] and loop["notes"] == []


@pytest.mark.parametrize("outcome", [{"returncode": 1, "stdout": "", "stderr": "HTTP 403"}, _ok("null\n"), SystemExit("gh timed out")])
def test_unreadable_setting_starts_the_loop_without_an_alert(monkeypatch, loop, capsys, outcome):
    _gh_says(monkeypatch, outcome)
    _tick()
    assert loop["launches"] == 1 and alerts.get(KEY) is None
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "o/r" in ln]
    assert len(lines) == 1


@pytest.mark.parametrize("case", ["no_merge_ready", "user_defined", "disabled", "dry_run"])
def test_no_read_without_merge_ready_or_for_other_loops(monkeypatch, loop, case):
    calls = _gh_says(monkeypatch, _ok("false\n"))
    if case == "no_merge_ready":
        C.CODE_HOST["labels"].pop("merge_ready")
    elif case == "user_defined":
        C.SERVICES["auto-review"].pop("builtin")
    elif case == "disabled":
        C.SERVICES = {}
    _tick(dry=case == "dry_run")
    assert calls == [] and alerts.get(KEY) is None
