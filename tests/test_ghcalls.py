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
PENDING = {"brain_tools.py", "setup.py", "tui/newpipeline.py"}   # slice 8 (pl#576) empties this
GH_CALL = re.compile(r'\[\s*"gh"\s*,|github\._run\(')


def test_no_gh_call_outside_the_recording_path():
    root = Path(commands.__file__).parent
    hits = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED or rel in PENDING:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if GH_CALL.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "gh called outside ghquota.gh:\n" + "\n".join(hits)
