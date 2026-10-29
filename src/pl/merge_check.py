"""The merge judge's conflicts and tests rules: merge a pull request onto the default branch in a throwaway worktree,
run the configured test command there, and read the pull request's CI checks. Nothing is pushed, merged or commented.
`[loops.auto-merge] test_command` runs the pull request's own code on this host, with only the `test_env` variables and a private HOME."""
import os
import re
import shlex
import subprocess
from pathlib import Path

from pl import config as C
from pl import tests_affected
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


def ci(pr, settings):
    """(ok, text) for the pull request's CI checks: a failing one not in `ignore_checks` blocks; unfinished checks leave it undecided;
    no checks at all block unless `require_ci` is false."""
    ignore = settings.get("ignore_checks", [])
    rollup = pr.get("statusCheckRollup") or []
    failed = [_name(c) for c in rollup if _failed(c)]
    blocking = [n for n in failed if n not in ignore]
    if blocking:
        return False, f"CI check failed: {', '.join(blocking)}"
    passed, _, pending = checks(rollup)
    if pending:
        return None, f"CI pending ({pending} of {len(rollup)}), not decided"
    if not rollup:
        if settings.get("require_ci", True):
            return False, "CI: none, and require_ci is on"
        return True, "CI: none"
    return True, f"CI: {passed} passed" + (f", ignored failing: {', '.join(failed)}" if failed else "")


_BASE_ENV = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")


def _test_env(settings):
    """The pull request's code runs unattended, so it gets a private HOME and only the named variables, never pl's secrets."""
    home = Path(C.STATE_DIR) / "merge-check" / "home"
    home.mkdir(parents=True, exist_ok=True)
    keep = (*_BASE_ENV, *settings.get("test_env", []))
    return {**{k: os.environ[k] for k in keep if k in os.environ}, "HOME": str(home)}


_FAILED = re.compile(r"^FAILED (\S+)", re.M)


def _failing(out):
    return list(dict.fromkeys(_FAILED.findall(out or "")))


def run_tests(path, cmd, timeout, env):
    proc = _popen(shlex.split(cmd), cwd=path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout * 60)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        return False, f"timed out after {timeout} minutes", []
    if proc.returncode:
        ids = _failing(out)
        if not ids:
            return False, f"tests failed (exit {proc.returncode}): {_last_line(out)}", []
        more = f" +{len(ids) - 5} more" if len(ids) > 5 else ""
        return False, f"tests failed (exit {proc.returncode}): {', '.join(ids[:5])}{more}", ids
    return True, "local tests passed", []


def _is_pytest(cmd):
    return any(Path(t).name in ("pytest", "py.test") for t in shlex.split(cmd))


def _selection(pr, path, cmd):
    if not _is_pytest(cmd):
        return {"full": True, "tests": [], "reason": "full suite: test_command is not pytest"}
    labels = [x["name"] for x in pr.get("labels") or []]
    return tests_affected.select(path, f"refs/pl-merge/{pr['number']}/base", labels)


def _tests_rule(pr, path, stack_top=False):
    settings = C.AUTO_MERGE
    cmd = settings.get("test_command")
    if not cmd:
        return {"ok": False, "why": "no test_command configured"}
    try:
        sel = {"full": True, "tests": [], "reason": "stack top"} if stack_top else _selection(pr, path, cmd)
        full = tests_affected.full_command(path, cmd)
    except tests_affected.TestsConfigError as e:
        return {"ok": False, "why": str(e)}
    label = sel["reason"]
    if not sel["full"] and not sel["tests"]:
        ci_ok, ci_why = ci(pr, settings)
        return {"ok": ci_ok, "why": f"{label}; {ci_why}"}
    run = full if sel["full"] else f"{full} {shlex.join(sel['tests'])}"
    ok, why, ids = run_tests(path, run, settings.get("test_timeout", 15), _test_env(settings))
    if not ok:
        return {"ok": False, "why": f"{why} ({label})", **({"failing": ids} if ids else {})}
    ci_ok, ci_why = ci(pr, settings)
    if stack_top:
        return {"ok": ci_ok, "why": f"full suite passed (stack top); {ci_why}"}
    return {"ok": ci_ok, "why": f"local tests passed ({label}); {ci_why}"}


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


def check(pr, base, full=False):
    """[conflicts rule, tests rule] for one pull request; `ok` is None when a rule could not run. `full` runs the whole
    suite whatever the affected-test selection would pick: the top of a stack."""
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
                {"rule": "tests", **_tests_rule(pr, path, full)}]
    finally:
        _cleanup(path, refs)
