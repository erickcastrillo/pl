import json
import subprocess
import time

import pytest

from pl import config as C
from pl import ghquota

ACCT = "me@github.com"


@pytest.fixture(autouse=True)
def ledger(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "PROFILE_NAME", "p")
    monkeypatch.setattr(ghquota, "_clock", lambda: time.time())
    monkeypatch.setattr(ghquota, "account", lambda folder=None: ACCT)
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    reset = int(time.time()) + 1800
    ghquota._write(ghquota._path(), {"limit": 5000, "remaining": 5000, "reset": reset, "at": time.time(),
                                     "window": reset, "spent": {}, "by": {}, "subs": {}})
    yield
    ghquota.set_role("command")


class Runner:
    def __init__(self, out="", code=0, error=None):
        self.calls, self.out, self.code, self.error = [], out, code, error

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code, self.out, "")


def test_gh_records_role_and_subcommand():
    ghquota.set_role("console")
    run = Runner()
    ghquota.gh(run, ["pr", "view", "u", "--json", "state"], timeout=5)
    assert run.calls == [(["gh", "pr", "view", "u", "--json", "state"], {"timeout": 5})]
    led = ghquota.ledger()
    assert led["by"]["p console"] == 1 and led["subs"]["p console"]["pr view"] == 1


def test_gh_uses_rate_limit_cost():
    ghquota.set_role("console")
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    out = json.dumps({"data": {"rateLimit": {"cost": 7, "remaining": 4000, "resetAt": reset, "limit": 5000}}})
    ghquota.gh(Runner(out), ["api", "graphql", "-f", "query=x"])
    led = ghquota.ledger()
    assert led["by"]["p console"] == 7 and led["remaining"] == 4000


@pytest.mark.parametrize("args", [["api", "repos/o/r/pulls"], ["auth", "status"], ["--version"],
                                  ["search", "prs", "--state", "open"], ["pr", "list", "--search", "x"]])
def test_gh_rest_and_no_call_record_nothing(args):
    before = ghquota.ledger()
    run = Runner()
    ghquota.gh(run, args)
    assert len(run.calls) == 1 and ghquota.ledger() == before


def test_gh_failure_still_counts_and_reraises():
    with pytest.raises(subprocess.TimeoutExpired):
        ghquota.gh(Runner(error=subprocess.TimeoutExpired("gh", 1)), ["pr", "merge", "u"])
    assert ghquota.ledger()["subs"]["p command"]["pr merge"] == 1
    failed = Runner(code=1)
    assert ghquota.gh(failed, ["pr", "view", "u"]).returncode == 1
    assert ghquota.ledger()["subs"]["p command"]["pr view"] == 1


def test_gh_never_writes_a_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "tok-secret-1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok-secret-2")
    ghquota.gh(Runner(), ["pr", "view", "u", "--header=Authorization: tok-secret-1"])
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files
    for p in files:
        text = p.read_text(errors="ignore")
        assert "tok-secret-1" not in text and "tok-secret-2" not in text


# ---------- the modules that run gh outside the tracker ----------

import re  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

from pl import commands, watch, worktrees  # noqa: E402
from pl.tui import prs  # noqa: E402

URL = "https://github.com/o/r/pull/7"


def _subs(role):
    return ghquota.ledger()["subs"].get(f"p {role}", {})


def test_prs_view_and_merge_reach_the_ledger(monkeypatch):
    ghquota.set_role("console")
    run = Runner("{}")
    monkeypatch.setattr(prs, "_run", run)
    monkeypatch.setattr(prs, "intent_text", lambda url, body: "")
    assert prs.fetch(URL)[0] == "ok"
    view = types.SimpleNamespace(
        _method={}, _cache={}, _ckey=lambda pr: pr["url"], run_worker=lambda fn, **kw: fn(),
        app=types.SimpleNamespace(call_from_thread=lambda fn, *a, **kw: None, notify=lambda *a, **kw: None, refresh_data=None),
        selected=lambda: None)
    prs.PrsView._merge(view, {"repo": "r", "number": 7, "url": URL}, "squash")
    assert _subs("console") == {"pr view": 1, "pr merge": 1}
    assert run.calls[1][0] == ["gh", "pr", "merge", URL, "--squash"]


def test_watch_calls_reach_the_ledger(monkeypatch):
    ghquota.set_role("dispatcher")
    run = Runner("[]")
    monkeypatch.setattr(watch.subprocess, "run", run)
    watch._gh(["pr", "list", "--json", "number"])
    assert _subs("dispatcher") == {"pr list": 1}
    before = ghquota.ledger()
    watch._gh(["search", "prs", "--state", "open"])
    assert run.calls[-1][0][:3] == ["gh", "search", "prs"] and ghquota.ledger() == before


def test_worktrees_pr_state_reaches_the_ledger(monkeypatch):
    ghquota.set_role("dispatcher")
    monkeypatch.setattr(worktrees, "_settled", lambda: {})
    monkeypatch.setattr(worktrees, "_run", Runner("{}"))
    worktrees._pr_info(URL)
    assert _subs("dispatcher") == {"pr view": 1}


def test_intent_reaches_the_ledger(monkeypatch):
    ghquota.set_role("pl intent")
    monkeypatch.setattr(commands.subprocess, "run", Runner('{"body": ""}'))
    monkeypatch.setattr(commands, "emit", lambda *a, **k: None)
    monkeypatch.setattr(commands, "cards", lambda: [])
    commands.cmd_intent(types.SimpleNamespace(pr=URL, json=True))
    assert _subs("pl intent") == {"pr view": 1}


def test_usage_github_lists_the_new_callers(monkeypatch):
    ghquota.set_role("console")
    monkeypatch.setattr(prs, "_run", Runner("{}"))
    monkeypatch.setattr(prs, "intent_text", lambda url, body: "")
    prs.fetch(URL)
    assert "p console 1" in ghquota.report()


ALLOWED = {"trackers/github.py", "ghquota.py", "ghwrap.py"}
GH_CALL = re.compile(r'\[\s*"gh"\s*,|github\._run\(')


def test_no_gh_call_outside_the_recording_path():
    root = Path(commands.__file__).parent
    hits = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if GH_CALL.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "gh called outside ghquota.gh:\n" + "\n".join(hits)


def _seed(acct):
    reset = int(time.time()) + 1800
    ghquota._write(ghquota._path(acct), {"limit": 5000, "remaining": 5000, "reset": reset, "at": time.time(),
                                         "window": reset, "spent": {}, "by": {}, "subs": {}})


def test_gh_records_against_the_calls_gh_folder(monkeypatch):
    monkeypatch.setattr(ghquota, "account", lambda folder=None: "other@github.com" if folder == "/tmp/x" else ACCT)
    _seed("other@github.com")
    before = ghquota.ledger()
    run = Runner()
    ghquota.gh(run, ["label", "list", "--repo", "o/r"], env={"GH_CONFIG_DIR": "/tmp/x"})
    assert run.calls[0][1] == {"env": {"GH_CONFIG_DIR": "/tmp/x"}}
    assert ghquota.ledger("other@github.com")["by"]["p command"] == 1
    assert ghquota.ledger() == before


def test_set_role_profile_names_the_spender():
    ghquota.set_role("pl setup", profile="sales")
    ghquota.gh(Runner(), ["pr", "view", "u"])
    assert ghquota.ledger()["by"]["sales pl setup"] == 1
    ghquota.set_role("command")
    assert ghquota.ROLE.get("profile") is None


def test_tracker_gh_keeps_the_trackers_errors(monkeypatch):
    from pl.trackers import github

    def missing(args):
        raise SystemExit("pl: gh is not installed: https://cli.github.com")
    monkeypatch.setattr(github, "_run", missing)
    with pytest.raises(SystemExit, match="gh is not installed"):
        ghquota.tracker_gh(["project", "list"])
    assert ghquota.ledger()["subs"]["p command"]["project list"] == 1


@pytest.mark.parametrize("verb", ["create", "edit", "delete"])
def test_label_writes_are_rest(verb):
    from pl import ghwrap
    assert ghwrap.kind(["label", verb, "x", "--repo", "o/r"]) == "rest"
    assert ghwrap.kind(["label", "list", "--repo", "o/r"]) == "graphql"


def test_brain_gh_reaches_the_ledger(monkeypatch):
    from pl import brain_tools
    ghquota.set_role("console")
    run = Runner("out")
    monkeypatch.setattr(brain_tools.subprocess, "run", run)
    assert brain_tools._gh(["gh", "pr", "view", "12"]) == "out"
    assert run.calls[0][0] == ["gh", "pr", "view", "12"]
    led = ghquota.ledger()
    assert led["by"]["p console"] == 1 and led["subs"]["p console"]["pr view"] == 1


def test_brain_gh_failure_raises_the_same_oserror_and_counts(monkeypatch):
    from pl import brain_tools
    monkeypatch.setattr(brain_tools.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom\n"))
    with pytest.raises(OSError, match="boom"):
        brain_tools._gh(["pr", "view", "12"])
    assert ghquota.ledger()["by"]["p command"] == 1


class Gh:
    def __init__(self, replies):
        self.calls, self.replies = [], replies

    def __call__(self, args):
        self.calls.append(args)
        out, code = self.replies.get(" ".join(args[:2]), ("", 0))
        return subprocess.CompletedProcess(["gh", *args], code, out, "")


PROJECTS = json.dumps({"projects": [{"owner": {"login": "me"}, "number": 1, "title": "T"}]})


def test_new_pipeline_project_lists_reach_the_ledger(monkeypatch):
    from pl.trackers import github
    from pl.tui import newpipeline
    fake = Gh({"project list": (PROJECTS, 0), "api user/orgs": ("acme\n", 0)})
    monkeypatch.setattr(github, "_run", fake)
    rows, why = newpipeline.gh_projects()
    assert why is None and len(rows) == 2
    led = ghquota.ledger()
    assert led["subs"]["p command"] == {"project list": 2}


def test_new_pipeline_not_signed_in_records_nothing(monkeypatch):
    from pl.trackers import github
    from pl.tui import newpipeline
    monkeypatch.setattr(github, "_run", Gh({"auth status": ("", 1)}))
    before = ghquota.ledger()
    assert newpipeline.gh_projects() == (None, "gh is not signed in: run gh auth login")
    assert ghquota.ledger() == before


def test_check_board_repo_view_reaches_the_ledger(monkeypatch):
    from pl.trackers import github
    from pl.tui import newpipeline
    monkeypatch.setattr(github, "_run", Gh({}))
    monkeypatch.setattr(github, "stage_field", lambda *a: {"options": [{"name": "Todo"}]})
    tracker = {"owner": "o", "number": 1, "repo": "o/r"}
    assert newpipeline._check_github(tracker, ["Todo"]) is None
    assert "repo view" in ghquota.ledger()["subs"]["p command"]


class Setup:
    def __init__(self, replies=None):
        self.calls, self.replies = [], replies or {}

    def __call__(self, argv, env=None, **kw):
        self.calls.append((argv, env))
        return self.replies.get(" ".join(argv[:3]), (0, ""))


def _label_answers(gh_dir):
    return {"gh_config_dir": gh_dir, "gh_ok": True, "todo": [], "labels": {"ready": "pl:ready", "review": "pl:review"},
            "tracker": {"type": "local"}}


def _flags():
    return types.SimpleNamespace(create_labels=True, interactive=False)


def test_setup_label_list_and_create_reach_the_folders_ledger(monkeypatch):
    from pl import setup
    monkeypatch.setattr(ghquota, "account", lambda folder=None: "other@github.com" if folder == "/tmp/x" else ACCT)
    _seed("other@github.com")
    before = ghquota.ledger()
    fake = Setup({"gh label list": (0, "[]")})
    monkeypatch.setattr(setup, "_run", fake)
    ghquota.set_role("pl setup", profile="sales")
    setup.create_labels(_label_answers("/tmp/x"), _flags(), "o/r")
    led = ghquota.ledger("other@github.com")
    assert led["by"] == {"sales pl setup": 1} and led["subs"]["sales pl setup"] == {"label list": 1}
    assert [a[:3] for a, _ in fake.calls].count(["gh", "label", "create"]) == 2
    assert all(e["GH_CONFIG_DIR"] == "/tmp/x" for _, e in fake.calls)
    assert ghquota.ledger() == before


def test_setup_auth_status_records_nothing(monkeypatch):
    from pl import setup
    monkeypatch.setattr(setup, "_run", Setup({"gh auth status": (0, "Logged in as me")}))
    before = ghquota.ledger()
    setup.ask_gh({"todo": []}, types.SimpleNamespace(gh_config_dir="", interactive=False, yes=True))
    assert ghquota.ledger() == before


def test_setup_label_list_failure_keeps_the_todo_and_counts(monkeypatch):
    from pl import setup
    monkeypatch.setattr(setup, "_run", lambda argv, **kw: None)
    answers = _label_answers(None)
    setup.create_labels(answers, _flags(), "o/r")
    assert answers["todo"][0].startswith("pl could not list the labels")
    assert ghquota.ledger()["subs"]["p command"]["label list"] == 1


def test_no_token_reaches_the_ledger_from_these_modules(monkeypatch, tmp_path):
    from pl import brain_tools, setup
    from pl.trackers import github
    from pl.tui import newpipeline
    monkeypatch.setenv("GH_TOKEN", "tok-secret-1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok-secret-2")
    monkeypatch.setattr(brain_tools.subprocess, "run", Runner("{}"))
    monkeypatch.setattr(github, "_run", Gh({"project list": (PROJECTS, 0)}))
    monkeypatch.setattr(setup, "_run", Setup({"gh label list": (0, "[]")}))
    brain_tools._gh(["pr", "view", "1"])
    newpipeline.gh_projects()
    setup.create_labels(_label_answers(None), _flags(), "o/r")
    for f in (p for p in tmp_path.rglob("*") if p.is_file()):
        assert b"tok-secret" not in f.read_bytes()
