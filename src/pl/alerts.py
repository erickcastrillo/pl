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
FIELDS = {"severity", "title", "fix", "first_seen", "last_seen", "count", "level", "acked", "resolved_at", "duration", "cards"}
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


def _offer(key, why, title):
    """Queue an alert that opened or escalated for the Assistant and chat. It never fails the alert."""
    hl = headline(why, title)
    try:
        from pl import assistant   # imported here: the alerts module stays light
        assistant.offer(key, hl)
    except (SystemExit, Exception):  # noqa: BLE001 - a tmux refusal raises SystemExit
        pass
    try:
        from pl.manager import machine_dir
        mdir = machine_dir()
        mdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        outbox = mdir / "chat-outbox.jsonl"
        fd = os.open(outbox, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"key": key, "headline": hl}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def flush_offers():
    """End of a dispatcher pass: type the queued alerts into an idle Assistant as one line. Never raises."""
    from pl import assistant
    for step in (assistant.restart_if_updated, lambda: assistant.flush_offers(_clock())):   # restart: on a new build
        try:
            step()
        except (SystemExit, Exception):  # noqa: BLE001
            pass


def _logged(kind, **detail):
    """Write an alert event to the Activity log. It never fails the alert."""
    try:
        from pl import events   # imported here: the alerts module stays light
        events.emit(kind, None, **detail)
    except Exception:  # noqa: BLE001
        pass


def open(key, severity, title, fix, cards=()):
    """Open the alert, or count one more sighting. Returns what to notify: "open", "1 h", "4 h", or None."""
    now, cards = _clock(), [str(c) for c in cards]
    with _locked() as d:
        a = d.get(key)
        if not _live(a):
            d[key] = {"severity": severity, "title": title, "fix": fix, "first_seen": now, "last_seen": now,
                      "count": 1, "level": 0, "cards": cards}
            why = "open"
        else:
            if a.get("acked") and (a.get("severity") != severity or sorted(a.get("cards") or []) != sorted(cards)):
                a.pop("acked")
                a["level"] = 0
            a.update(severity=severity, title=title, fix=fix, cards=cards, last_seen=now,
                     count=int(a.get("count") or 0) + 1)
            why = _step(a, now)
    if why == "open":
        _logged("alert_opened", key=key, severity=severity, title=title, cards=cards)
    if why:
        _offer(key, why, title)
    return why


def resolve(key):
    """Mark an open alert resolved and record how long it was open. False when it was not open."""
    now = _clock()
    with _locked() as d:
        a = d.get(key)
        if not _live(a):
            return False
        a.update(resolved_at=now, duration=now - a["first_seen"])
        title, duration = a["title"], int(a["duration"])
    _logged("alert_resolved", key=key, title=title, duration=duration)
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
    now, out, closed = _clock(), [], []
    with _locked() as d:
        for key, a in d.items():
            if not key.startswith(prefix) or not _live(a):
                continue
            if key not in active:
                a.update(resolved_at=now, duration=now - a["first_seen"])
                closed.append((key, a["title"], int(a["duration"])))
            elif why := _step(a, now):
                out.append((key, why, a["title"], a["fix"]))
    for key, title, duration in closed:
        _logged("alert_resolved", key=key, title=title, duration=duration)
    for key, why, title, fix in out:
        send(headline(why, title), fix)
        _offer(key, why, title)


def headline(why, title):
    return title if why == "open" else f"Still open after {why}: {title}"


def get(key):
    return _load().get(key)


def listing(all=False):
    """Open alerts (all: resolved ones too), high severity first, then oldest first."""
    rows = [{"key": k, **v} for k, v in _load().items() if all or _live(v)]
    return sorted(rows, key=lambda a: (bool(a.get("resolved_at")), a.get("severity") != "high", a.get("first_seen") or 0))


def shown():
    """Open, unacknowledged alerts, newest first (the Alerts tab and header)."""
    return sorted((r for r in listing() if not r.get("acked")), key=lambda a: -(a.get("first_seen") or 0))


WATCHES = {
    "account_parked": "parked accounts",
    "accounts_all_out": "accounts out of credits",
    "api_error": "agents stopped by API errors",
    "card_unreadable": "cards with a broken pl metadata block",
    "chat_unknown": "unknown chat users",
    "connector_new_place": "connector sign-ins from a new place",
    "connector_signin": "connector sign-ins",
    "connector_tools": "connector tools",
    "dep_broken": "unmerged prerequisites",
    "exited_window": "agents that finished without moving their card",
    "foreign_window": "tmux windows pl did not start",
    "gh_hold": "the GitHub budget hold",
    "loop_dead": "loops that burn no tokens",
    "loop_failed": "loops that keep failing",
    "loop_stale": "loops that stopped firing",
    "near_cap": "cards near their token or time cap",
    "permission_wait": "waiting agents",
    "pr_waiting": "PRs waiting over 24 h",
    "send_spike": "connector send spikes",
    "stage_failed": "stages whose agent died",
    "stage_unsafe": "unsafe stage settings",
    "tmp_full": "temp folder space or inodes",
    "trigger_cap": "triggers at their cap",
    "trust": "folders an account does not trust",
}


def watching():
    """What pl checks for, as one phrase list for the empty Alerts tab."""
    return ", ".join(dict.fromkeys(WATCHES.values()))


def cmd_alerts(a):
    if a.ack and getattr(a, "dry_run", False):
        print(f"would acknowledge {a.ack}: no more reminders until it clears")
        return
    if a.ack:
        if not ack(a.ack):
            raise SystemExit(f"pl: no open alert {a.ack} (pl alerts lists them)")
        print(f"acknowledged {a.ack}: no more reminders until it clears")
        return
    rows = listing(a.all)
    if getattr(a, "json", False):
        from pl.jsonout import emit
        emit({"alerts": rows})
        return
    if not rows:
        print("no open alerts")
        return
    for r in rows:
        state = (f"resolved after {age(_clock() - r['duration'])}" if r.get("resolved_at")
                 else "acked" if r.get("acked") else "open")
        print(f"{r.get('severity', '?'):5} {age(r.get('first_seen')):>4} ×{r.get('count', 1):<4} {state:18} {r['key']}")
        print(f"      {r.get('title', '')} · {r.get('fix', '')}")
