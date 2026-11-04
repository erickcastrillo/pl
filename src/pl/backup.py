"""Git backup of the local board: the dispatcher writes the whole board to <[sync] git>/board.json, commits only that
file and pushes it, at most once per 5-minute window after a change and once on shutdown. A failed push stays queued
in the clone and is tried again in the next window; three in a row open one alert. Only [tracker] type = "local"."""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import alerts, board, events, trackers
from pl.util import mask

INTERVAL = 300
MARGIN = 30       # the window closes early, so a commit lands inside 5 minutes given the dispatcher's nap slices
ALERT_AFTER = 3
FILE = "board.json"
_clock = time.time
URL_CREDS_RE = re.compile(r"(?<=://)[^/@\s]+@")
_ok = None        # (folder, True/False): the start check's answer


def _run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _git(f, *args):
    return _run(["git", "-C", str(f), *args])


def folder():
    git = (C.SYNC or {}).get("git")
    if not git or (C.TRACKER or {}).get("type") != "local":
        return None
    return Path(git).expanduser()


def snapshot():
    t = trackers.get("tracker")
    cols = t.columns()
    title = {i: n for n, i in cols.items()}
    cards = sorted(({"id": c["id"], "column": title.get(c["list_id"], c["list_id"]), "title": c["title"],
                     "description": c["description"], "tags": c["tags"], "assigned_to": c["assigned_to"],
                     "metadata": c["metadata"]} for c in t.cards()), key=lambda c: str(c["id"]))
    return json.dumps({"columns": [{"id": i, "title": n} for n, i in cols.items()], "cards": cards},
                      sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _state_path():
    return C.STATE_DIR / "backup.json"


def _load():
    try:
        got = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        got = {}
    return {"pending_since": None, "seen": 0, "last_try": 0, "fails": 0, "unpushed": False,
            **(got if isinstance(got, dict) else {})}


def _save(st):
    _atomic(_state_path(), json.dumps(st))


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def check():
    """At dispatcher start: is [sync] git a git clone? One error and one alert when not. False also when unset."""
    global _ok
    f = folder()
    if f is None:
        _ok = None
        return False
    try:
        good = f.is_dir() and _git(f, "rev-parse", "--is-inside-work-tree")[0] == 0
    except (Exception, SystemExit) as e:  # noqa: BLE001 - a backup problem never stops the dispatcher
        events.emit("error", message=f"board backup check failed: {e}")
        good = False
    _ok = (str(f), good)
    if good:
        alerts.resolve("backup_clone")
        return True
    title = f"[sync] git: {f} is not a git clone"
    events.emit("error", message=title)
    alerts.open("backup_clone", "warn", title, "clone the backup repository there, then restart the dispatcher")
    return False


def _first_line(text):
    lines = (text or "").strip().splitlines()
    return mask(URL_CREDS_RE.sub("***@", lines[0])) if lines else "git failed"


def _failed(f, st, now, text):
    st["fails"] += 1
    st["last_try"] = now
    line = _first_line(text)
    events.emit("backup_failed", message=line)
    if st["fails"] >= ALERT_AFTER:
        alerts.open("backup_push", "warn", f"Board backup to {f} failed {st['fails']} times: {line}",
                    "fix the clone by hand (pull, credentials, remote), pl retries every 5 min")


def _commit(f, now):
    """True when committed or nothing changed; (False, error text) when git failed."""
    _atomic(f / FILE, snapshot())
    rc, _, err = _git(f, "add", "--", FILE)
    if rc:
        return False, err
    if _git(f, "diff", "--cached", "--quiet", "--", FILE)[0] == 0:
        return True, ""
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rc, _, err = _git(f, "commit", "-m", f"pl board backup {stamp}", "--", FILE)
    return (False, err) if rc else (True, "committed")


def tick(force=False):
    """Back up when a window has ended (or force). Never raises."""
    try:
        _tick(force)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - a backup problem never stops the dispatcher
        events.emit("error", message=f"board backup failed: {e}")


def refresh():
    """Back up right now, whatever the window says (pl forget uses it). False when [sync] git is unset or unusable."""
    if folder() is None or not check():
        return False
    st = _load()
    st["pending_since"] = st["pending_since"] or _clock()
    _save(st)
    tick(force=True)
    return True


def _tick(force):
    f = folder()
    if f is None or _ok != (str(f), True):
        return
    now, st = _clock(), _load()
    window = INTERVAL - MARGIN
    w = board.last_write() or 0
    if w > st["seen"]:
        st["seen"] = w
        st["pending_since"] = st["pending_since"] or w
    pend = st["pending_since"]
    due = force or (pend and now - max(pend, st["last_try"]) >= window) \
        or (st["unpushed"] and now - st["last_try"] >= window)
    if not due:
        _save(st)
        return
    if pend:
        ok, out = _commit(f, now)
        if not ok:
            _failed(f, st, now, out)
            _save(st)
            return
        st["pending_since"] = None
        st["unpushed"] = st["unpushed"] or out == "committed"
    if st["unpushed"]:
        rc, _, err = _git(f, "push")
        if rc:
            _failed(f, st, now, err)
        else:
            st.update(unpushed=False, fails=0)
            alerts.resolve("backup_push")
            events.emit("backup_pushed")
    _save(st)
