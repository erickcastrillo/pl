"""Alerts: a failing check opens an alert, which escalates after 1 h and again after 4 h and resolves when the check
clears. People are told only on open and escalate; ack stops the reminders until it resolves and opens again.
<profile>/state/alerts.json holds keys, short titles and fixes with card ids only: never card text or secrets."""
import contextlib
import fcntl
import json
import os
import time

from pl import config as C
from pl.util import age

ESCALATE = (3600, 4 * 3600)   # seconds after first seen: the two escalations
STEP = {1: "1 h", 2: "4 h"}
KEEP = 7 * 86400              # a resolved alert stays this long for pl alerts --all
FIELDS = {"severity", "title", "fix", "first_seen", "last_seen", "count", "level", "acked", "resolved_at", "duration"}
_clock = time.time


def _path():
    return C.STATE_DIR / "alerts.json"


def _load():
    try:
        d = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _save(d):
    tmp = _path().with_name(f".alerts.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _path())


@contextlib.contextmanager
def _locked():
    """The alerts under a flock (the dispatcher, its fast guard and the console all write); saved only when changed."""
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(C.STATE_DIR / "alerts.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        d = _load()
        before = json.dumps(d, sort_keys=True)
        yield d
        now = _clock()
        d = {k: v for k, v in d.items() if not v.get("resolved_at") or now - v["resolved_at"] < KEEP}
        if json.dumps(d, sort_keys=True) != before:
            _save(d)
    finally:
        os.close(fd)


def _live(a):
    return bool(a) and not a.get("resolved_at")


def _step(a, now):
    """Raise the level to the alert's age: "1 h" or "4 h" when that is news to tell (not acked), else None."""
    level = sum(now - a["first_seen"] >= s for s in ESCALATE)
    if level <= a.get("level", 0):
        return None
    a["level"] = level
    return None if a.get("acked") else STEP[level]


def open(key, severity, title, fix):
    """Open the alert, or count one more sighting. Returns what to notify: "open", "1 h", "4 h", or None."""
    now = _clock()
    with _locked() as d:
        a = d.get(key)
        if not _live(a):
            d[key] = {"severity": severity, "title": title, "fix": fix, "first_seen": now, "last_seen": now,
                      "count": 1, "level": 0}
            return "open"
        a.update(severity=severity, title=title, fix=fix, last_seen=now, count=int(a.get("count") or 0) + 1)
        return _step(a, now)


def resolve(key):
    """Mark an open alert resolved and record how long it was open. False when it was not open."""
    now = _clock()
    with _locked() as d:
        a = d.get(key)
        if not _live(a):
            return False
        a.update(resolved_at=now, duration=now - a["first_seen"])
        return True


def ack(key):
    """Stop the escalation reminders of an open alert until it resolves. False when it is not open."""
    with _locked() as d:
        a = d.get(key)
        if not _live(a):
            return False
        a["acked"] = True
        return True


def sweep(prefix, active, send):
    """Resolve the open alerts under prefix whose key is not in active; escalate the rest, calling send(title, fix)."""
    now, out = _clock(), []
    with _locked() as d:
        for key, a in d.items():
            if not key.startswith(prefix) or not _live(a):
                continue
            if key not in active:
                a.update(resolved_at=now, duration=now - a["first_seen"])
            elif why := _step(a, now):
                out.append((why, a["title"], a["fix"]))
    for why, title, fix in out:
        send(headline(why, title), fix)


def headline(why, title):
    return title if why == "open" else f"Still open after {why}: {title}"


def get(key):
    return _load().get(key)


def listing(all=False):
    """Open alerts (all: resolved ones too), high severity first, then oldest first."""
    rows = [{"key": k, **v} for k, v in _load().items() if all or _live(v)]
    return sorted(rows, key=lambda a: (bool(a.get("resolved_at")), a.get("severity") != "high", a.get("first_seen") or 0))


def cmd_alerts(a):
    if a.ack:
        if not ack(a.ack):
            raise SystemExit(f"pl: no open alert {a.ack} (pl alerts lists them)")
        print(f"acknowledged {a.ack}: no more reminders until it clears")
        return
    rows = listing(a.all)
    if not rows:
        print("no open alerts")
        return
    for r in rows:
        state = (f"resolved after {age(_clock() - r['duration'])}" if r.get("resolved_at")
                 else "acked" if r.get("acked") else "open")
        print(f"{r.get('severity', '?'):5} {age(r.get('first_seen')):>4} ×{r.get('count', 1):<4} {state:18} {r['key']}")
        print(f"      {r.get('title', '')} · {r.get('fix', '')}")
