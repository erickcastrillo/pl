"""The merge checks: a throwaway merge, the test command and the CI checks. Git, the test command and gh are all faked."""
import os
import shlex
import subprocess
import types

import pytest

from pl import config as C
from pl import merge_check as mc

PR = {"number": 7, "statusCheckRollup": []}
FULL_SEL = {"full": True, "tests": [], "reason": "full suite: pyproject.toml changed", "e2e_ui": False}
FULL_LABEL = "(full suite: pyproject.toml changed)"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(C, "WORK_DIR", tmp_path / "repo")
    monkeypatch.setattr(C, "AUTO_MERGE", {"test_command": "uv run pytest -q"})
    ns = types.SimpleNamespace(git=[], popen=[], killed=[], unmerged="", fail={}, out="ok\n", rc=0, timeout=False,
                               select=[], select_error=None, sel=dict(FULL_SEL),
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

    def fake_select(root, base, labels=()):
        ns.select.append((root, base, list(labels)))
        if ns.select_error:
            raise ns.select_error
        return ns.sel

    monkeypatch.setattr(mc.tests_affected, "select", fake_select)
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


def _pr_worktree_has(env, monkeypatch, tests_toml):
    """Faked `git worktree add` also writes .pl/tests.toml into the pull request's worktree."""
    fake = mc._git

    def git(argv, cwd):
        if argv[:2] == ["worktree", "add"]:
            (env.path / ".pl").mkdir(parents=True, exist_ok=True)
            (env.path / ".pl" / "tests.toml").write_text(tests_toml)
        return fake(argv, cwd)

    monkeypatch.setattr(mc, "_git", git)


def test_tests_rule_adds_workers_from_the_pr_worktree(env, monkeypatch):
    _pr_worktree_has(env, monkeypatch, '[tests]\nworkers = "auto"\n')
    env.rules()
    assert env.popen[0][0] == ["uv", "run", "pytest", "-q", "-n", "auto"]


def test_tests_rule_command_unchanged_without_workers(env):
    env.rules()
    assert env.popen[0][0] == ["uv", "run", "pytest", "-q"]


@pytest.mark.parametrize("cmd", ["make test", "uv run pytest -q -n 2"])
def test_tests_rule_runs_non_pytest_and_n_commands_as_configured(env, monkeypatch, cmd):
    C.AUTO_MERGE["test_command"] = cmd
    _pr_worktree_has(env, monkeypatch, '[tests]\nworkers = "auto"\n')
    r = env.rules()["tests"]
    assert env.popen[0][0] == shlex.split(cmd)
    if cmd == "make test":
        assert env.select == [] and "full suite: test_command is not pytest" in r["why"]


def test_tests_rule_bad_workers_fails_and_runs_nothing(env, monkeypatch):
    _pr_worktree_has(env, monkeypatch, '[tests]\nworkers = "many"\n')
    r = env.rules()["tests"]
    assert r["ok"] is False and "tests.toml" in r["why"] and "workers" in r["why"] and env.popen == []


def test_tests_pass_with_green_ci(env):
    pr = {**PR, "statusCheckRollup": _checks(("a", "SUCCESS"), ("b", "SUCCESS"), ("c", "SKIPPED"))}
    assert env.rules(pr)["tests"] == {"rule": "tests", "ok": True, "why": f"local tests passed {FULL_LABEL}; CI: 3 passed"}


def test_no_ci_is_not_ok_by_default(env):
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == f"local tests passed {FULL_LABEL}; CI: none, and require_ci is on"


def test_no_ci_is_ok_when_require_ci_is_off(env, monkeypatch):
    monkeypatch.setattr(C, "AUTO_MERGE", {"test_command": "x", "require_ci": False})
    assert env.rules()["tests"] == {"rule": "tests", "ok": True,
                                    "why": "local tests passed (full suite: test_command is not pytest); CI: none"}


def test_pending_ci_is_not_decided(env):
    pr = {**PR, "statusCheckRollup": _checks(("a", "SUCCESS"), ("b", ""))}
    r = env.rules(pr)["tests"]
    assert r["ok"] is None and r["why"] == f"local tests passed {FULL_LABEL}; CI pending (1 of 2), not decided"


def test_failing_ci_beats_pending(env):
    pr = {**PR, "statusCheckRollup": _checks(("a", "FAILURE"), ("b", ""))}
    assert env.rules(pr)["tests"]["ok"] is False


def test_test_command_gets_only_an_allow_listed_environment(env, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_TOOLCHAIN", "yes")
    monkeypatch.setattr(C, "AUTO_MERGE", {"test_command": "x", "test_env": ["MY_TOOLCHAIN"]})
    env.rules()
    e = env.popen[0][1]["env"]
    assert "GH_TOKEN" not in e and e["PATH"] == "/usr/bin" and e["MY_TOOLCHAIN"] == "yes"
    assert e["HOME"] != os.environ["HOME"] and e["HOME"].startswith(str(env.path.parent))


def test_failing_tests_give_exit_code_and_last_line(env):
    env.rc, env.out = 2, "collecting\n1 failed\n\n"
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == f"tests failed (exit 2): 1 failed {FULL_LABEL}"


def test_timeout_stops_and_fails(env):
    env.timeout = True
    C.AUTO_MERGE["test_timeout"] = 1
    r = env.rules()["tests"]
    assert r == {"rule": "tests", "ok": False, "why": f"timed out after 1 minutes {FULL_LABEL}"}
    assert env.killed and env.killed[0][0] == 4242
    assert env.popen[0][1]["start_new_session"] is True


def test_no_test_command_fails_and_runs_nothing(env):
    C.AUTO_MERGE.pop("test_command")
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == "no test_command configured" and env.popen == []


def test_failing_ci_check_fails_and_is_named(env):
    pr = {**PR, "statusCheckRollup": _checks(("test", "SUCCESS"), ("lint", "FAILURE"))}
    r = env.rules(pr)["tests"]
    assert r["ok"] is False and r["why"] == f"local tests passed {FULL_LABEL}; CI check failed: lint"


def test_commit_status_uses_context_name(env):
    pr = {**PR, "statusCheckRollup": [{"context": "ci/build", "state": "FAILURE"}]}
    assert "ci/build" in env.rules(pr)["tests"]["why"]


def test_ignored_failing_check_stays_ok(env):
    C.AUTO_MERGE["ignore_checks"] = ["lint"]
    pr = {**PR, "statusCheckRollup": _checks(("test", "SUCCESS"), ("lint", "FAILURE"))}
    r = env.rules(pr)["tests"]
    assert r["ok"] is True and r["why"] == f"local tests passed {FULL_LABEL}; CI: 1 passed, ignored failing: lint"


AFFECTED = {"full": False, "tests": ["tests/test_a.py", "tests/test_b.py"], "reason": "affected: 2 files", "e2e_ui": False}
CI_OK = {**PR, "statusCheckRollup": _checks(("a", "SUCCESS"))}


def test_affected_run_appends_selected_files_and_names_the_count(env):
    env.sel = AFFECTED
    r = env.rules(CI_OK)["tests"]
    assert env.popen[0][0] == ["uv", "run", "pytest", "-q", "tests/test_a.py", "tests/test_b.py"]
    assert r["ok"] is True and r["why"] == "local tests passed (affected: 2 files); CI: 1 passed"


def test_select_gets_merge_base_ref_and_pr_labels(env):
    env.rules({**PR, "labels": [{"name": "pl:full-tests"}]})
    assert env.select == [(env.path, "refs/pl-merge/7/base", ["pl:full-tests"])]


@pytest.mark.parametrize("cmd,head", [("uv run pytest -q", ["-q", "-n", "auto"]), ("uv run pytest -q -n 2", ["-q", "-n", "2"])])
def test_affected_run_keeps_the_full_commands_workers(env, monkeypatch, cmd, head):
    C.AUTO_MERGE["test_command"] = cmd
    env.sel = AFFECTED
    _pr_worktree_has(env, monkeypatch, '[tests]\nworkers = "auto"\n')
    env.rules()
    assert env.popen[0][0] == ["uv", "run", "pytest", *head, "tests/test_a.py", "tests/test_b.py"]


@pytest.mark.parametrize("reason", ["full suite: pyproject.toml changed", "full suite: git diff failed: boom",
                                    "full suite: pl:full-tests label"])
def test_full_selection_runs_full_command_and_names_reason(env, reason):
    env.sel = {**FULL_SEL, "reason": reason}
    r = env.rules()["tests"]
    assert env.popen[0][0] == ["uv", "run", "pytest", "-q"] and reason in r["why"]


def test_zero_files_runs_nothing_and_passes_tests_part(env):
    env.sel = {"full": False, "tests": [], "reason": "affected: 0 files", "e2e_ui": False}
    r = env.rules(CI_OK)["tests"]
    assert env.popen == [] and r["ok"] is True and r["why"] == "affected: 0 files; CI: 1 passed"
    assert env.rules()["tests"]["ok"] is False


def test_failing_affected_run_keeps_message_plus_set_label(env):
    env.sel = AFFECTED
    env.rc, env.out = 1, "collecting\n1 failed\n"
    r = env.rules()["tests"]
    assert r["ok"] is False and r["why"] == "tests failed (exit 1): 1 failed (affected: 2 files)"


def test_select_config_error_fails_and_runs_nothing(env):
    env.select_error = mc.tests_affected.TestsConfigError(".pl/tests.toml: [tests] full_over must be a whole number")
    r = env.rules()["tests"]
    assert r["ok"] is False and "tests.toml" in r["why"] and env.popen == []
