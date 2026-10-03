"""Update check: at most once a day, read pl's release tags (vX.Y.Z) from the official repo and say when a newer one
exists. Notify only: pl never installs anything by itself. Off: PL_NO_UPDATE_CHECK, or [updates] check = false."""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from pl import __version__
from pl import config as C

REPO = "https://github.com/erickcastrillo/pl"
DAY, TIMEOUT = 86400, 3
TAG = re.compile(r"refs/tags/(v(\d+)\.(\d+)\.(\d+))$")
COMMANDS = """cd ~/code/pl
git pull
uv tool install --force --reinstall . --constraints <(uv export --frozen --no-dev --no-emit-project --no-header)
pl --version"""
HOW = ("To update pl, run these in your clone of " + REPO + " (INSTALL.md, section 8).\n"
       "pl never runs these for you. After the install, pl restarts itself: the manager and the dispatchers pick up\n"
       "the new version within a minute; running agents and loops keep running. The first time, from a pl older than\n"
       "this, open a new console (or run pl manager stop, then pl manager start) once.\n\n")
PKG = Path(__file__).parent
_BUILD = {}
_sleep = time.sleep


def build_id_of(root):
    """The version plus a short hash of every file's path, size and mtime under root (compiled caches left out),
    so a reinstall of the same version is a new build. None when root holds no package (an install in progress)."""
    if not (root / "__init__.py").is_file():
        return None
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        try:
            if "__pycache__" not in p.parts and p.is_file():
                s = p.stat()
                h.update(f"{p.relative_to(root)} {s.st_size} {s.st_mtime_ns}\n".encode())
        except OSError:
            continue
    try:   # the version of the files at root, not of this running process (after an upgrade they differ)
        found = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', (root / "__init__.py").read_text(), re.M)
    except OSError:
        found = None
    return f"{found.group(1) if found else __version__}-{h.hexdigest()[:10]}"


def build_id(fresh=False):
    """This process's build, read once (at its first call) and kept; fresh: the build installed now."""
    if fresh:
        return build_id_of(PKG)
    if "id" not in _BUILD:
        _BUILD["id"] = build_id_of(PKG)
    return _BUILD["id"]


_BAD = set()   # installed builds whose import check failed: this process never tries them again
CHECK = "import pl.cli, pl.manager, pl.dispatch; print('ok')"


def new_build(asked=False):
    """The installed build when it is not this process's (asked: even when it is) and stayed the same for 2 s
    (a reinstall in progress is not a build yet), else None. A build that failed its import check is never new."""
    b = build_id(fresh=True)
    if not b or b in _BAD or (b == build_id() and not asked):
        return None
    _sleep(2)
    return b if build_id(fresh=True) == b else None


def importable():
    """The installed pl imports in a fresh Python (the one this process runs: the install's own)."""
    try:
        return _run([sys.executable, "-c", CHECK], timeout=60).strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


def restart_build(asked=False, warn=print):
    """The installed build to restart into: new_build(asked) and it imports; else None. A build that does not import
    is warned about once and remembered, so the running code keeps running until the next install."""
    b = new_build(asked)
    if not b:
        return None
    if importable():
        return b
    _BAD.add(b)
    warn(f"pl build {b} is installed but does not import; keeping the running version until the next install")
    return None


def _cache():
    return Path.home() / ".local" / "state" / "pl" / "update.json"


def _run(argv, timeout=None):
    """The command's stdout ("" when it fails); the whole process group is killed at timeout so a stuck child cannot
    hold the pipe open."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         start_new_session=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out = p.communicate(timeout=timeout or TIMEOUT)[0]
        return out if p.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        p.stdout.close()
        raise


def enabled():
    return not os.environ.get("PL_NO_UPDATE_CHECK") and (C.UPDATES or {}).get("check", True) is not False


def _num(v):
    return tuple(int(x) for x in v.removeprefix("v").split("."))


def check(now=None):
    """The newest vX.Y.Z tag: from the cache when it is under a day old, else from git ls-remote. None when the check
    is off, nothing was found, or anything failed. A failed check still counts as the day's check."""
    if not enabled():
        return None
    now = time.time() if now is None else now
    try:
        c = json.loads(_cache().read_text())
        if 0 <= now - float(c["checked_at"]) < DAY:
            return c.get("latest") if isinstance(c.get("latest"), str) else None
    except Exception:  # noqa: BLE001 - no cache or a bad one: check again
        pass
    latest = None
    try:
        tags = [m.group(1) for line in _run(["git", "ls-remote", "--tags", "--refs", REPO]).splitlines()
                if (m := TAG.search(line.strip()))]
        latest = max(tags, key=_num, default=None)
    except Exception:  # noqa: BLE001 - offline, no git, a timeout: silent
        pass
    try:
        _cache().parent.mkdir(parents=True, exist_ok=True)
        _cache().write_text(json.dumps({"checked_at": now, "latest": latest}))
    except OSError:
        pass
    return latest


def notice(latest, hint="press U for how to update"):
    """The one-line notice when latest is newer than the installed version, else None."""
    try:
        if latest and _num(latest) > _num(__version__):
            return f"pl {latest} is available (you have v{__version__}) — {hint}"
    except ValueError:
        pass
    return None


def start_background(callback, hint="press U for how to update"):
    """Check in a daemon thread and call callback(notice) only when there is one. Returns the thread (None when off)."""
    if not enabled():
        return None

    def job():
        try:
            if n := notice(check(), hint):
                callback(n)
        except Exception:  # noqa: BLE001 - never breaks start-up
            pass
    t = threading.Thread(target=job, name="pl-update-check", daemon=True)
    t.start()
    return t


CLI_HINT = "run pl update for how to update"


def version_text():
    lines = [f"pl {__version__}"]
    if n := notice(check(), CLI_HINT):
        lines.append(n)
    return "\n".join(lines)


def cmd_update(a):
    if getattr(a, "check", False):
        if not enabled():
            print(f"pl {__version__}\nthe update check is off (PL_NO_UPDATE_CHECK, or [updates] check = false)")
        else:
            print(version_text())
        return 0
    print(HOW + COMMANDS)
    return 0
