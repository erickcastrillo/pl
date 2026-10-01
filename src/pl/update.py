"""Update check: at most once a day, read pl's release tags (vX.Y.Z) from the official repo and say when a newer one
exists. Notify only: pl never installs anything by itself. Off: PL_NO_UPDATE_CHECK, or [updates] check = false."""
import json
import os
import re
import signal
import subprocess
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
       "pl never runs these for you.\n\n")


def _cache():
    return Path.home() / ".local" / "state" / "pl" / "update.json"


def _run(argv):
    """git's stdout; the whole process group is killed at TIMEOUT so a stuck child cannot hold the pipe open."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         start_new_session=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out = p.communicate(timeout=TIMEOUT)[0]
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
