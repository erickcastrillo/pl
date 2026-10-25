"""The merge judge's conflicts and tests rules: merge a pull request onto the default branch in a throwaway worktree,
run the configured test command there, and read the pull request's CI checks. Nothing is pushed, merged or commented."""
import shlex
import subprocess
from pathlib import Path

from pl import config as C
from pl.trackers import github
from pl.tui.prs import checks
from pl.worktrees import _stop_group

_popen = subprocess.Popen


def _git(argv, cwd):
    r = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def default_branch(repo):
    return github._gh(["repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]).strip()


def _last_line(text):
    return next((ln.strip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")[:200]


def _name(c):
    return c.get("name") or c.get("context") or "?"


def _failed(c):
    return checks([c])[1] == 1


def ci(pr, ignore):
    """(ok, text) for the pull request's CI checks: pending and missing checks never block; a failing one not in `ignore` does."""
    rollup = pr.get("statusCheckRollup") or []
    failed = [_name(c) for c in rollup if _failed(c)]
    blocking = [n for n in failed if n not in ignore]
    if blocking:
        return False, f"CI check failed: {', '.join(blocking)}"
    passed, _, pending = checks(rollup)
    if not rollup:
        return True, "CI: none"
    text = f"CI: {passed} passed" + (f", {pending} pending" if pending else "")
    return True, text + (f", ignored failing: {', '.join(failed)}" if failed else "")


def run_tests(path, cmd, timeout):
    proc = _popen(shlex.split(cmd), cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout * 60)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        return False, f"timed out after {timeout} minutes"
    if proc.returncode:
        return False, f"tests failed (exit {proc.returncode}): {_last_line(out)}"
    return True, "local tests passed"


def _tests_rule(pr, path):
    settings = C.AUTO_MERGE
    cmd = settings.get("test_command")
    if not cmd:
        return {"ok": False, "why": "no test_command configured"}
    ok, why = run_tests(path, cmd, settings.get("test_timeout", 15))
    if not ok:
        return {"ok": False, "why": why}
    ci_ok, ci_why = ci(pr, settings.get("ignore_checks", []))
    return {"ok": ci_ok, "why": f"local tests passed; {ci_why}"}


def _conflict_paths(path):
    _, out = _git(["diff", "--name-only", "--diff-filter=U"], path)
    paths = out.split()
    return ", ".join(paths[:3]) + (f" +{len(paths) - 3} more" if len(paths) > 3 else "")


def _cleanup(path, refs):
    for argv in (["worktree", "remove", "--force", str(path)], ["worktree", "prune"], *(["update-ref", "-d", r] for r in refs)):
        try:
            _git(argv, C.WORK_DIR)
        except Exception:
            pass


def check(pr, base):
    """[conflicts rule, tests rule] for one pull request; `ok` is None when a rule could not run."""
    n = pr["number"]
    refs = [f"refs/pl-merge/{n}/base", f"refs/pl-merge/{n}/head"]
    path = Path(C.STATE_DIR) / "merge-check" / f"pr-{n}"
    if path.exists():
        _cleanup(path, refs)
    try:
        rc, out = _git(["fetch", "--no-tags", "origin", f"+refs/heads/{base}:{refs[0]}", f"+refs/pull/{n}/head:{refs[1]}"], C.WORK_DIR)
        if rc:
            return [{"rule": "conflicts", "ok": None, "why": f"not run: git fetch failed: {_last_line(out)}"},
                    {"rule": "tests", "ok": None, "why": "not run: git fetch failed"}]
        rc, out = _git(["worktree", "add", "--detach", str(path), refs[0]], C.WORK_DIR)
        if rc:
            return [{"rule": "conflicts", "ok": None, "why": f"not run: git worktree add failed: {_last_line(out)}"},
                    {"rule": "tests", "ok": None, "why": "not run: git worktree add failed"}]
        rc, _ = _git(["-c", "user.name=pl", "-c", "user.email=pl@localhost", "merge", "--no-ff", "--no-edit", refs[1]], path)
        if rc:
            return [{"rule": "conflicts", "ok": False, "why": f"conflicts with {base}: {_conflict_paths(path)}"},
                    {"rule": "tests", "ok": None, "why": "not run: conflicts"}]
        return [{"rule": "conflicts", "ok": True, "why": f"merges cleanly onto {base}"},
                {"rule": "tests", **_tests_rule(pr, path)}]
    finally:
        _cleanup(path, refs)
