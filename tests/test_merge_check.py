"""The merge checks: a throwaway merge, the test command and the CI checks. Git, the test command and gh are all faked."""
import os
import subprocess
import types

import pytest

from pl import config as C
from pl import merge_check as mc

PR = {"number": 7, "statusCheckRollup": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(C, "WORK_DIR", tmp_path / "repo")
    monkeypatch.setattr(C, "AUTO_MERGE", {"test_command": "uv run pytest -q"})
    ns = types.SimpleNamespace(git=[], popen=[], killed=[], unmerged="", fail={}, out="ok\n", rc=0, timeout=False,
                               path=tmp_path / "state" / "merge-check" / "pr-7")

    def fake_git(argv, cwd):
        ns.git.append((argv, cwd))
        key = " ".join(argv[:2]) if argv[0] != "-c" else "merge"
        if key in ns.fail:
            if isinstance(ns.fail[key], Exception):
                raise ns.fail[key]
            return 1, ns.fail[key]
        if key == "diff --name-only":
            return 0, ns.unmerged
        return 0, ""

    class Proc:
        pid = 4242

        def __init__(self, argv, **kw):
            ns.popen.append((argv, kw))
            self.returncode = ns.rc

        def communicate(self, timeout=None):
            if ns.timeout:
                raise subprocess.TimeoutExpired("t", timeout)
            return ns.out, None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mc, "_git", fake_git)
    monkeypatch.setattr(mc, "_popen", Proc)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: ns.killed.append((pid, sig)))
    ns.rules = lambda pr=PR: {r["rule"]: r for r in mc.check(pr, "main")}
    ns.merge_fails = lambda paths="a.py\n": (ns.fail.update({"merge": "CONFLICT"}), setattr(ns, "unmerged", paths))
    return ns


def _checks(*pairs):
    return [{"name": n, "conclusion": s} for n, s in pairs]


def test_default_branch_asks_gh_once(monkeypatch):
    monkeypatch.setattr(mc.github, "_gh", lambda a: "main\n" if a[:2] == ["repo", "view"] else 1 / 0)
    assert mc.default_branch("acme/app") == "main"


def test_clean_merge_conflicts_ok_names_base(env):
    r = env.rules()["conflicts"]
    assert r["ok"] is True and "main" in r["why"]


def test_conflict_fails_with_three_paths_and_skips_tests(env):
    env.merge_fails("a.py\nb.py\nc.py\nd.py\n")
    r = env.rules()
    assert r["conflicts"] == {"rule": "conflicts", "ok": False, "why": "conflicts with main: a.py, b.py, c.py +1 more"}
    assert r["tests"]["ok"] is None and r["tests"]["why"] == "not run: conflicts"
    assert env.popen == []


def test_fetch_failure_runs_nothing_else(env):
    env.fail["fetch --no-tags"] = "fatal: no remote\nfatal: unreachable"
    r = env.rules()
    assert r["conflicts"]["ok"] is None and r["conflicts"]["why"] == "not run: git fetch failed: fatal: unreachable"
    assert r["tests"]["ok"] is None
    argvs = [a for a, _ in env.git]
    assert argvs[0][0] == "fetch" and not any("merge" in a and a[0] == "-c" or a[:2] == ["worktree", "add"] for a in argvs)


@pytest.mark.parametrize("mode", ["clean", "conflict", "raises"])
def test_cleanup_always_runs(env, mode):
    if mode == "conflict":
        env.merge_fails()
    if mode == "raises":
        env.fail["merge"] = RuntimeError("boom")
    if mode == "raises":
        with pytest.raises(RuntimeError):
            mc.check(PR, "main")
    else:
        mc.check(PR, "main")
    argvs = [a for a, _ in env.git]
    assert argvs[-4:] == [["worktree", "remove", "--force", str(env.path)], ["worktree", "prune"],
                          ["update-ref", "-d", "refs/pl-merge/7/base"], ["update-ref", "-d", "refs/pl-merge/7/head"]]
    assert not [a for a in argvs if a[0] in ("push", "checkout", "branch", "switch")]
    assert {cwd for _, cwd in env.git} <= {C.WORK_DIR, env.path}


def test_worktree_lives_under_state_dir(env):
    env.rules()
    add = next(a for a, _ in env.git if a[:2] == ["worktree", "add"])
    assert add == ["worktree", "add", "--detach", str(env.path), "refs/pl-merge/7/base"]
    assert str(env.path).startswith(str(C.STATE_DIR))


def test_leftover_worktree_is_removed_first(env):
    env.path.mkdir(parents=True)
    env.rules()
    assert env.git[0][0][:2] == ["worktree", "remove"]


def test_test_command_runs_in_the_merge_worktree(env):
    env.rules()
    argv, kw = env.popen[0]
    assert argv == ["uv", "run", "pytest", "-q"] and kw["cwd"] == env.path and "shell" not in kw


def test_tests_pass_with_green_ci(env):
    pr = {**PR, "statusCheckRollup": _checks(("a", "SUCCESS"), ("b", "SUCCESS"), ("c", "SKIPPED"))}
    assert env.rules(pr)["tests"] == {"rule": "tests", "ok": True, "why": "local tests passed; CI: 3 passed"}


def test_tests_pass_with_no_ci(env):
    assert env.rules()["tests"]["why"] == "local tests passed; CI: none"


def test_pending_ci_does_not_block(env):
    pr = {**PR, "statusCheckRollup": _checks(("a", "SUCCESS"), ("b", "")) }
    r = env.rules(pr)["tests"]
    assert r["ok"] is True and r["why"] == "local tests passed; CI: 1 passed, 1 pending"


def test_failing_tests_give_exit_code_and_last_line(env):
    env.rc, env.out = 2, "collecting\n1 failed\n\n"
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == "tests failed (exit 2): 1 failed"


def test_timeout_stops_and_fails(env):
    env.timeout = True
    C.AUTO_MERGE["test_timeout"] = 1
    r = env.rules()["tests"]
    assert r == {"rule": "tests", "ok": False, "why": "timed out after 1 minutes"}
    assert env.killed and env.killed[0][0] == 4242
    assert env.popen[0][1]["start_new_session"] is True


def test_no_test_command_fails_and_runs_nothing(env):
    C.AUTO_MERGE.pop("test_command")
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == "no test_command configured" and env.popen == []


def test_failing_ci_check_fails_and_is_named(env):
    pr = {**PR, "statusCheckRollup": _checks(("test", "SUCCESS"), ("lint", "FAILURE"))}
    r = env.rules(pr)["tests"]
    assert r["ok"] is False and r["why"] == "local tests passed; CI check failed: lint"


def test_commit_status_uses_context_name(env):
    pr = {**PR, "statusCheckRollup": [{"context": "ci/build", "state": "FAILURE"}]}
    assert "ci/build" in env.rules(pr)["tests"]["why"]


def test_ignored_failing_check_stays_ok(env):
    C.AUTO_MERGE["ignore_checks"] = ["lint"]
    pr = {**PR, "statusCheckRollup": _checks(("test", "SUCCESS"), ("lint", "FAILURE"))}
    r = env.rules(pr)["tests"]
    assert r["ok"] is True and r["why"] == "local tests passed; CI: 1 passed, ignored failing: lint"
