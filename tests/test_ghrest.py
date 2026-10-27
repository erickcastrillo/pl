import json
import subprocess
import time

import pytest

from pl import config as C
from pl import ghquota, ghrest, ghwrap
from pl.trackers import github

pytestmark = pytest.mark.usefixtures("profile")

ACCT = "me@github.com"


@pytest.fixture
def profile(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "PROFILE_NAME", "p")
    monkeypatch.setattr(C, "load", lambda *a, **k: None)
    monkeypatch.setattr(ghquota, "_clock", lambda: time.time())
    for k in ("PL_STAGE", "PL_LOOP", "PL_CARD", "PL_GH_LEDGER", "GH_HOST"):
        monkeypatch.delenv(k, raising=False)
    return state


def _seed(remaining):
    reset = int(time.time()) + 1800
    ghquota._write(ghquota._path(ACCT), {"limit": 5000, "remaining": remaining, "reset": reset, "at": time.time(),
                                         "window": reset, "spent": {}, "by": {}, "subs": {}})
    return reset


def _item(number=12, merged=True, state="closed", **extra):
    return {"number": number, "title": f"t{number}", "body": "b", "state": state, "html_url": f"https://github.com/o/r/pull/{number}",
            "draft": False, "head": {"ref": "feat", "sha": "abc"}, "base": {"ref": "main"}, "user": {"login": "a"},
            "labels": [{"name": "bug", "id": 1, "color": "f00"}], "assignees": [{"login": "z"}],
            "created_at": "c", "updated_at": "u", "closed_at": "x" if state == "closed" else None,
            "merged_at": "m" if merged else None, **extra}


class Rest:
    def __init__(self, monkeypatch, payload=None, code=0):
        self.calls, self.payload, self.code = [], payload, code
        monkeypatch.setattr(ghrest, "_run", self.run)

    def run(self, argv, env):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, json.dumps(self.payload) if self.code == 0 else "", "gh: Not Found (HTTP 404)" if self.code else "")


@pytest.fixture
def rest(monkeypatch):
    return Rest(monkeypatch, _item())


# ---------- WP1 ----------

def test_view_maps_fields_and_merged_state(rest):
    out = ghrest.answer("gh", ["pr", "view", "12", "--json", "number,title,state,url,headRefName,baseRefName,isDraft,author,labels"], {})
    assert rest.calls == [["gh", "api", "repos/{owner}/{repo}/pulls/12"]]
    data = json.loads(out)
    assert list(data) == ["number", "title", "state", "url", "headRefName", "baseRefName", "isDraft", "author", "labels"]
    assert data["state"] == "MERGED" and data["author"] == {"login": "a"} and data["labels"] == [{"name": "bug"}]
    assert data["url"] == "https://github.com/o/r/pull/12" and data["isDraft"] is False


def test_open_state_and_null_times(monkeypatch):
    Rest(monkeypatch, _item(merged=False, state="open"))
    data = json.loads(ghrest.answer("gh", ["pr", "view", "1", "--json=state,mergedAt,closedAt,assignees"], {}))
    assert data == {"state": "OPEN", "mergedAt": None, "closedAt": None, "assignees": [{"login": "z"}]}


def test_view_pr_url_and_repo_flag(rest):
    ghrest.answer("gh", ["pr", "view", "https://github.com/o/r/pull/12", "--json", "number"], {})
    ghrest.answer("gh", ["pr", "view", "12", "-R", "o/r", "--json", "number"], {})
    assert [c[2] for c in rest.calls] == ["repos/o/r/pulls/12"] * 2


def test_list_merged_and_limit(monkeypatch):
    items = [_item(1), _item(2, merged=False), _item(3), _item(4)]
    rest = Rest(monkeypatch, items)
    data = json.loads(ghrest.answer("gh", ["pr", "list", "--json", "number,state", "--state", "merged", "--limit", "2", "-R", "o/r"], {}))
    assert "state=closed&per_page=100" in rest.calls[0][2]
    assert data == [{"number": 1, "state": "MERGED"}, {"number": 3, "state": "MERGED"}]


def test_list_default_limit_and_head(rest):
    ghrest.answer("gh", ["pr", "list", "--json", "number", "--head", "feat", "--base", "main"], {})
    endpoint = rest.calls[0][2]
    assert "per_page=30" in endpoint and "head={owner}:feat" in endpoint and "base=main" in endpoint and "state=open" in endpoint


@pytest.mark.parametrize("args", [
    ["pr", "view", "1", "--json", "statusCheckRollup"],
    ["pr", "view", "1", "--json", "reviewDecision"],
    ["pr", "view", "1", "--json", "number", "--jq", ".x"],
    ["pr", "view", "1", "--json", "number", "--template", "x"],
    ["pr", "list", "--json", "number", "--search", "x"],
    ["pr", "list", "--json", "number", "--label", "x"],
    ["pr", "list", "--json", "number", "--author", "x"],
    ["pr", "view", "1", "--json", "number", "--web"],
    ["pr", "view", "my-branch", "--json", "number"],
    ["pr", "view", "--json", "number"],
    ["pr", "view", "1"],
    ["pr", "view", "1", "--json", "number", "-R", "../x"],
    ["pr", "list", "--json", "number", "--limit", "500"],
    ["pr", "view", "https://evil.example/o/r/pull/1", "--json", "number"],
])
def test_unhandled_calls_return_none(rest, args):
    assert ghrest.answer("gh", args, {}) is None
    assert rest.calls == []


def test_rest_failure_returns_none(monkeypatch):
    Rest(monkeypatch, code=1)
    assert ghrest.answer("gh", ["pr", "view", "1", "--json", "number"], {}) is None


def test_below_reserve():
    now = time.time()
    assert ghrest.below_reserve({"remaining": 100, "reset": now + 60, "limit": 5000}, now)
    assert not ghrest.below_reserve({"remaining": 250, "reset": now + 60, "limit": 5000}, now)
    assert not ghrest.below_reserve({"remaining": 100, "reset": now - 1, "limit": 5000}, now)
    assert not ghrest.below_reserve({}, now)


# ---------- WP2 ----------

class Real:
    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(ghwrap, "_run", self.run)
        monkeypatch.setattr(ghwrap.shutil, "which", lambda name: "/real/gh")
        monkeypatch.setattr(github, "_run", lambda args: (_ for _ in ()).throw(SystemExit("gh gone")))

    def run(self, argv, env):
        self.calls.append(argv)
        print("real gh output")
        return 0


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setenv("PL_GH_LEDGER", ACCT)
    monkeypatch.setenv("PL_STAGE", "run")
    monkeypatch.setenv("PL_CARD", "pl#5")
    return Real(monkeypatch)


ARGS = ["pr", "view", "12", "--json", "number,state"]


def test_wrapper_answers_through_rest_below_reserve(real, rest, capsys):
    _seed(100)
    assert ghwrap.main(ARGS) == 0
    out, err = capsys.readouterr()
    assert real.calls == [] and err == ""
    assert json.loads(out) == {"number": 12, "state": "MERGED"}
    led = ghquota.ledger(ACCT)
    caller = "p agent run pl#5"
    assert led["by"].get(caller, 0) == 0
    assert led["subs"][caller]["pr view"] == 0
    assert led["rest"][caller]["pr view"] == 1


def test_wrapper_unsupported_field_falls_through(real, rest, capsys):
    _seed(100)
    args = ["pr", "view", "12", "--json", "statusCheckRollup"]
    assert ghwrap.main(args) == 0
    assert rest.calls == [] and real.calls == [["/real/gh", *args]]


def test_wrapper_rest_failure_runs_original_call(real, monkeypatch, capsys):
    rest = Rest(monkeypatch, code=1)
    _seed(100)
    assert ghwrap.main(ARGS) == 0
    assert len(rest.calls) == 1 and real.calls == [["/real/gh", *ARGS]]
    assert capsys.readouterr().out == "real gh output\n"


def test_wrapper_passes_through_above_reserve(real, rest):
    _seed(4000)
    assert ghwrap.main(ARGS) == 0
    assert rest.calls == [] and real.calls == [["/real/gh", *ARGS]]


def test_no_token_in_output(real, rest, monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "tok-secret-1")
    _seed(100)
    ghwrap.main(ARGS)
    out, err = capsys.readouterr()
    assert "tok-secret-1" not in out + err + ghquota._path(ACCT).read_text()
