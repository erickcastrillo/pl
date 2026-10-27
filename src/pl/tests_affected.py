"""pl tests affected: the test files a change can affect in a Python repository, from the import graph.

Files are parsed with ast, never imported. git runs through _run (tests fake it). A repository's own
.pl/tests.toml ([tests]: affected_cmd, always, full_on) adjusts or replaces the finder. Anything the finder cannot
judge (a git failure, no Python files, a shared test file changed) says "full" so the build runs the whole suite."""
import argparse
import ast
import fnmatch
import shlex
import subprocess
import sys
import tomllib
from collections import deque
from pathlib import Path

from pl import jsonout, worker_budget

SKIP_DIRS = {"node_modules", "__pycache__", "build", "dist"}
FULL_FILES = {"conftest.py", "pyproject.toml", "uv.lock"}
TEST_DIRS = {"tests", "test"}
CMD_TIMEOUT = 120


class GitError(Exception):
    pass


class SettingsError(Exception):
    pass


def _run(argv, cwd, timeout=60, capture=True):
    """The one subprocess seam of this module (tests fake it): (return code, stdout, stderr).
    capture=False runs attached to the terminal with no timeout and returns empty output."""
    if not capture:
        try:
            return subprocess.run(argv, cwd=cwd).returncode, "", ""
        except OSError as e:
            return 1, "", str(e)
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout, r.stderr


def _git(root, *args):
    code, out, err = _run(["git", *args], root)
    if code:
        raise GitError(f"git {args[0]} failed: {(err or out).strip()}")
    return out.strip()


def base_ref(root, ref=None):
    if ref:
        return ref
    try:
        default = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except GitError:
        default = "origin/main"
    return _git(root, "merge-base", "HEAD", default)


def changed_paths(root, base):
    tracked = _git(root, "diff", "--name-only", base).splitlines()
    new = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(tracked) | set(new))


def _python_files(root):
    return sorted(
        p.relative_to(root).as_posix() for p in Path(root).rglob("*.py")
        if not any(part.startswith(".") or part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]))


def _module_name(rel):
    parts = Path(rel).with_suffix("").parts
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(rel, text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    module = _module_name(rel)
    package = module.split(".") if rel.endswith("__init__.py") else module.split(".")[:-1]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            name = ".".join(base + ([node.module] if node.module else []))
            found.add(name)
            found.update(f"{name}.{a.name}" if name else a.name for a in node.names)
    found.discard("")
    return found


def _is_test(rel):
    name = Path(rel).name
    return name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")


def _full_reason(changed):
    for p in changed:
        parts = Path(p).parts
        helper = p.endswith(".py") and not _is_test(p) and bool(TEST_DIRS & set(parts[:-1]))
        if parts[-1] in FULL_FILES or helper:
            return f"{p} changed"
    return None


def _importers(root, files):
    """module -> the modules that import it."""
    graph = {}
    for rel in files:
        try:
            text = (Path(root) / rel).read_text(errors="replace")
        except OSError:
            continue
        for name in _imports(rel, text):
            graph.setdefault(name, set()).add(_module_name(rel))
    return graph


def _reached(graph, changed_modules):
    """Breadth-first from the changed modules: module -> (changed module it came from, first importer or None)."""
    seen = {m: (m, None) for m in changed_modules}
    queue = deque(changed_modules)
    while queue:
        mod = queue.popleft()
        origin, first = seen[mod]
        for imp in sorted(graph.get(mod, ())):
            if imp not in seen:
                seen[imp] = (origin, first or (mod if mod != origin else None))
                queue.append(imp)
    return seen


def python_affected(root, changed, files):
    tests = {}
    by_module = {_module_name(f): f for f in files}
    changed_py = [p for p in changed if p.endswith(".py")]
    reached = _reached(_importers(root, files), {_module_name(p) for p in changed_py})
    for p in changed_py:
        if _is_test(p) and p in files:
            tests[p] = "changed test"
    for mod, (origin, first) in sorted(reached.items()):
        path = by_module.get(mod)
        if path and _is_test(path) and path not in tests and mod != origin:
            tests[path] = f"imports {origin}" + (f" (via {first})" if first else "")
    for p in changed_py:
        stem = Path(p).name
        for f in files:
            if f not in tests and _is_test(f) and Path(f).name == f"test_{stem}":
                tests[f] = f"name match {stem}"
    for p in changed:
        if p.endswith(".py") or Path(p).name in FULL_FILES:
            continue
        name, folder = Path(p).name, Path(p).parent.name
        for f in files:
            if f not in tests and _is_test(f):
                text = (Path(root) / f).read_text(errors="replace")
                if name in text and folder in text:
                    tests[f] = f"mentions {p}"
    return tests


def _string_list(cfg, key):
    value = cfg.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f".pl/tests.toml: {key} must be a list of strings")
    return value


def _settings(root):
    """The validated [tests] table of <root>/.pl/tests.toml; {} when there is no file or no table."""
    try:
        cfg = tomllib.loads((Path(root) / ".pl" / "tests.toml").read_text()).get("tests")
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(f".pl/tests.toml: {e}") from e
    if not isinstance(cfg, dict):
        return {}
    found = {"always": _string_list(cfg, "always"), "full_on": _string_list(cfg, "full_on")}
    cmd = cfg.get("affected_cmd")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if cmd is not None:
        if not cmd or not isinstance(cmd, list) or not all(isinstance(v, str) for v in cmd):
            raise SettingsError(".pl/tests.toml: affected_cmd must be a non-empty string or list of strings")
        found["affected_cmd"] = cmd
    return found


def _workers_setting(root):
    """[tests] workers of <root>/.pl/tests.toml, the repo's ceiling; None when unset. A bad value is an error."""
    try:
        cfg = tomllib.loads((Path(root) / ".pl" / "tests.toml").read_text()).get("tests")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(cfg, dict) or "workers" not in cfg:
        return None
    value = cfg["workers"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SettingsError(".pl/tests.toml: workers must be a whole number of at least 1")
    return value


def test_command(root, files=None):
    """The pytest command to run, started through `pl tests run` with the workers the budget allows now."""
    n = worker_budget.allowed(_workers_setting(root))
    base = ["uv", "run", "pytest"] if (Path(root) / "uv.lock").exists() else ["python", "-m", "pytest"]
    return ["pl", "tests", "run", "--", *base, "-n", str(n), "-q", *(files or [])]


def _workers_in(argv):
    for i, a in enumerate(argv):
        if a == "-n" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if a.startswith("-n") and a[2:].isdigit():
            return int(a[2:])
    return 1


def _from_cmd(root, argv, base):
    code, out, err = _run([*argv, base], root, timeout=CMD_TIMEOUT)
    if code:
        raise SettingsError(f"affected_cmd failed: {(err or out).strip() or f'exit {code}'}")
    return {line.strip(): "from affected_cmd" for line in out.splitlines() if line.strip()}


def _full_on_reason(changed, patterns):
    for p in changed:
        for pat in patterns:
            if fnmatch.fnmatch(p, pat):
                return f"{p} matches full_on {pat}"
    return None


def _fallback(reason):
    print(f"pl tests: {reason}", file=sys.stderr)
    return {"tests": [], "reasons": {}, "full": True, "full_reason": reason}


def affected(root, ref=None):
    try:
        cfg = _settings(root)
    except SettingsError as e:
        return _fallback(str(e))
    files = _python_files(root)
    if not cfg and not files:
        return {"tests": [], "reasons": {}, "full": True, "full_reason": "no Python files and no .pl/tests.toml"}
    changed = changed_paths(root, base_ref(root, ref))
    reasons, reason = {}, None
    if "affected_cmd" in cfg:
        try:
            reasons = _from_cmd(root, cfg["affected_cmd"], base_ref(root, ref))
        except SettingsError as e:
            print(f"pl tests: {e}", file=sys.stderr)
            reason = str(e)
    elif files:
        reasons = python_affected(root, changed, files)
        reason = _full_reason(changed)
    reason = reason or _full_on_reason(changed, cfg.get("full_on", []))
    reasons.update({p: "always rule" for p in cfg.get("always", [])})
    return {"tests": sorted(reasons), "reasons": reasons, "full": bool(reason), "full_reason": reason}


def cmd_tests(argv):
    ap = argparse.ArgumentParser(prog="pl tests")
    sub = ap.add_subparsers(dest="action", required=True)
    aff = sub.add_parser("affected", help="the test files a change can affect")
    aff.add_argument("--base", metavar="REF", help="compare with REF (default: merge base with the default branch)")
    aff.add_argument("--json", action="store_true")
    sub.add_parser("full", help="the command that runs the full suite with the workers the budget allows")
    run = sub.add_parser("run", help="run a test command while holding its pytest workers (-n N) from the budget")
    run.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND")
    args = ap.parse_args(argv)
    if args.action == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            print("pl tests run: give the command after --", file=sys.stderr)
            return 2
        with worker_budget.hold(_workers_in(command)):
            return _run(command, ".", capture=False)[0]
    try:
        root = _git(".", "rev-parse", "--show-toplevel")
        if args.action == "full":
            print(shlex.join(test_command(root)))
            return 0
        res = affected(root, args.base)
    except GitError as e:
        print(f"pl tests: {e}", file=sys.stderr)
        root, res = ".", {"tests": [], "reasons": {}, "full": True, "full_reason": str(e)}
    except SettingsError as e:
        print(f"pl tests: {e}", file=sys.stderr)
        return 2
    try:
        command = test_command(root) if res["full"] else test_command(root, res["tests"]) if res["tests"] else None
    except SettingsError as e:
        print(f"pl tests: {e}", file=sys.stderr)
        return 2
    command = shlex.join(command) if command else None
    if args.json:
        jsonout.emit({**{k: res[k] for k in ("tests", "reasons", "full")}, "command": command})
        return 0
    if res["full"]:
        print(f"full suite: {res['full_reason']}")
    elif not res["tests"]:
        print("no tests affected")
    else:
        print("\n".join(f"{t}  {res['reasons'][t]}" for t in res["tests"]))
    if command:
        print(command)
    return 0
