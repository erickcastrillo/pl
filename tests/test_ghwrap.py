import os
import shlex
import subprocess
import time

import pytest

from pl import config as C
from pl import ghquota, ghwrap, harnesses
from pl.trackers import github

pytestmark = pytest.mark.usefixtures("profile")


@pytest.fixture
def profile(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "PROFILE_NAME", "p")
    monkeypatch.setattr(C, "load", lambda *a, **k: None)
    monkeypatch.setattr(ghquota, "_clock", lambda: time.time())
    for k in ("PL_STAGE", "PL_LOOP", "PL_CARD", "PL_GH_LEDGER"):
        monkeypatch.delenv(k, raising=False)
    return state


def _seed(remaining, reset=None, acct="me@github.com"):
    reset = reset or int(time.time()) + 1800
    ghquota._write(ghquota._path(acct), {"limit": 5000, "remaining": remaining, "reset": reset, "at": time.time(),
                                         "window": reset, "spent": {}, "by": {}, "subs": {}})
    return reset


def _reading(remaining, reset):
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))
    return subprocess.CompletedProcess([], 0, f"{remaining}\n{iso}\n", "")


class Gh:
    """A fake gh: the wrapper's runner and pl's own reading runner."""

    def __init__(self, monkeypatch, code=0, reading=None):
        self.calls, self.reads, self.envs, self.reading, self.code = [], [], [], reading, code
        monkeypatch.setattr(ghwrap, "_run", self.run)
        monkeypatch.setattr(ghwrap.shutil, "which", lambda name: "/real/gh")
        monkeypatch.setattr(github, "_run", self.read)

    def run(self, argv, env):
        self.calls.append(argv)
        self.envs.append(env)
        return self.code

    def read(self, args):
        self.reads.append(args)
        if self.reading is None:
            raise SystemExit("gh gone")
        return self.reading


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setenv("PL_GH_LEDGER", "me@github.com")
    return Gh(monkeypatch)


# ---------- WP1: ghquota.record_agent ----------

def test_record_agent_counts_by_caller_and_subcommand():
    reset = int(time.time()) + 1800
    _seed(4000, reset)
    ghquota.record_agent("me@github.com", "p agent run pl#5", "pr view", 10, 3990, reset)
    ghquota.record_agent("me@github.com", "p agent run pl#5", "pr view", 5, 3985, reset)
    led = ghquota.ledger("me@github.com")
    assert led["by"]["p agent run pl#5"] == 15 and led["subs"]["p agent run pl#5"]["pr view"] == 15
    assert led["spent"] == {"p": 15} and led["remaining"] == 3985


def test_record_agent_new_window_starts_over():
    reset = int(time.time()) + 1800
    _seed(4000, reset)
    ghquota.record_agent("me@github.com", "p agent run pl#5", "pr view", 10, 3990, reset)
    ghquota.record_agent("me@github.com", "p agent run pl#6", "pr list", 3, 4997, reset + 3600)
    led = ghquota.ledger("me@github.com")
    assert led["by"] == {"p agent run pl#6": 3} and led["subs"] == {"p agent run pl#6": {"pr list": 3}}


# ---------- WP2: the wrapper ----------

def test_passes_argv_and_exit_code(gh):
    gh.code = 3
    assert ghwrap.main(["pr", "view", "5", "--json", "title"]) == 3
    assert gh.calls == [["/real/gh", "pr", "view", "5", "--json", "title"]]


def test_records_cost_by_stage_and_card(gh, monkeypatch):
    monkeypatch.setenv("PL_STAGE", "run")
    monkeypatch.setenv("PL_CARD", "pl#5")
    reset = _seed(4000)
    gh.reading = _reading(3990, reset)
    ghwrap.main(["pr", "view", "5"])
    led = ghquota.ledger("me@github.com")
    assert led["by"]["p agent run pl#5"] == 10 and led["subs"]["p agent run pl#5"]["pr view"] == 10
    assert led["remaining"] == 3990


def test_loop_and_no_card_are_named(gh, monkeypatch):
    monkeypatch.setenv("PL_LOOP", "review")
    reset = _seed(4000)
    gh.reading = _reading(3999, reset)
    ghwrap.main(["pr", "list"])
    assert ghquota.ledger("me@github.com")["by"] == {"p agent review -": 1}


def test_estimate_when_reading_fails(gh):
    _seed(4000)
    ghwrap.main(["api", "graphql", "-f", "query=x"])
    led = ghquota.ledger("me@github.com")
    assert led["by"] == {"p agent - -": 1} and led["subs"]["p agent - -"] == {"api graphql": 1}


def test_estimate_without_a_reading_of_this_window_records_nothing(gh):
    ghwrap.main(["pr", "view"])
    assert ghquota.ledger("me@github.com") == {}


@pytest.mark.parametrize("args", [["api", "repos/o/r/pulls"], ["--version"], ["auth", "status"], []])
def test_rest_and_no_call_record_nothing(gh, args):
    reset = _seed(4000)
    gh.reading = _reading(10, reset)
    before = ghquota.ledger("me@github.com")
    ghwrap.main(args)
    assert gh.reads == [] and ghquota.ledger("me@github.com") == before


def test_kind():
    assert ghwrap.kind(["api", "-X", "GET", "repos/o/r"]) == "rest"
    assert ghwrap.kind(["api", "graphql"]) == "graphql"
    assert ghwrap.kind(["pr", "create"]) == "graphql"


def test_wrapper_not_on_child_path(gh, monkeypatch, profile):
    shim = profile / "bin"
    monkeypatch.setenv("PATH", f"{shim}:/usr/bin")
    ghwrap.main(["pr", "view"])
    assert str(shim) not in gh.envs[0]["PATH"].split(":") and "/usr/bin" in gh.envs[0]["PATH"].split(":")
    assert str(shim) not in os.environ["PATH"].split(":")


def test_unloadable_profile_still_runs_gh(gh, monkeypatch, profile):
    def broken(*a, **k):
        raise SystemExit("pl: no profile")
    monkeypatch.setattr(C, "load", broken)
    shim = profile / "bin"
    monkeypatch.setenv("PL_GH_SHIM_DIR", str(shim))
    monkeypatch.setenv("PL_GH_LEDGER", "me@github.com")
    monkeypatch.setenv("PATH", f"{shim}:/usr/bin")
    gh.code = 3
    assert ghwrap.main(["pr", "view"]) == 3
    assert gh.calls == [["/real/gh", "pr", "view"]]
    assert str(shim) not in gh.envs[0]["PATH"].split(":")
    assert gh.reads == []


def test_no_token_read_or_written(gh, monkeypatch, capsys, profile):
    monkeypatch.setenv("GH_TOKEN", "tok-secret-1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok-secret-2")
    reset = _seed(4000)
    gh.reading = _reading(3990, reset)
    ghwrap.main(["pr", "view"])
    assert all(a[:2] != ["auth", "token"] for a in gh.reads) and all("token" not in c for c in gh.calls)
    seen = capsys.readouterr()
    files = [p for root in (profile, ghquota._dir()) for p in root.rglob("*") if p.is_file()]
    for secret in ("tok-secret-1", "tok-secret-2"):
        assert secret not in seen.out + seen.err
        assert all(secret not in p.read_text() for p in files)


def test_without_a_ledger_account_nothing_is_recorded(gh, monkeypatch):
    monkeypatch.delenv("PL_GH_LEDGER")
    ghwrap.main(["pr", "view"])
    assert gh.reads == []


def test_recording_failure_never_reaches_the_agent(gh, monkeypatch):
    gh.code = 4

    def boom(*a):
        raise RuntimeError("ledger broke")
    monkeypatch.setattr(ghquota, "record_agent", boom)
    _seed(4000)
    assert ghwrap.main(["pr", "view"]) == 4


def test_missing_gh_exits_127(monkeypatch, capsys):
    monkeypatch.setattr(ghwrap.shutil, "which", lambda name: None)
    assert ghwrap.main(["pr", "view"]) == 127
    assert "gh is not installed" in capsys.readouterr().err


# ---------- WP3: the wrapper first on agents' PATH ----------

@pytest.fixture
def launch(monkeypatch):
    monkeypatch.setattr(ghquota, "account", lambda folder=None: "me.abc123def456@github.com")
    monkeypatch.setattr(C, "PROFILES", {"a": C.STATE_DIR / "acct"})
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude"}}, raising=False)

    def script(**kw):
        return harnesses.launch_script(harnesses.get("claude"), "a", "go", "sid", kw.pop("label", "lbl"),
                                       worktrees=False, plugin=False, **kw)
    return script


def test_launch_script_puts_wrapper_first(launch):
    lines = launch(stage="run", card_id="pl#5").splitlines()
    shim = C.STATE_DIR / "bin"
    assert f"export PATH={shlex.quote(str(shim))}:\"$PATH\"" in lines
    assert "export PL_CARD=pl#5" in lines or "export PL_CARD='pl#5'" in lines
    assert "export PL_GH_LEDGER=me.abc123def456@github.com" in lines
    assert not any(line.startswith("export PL_LOOP") for line in lines)
    gh_file = shim / "gh"
    assert gh_file.read_text().splitlines()[0] == "#!/bin/sh" and "-m pl.ghwrap" in gh_file.read_text()
    assert gh_file.stat().st_mode & 0o777 == 0o700 and shim.stat().st_mode & 0o777 == 0o700
    assert lines[-1].startswith("exec ")


def test_loop_launch_names_the_loop(launch):
    lines = launch(label="review-loop").splitlines()
    assert "export PL_LOOP=review-loop" in lines


def test_card_text_is_quoted(launch):
    lines = launch(stage="run", card_id="x;$(touch /tmp/pwned)").splitlines()
    card = next(line for line in lines if line.startswith("export PL_CARD="))
    assert card == "export PL_CARD=" + shlex.quote("x;$(touch /tmp/pwned)")
    assert shlex.split(card)[1] == "PL_CARD=x;$(touch /tmp/pwned)"


def test_headless_env_has_wrapper(monkeypatch):
    monkeypatch.setattr(ghquota, "account", lambda folder=None: "me.abc123def456@github.com")
    monkeypatch.setattr(C, "PROFILES", {"a": C.STATE_DIR / "acct"})
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude"}}, raising=False)
    _, env = harnesses.headless_argv(harnesses.get("claude"), "a", "go", stage="run")
    assert env["PATH"].split(":")[0] == str(C.STATE_DIR / "bin")
    assert env["PL_GH_LEDGER"] == "me.abc123def456@github.com" and env["PL_STAGE"] == "run"


def test_shim_falls_back_to_next_gh_when_pl_is_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(harnesses.sys, "executable", str(tmp_path / "no-such-python"))
    shim = harnesses.gh_shim()
    other = tmp_path / "other"
    other.mkdir()
    real = other / "gh"
    real.write_text('#!/bin/sh\necho "real gh $*"\nexit 7\n')
    real.chmod(0o700)
    env = {"PATH": f"{shim}:{other}:/usr/bin:/bin"}
    out = subprocess.run([str(shim / "gh"), "pr", "list"], env=env, capture_output=True, text=True)
    assert (out.returncode, out.stdout) == (7, "real gh pr list\n")
