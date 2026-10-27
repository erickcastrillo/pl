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
