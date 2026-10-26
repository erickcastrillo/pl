"""Append-only event log (events.jsonl in the state folder) and the counts the Dashboard reads from it.

Events carry ids, column names, stage, harness and short messages only: never card text, secrets or config values."""
import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from pl import config as C

MAX_LINES = 20_000      # past this many lines the file is rewritten without events older than KEEP_DAYS
KEEP_DAYS = 30          # the Dashboard needs 14 days; never prune younger than this
ERROR_CHARS = 200


def _now():
    """The clock retention measures age against (tests pin it)."""
    return datetime.now(timezone.utc)


def _path():
    return C.STATE_DIR / "events.jsonl"


def emit(kind, card_id=None, **detail):
    """Append one event line. Never raises on a disk problem: losing an event must not stop the dispatcher."""
    from pl import trackers
    if trackers.dry_run():
        return
    if kind == "error" and "message" in detail:
        detail["message"] = str(detail["message"])[:ERROR_CHARS]
    rec = {**detail, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "profile": C.PROFILE_NAME,
           "kind": kind, "card": card_id}
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            lock = os.open(_path().with_name("events.jsonl.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:                               # no lock file possible (read-only dir): append unlocked, no trim
            lock = None
        if lock is None:
            fd = os.open(_path(), os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)          # append and trim never interleave across writers
            fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)   # 0600 before the first write
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
            _prune()
        finally:
            os.close(lock)                            # closing releases the lock
    except OSError:
        pass


def failure(kind, level, message, card_id=None, **detail):
    """A failure pl detected, as an entry with a level (error: the work stopped; warning: skipped or retried).
    The message is masked and cut to ERROR_CHARS: it may quote an exception that holds a ${VAR} value."""
    from pl import util   # imported here, like trackers in emit
    emit(kind, card_id, level=level, message=util.mask(message)[:ERROR_CHARS], **detail)


def _read():
    out = []
    try:
        text = _path().read_text()
    except OSError:
        return out
    for line in text.splitlines():
        try:
            e = json.loads(line)
            e["_t"] = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            if e["_t"].tzinfo is None:
                e["_t"] = e["_t"].replace(tzinfo=timezone.utc)
            out.append(e)
        except (ValueError, KeyError, AttributeError, TypeError):
            continue
    return out


def state_transition_counts(since_ts, now_ts=None):
    """{"hook": n, "screen": n}: state_transition events from `since_ts` (epoch seconds) to `now_ts` (default: now)."""
    end = now_ts if now_ts is not None else float("inf")
    out = {"hook": 0, "screen": 0}
    for e in _read():
        if e["kind"] == "state_transition" and e.get("source") in out and since_ts <= e["_t"].timestamp() <= end:
            out[e["source"]] += 1
    return out


def _prune():
    path = _path()
    if path.stat().st_size < MAX_LINES or path.read_bytes().count(b"\n") <= MAX_LINES:
        return
    cutoff = _now() - timedelta(days=KEEP_DAYS)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")   # unique, 0600
    try:
        with os.fdopen(fd, "w") as f:
            for e in _read():
                if e["_t"] >= cutoff:
                    f.write(json.dumps({k: v for k, v in e.items() if k != "_t"}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def forget(target, dry_run=False):
    """Rewrite events.jsonl and mcp-*.log without lines holding target. Returns count of lines removed."""
    if not target:
        return 0
    target_lower, total_removed = str(target).strip().lower(), 0
    log_files = ([_path()] if _path().is_file() else []) + (
        [p for p in sorted(C.STATE_DIR.glob("mcp-*.log")) if p.is_file()] if C.STATE_DIR.is_dir() else []
    )
    for path in log_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines(keepends=True)
        kept = [ln for ln in lines if target_lower not in ln.lower()]
        removed = len(lines) - len(kept)
        if not removed:
            continue
        total_removed += removed
        if not dry_run:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
    return total_removed


def metrics(window_seconds, now=None):
    """Counts of events inside the last `window_seconds`; an event exactly at the edge does not count.
    `errors_last` is the most recent error event of all time (or None)."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=window_seconds)
    evs = _read()
    inside = [e for e in evs if start < e["_t"] <= now]
    moved = lambda col: sum(1 for e in inside if e["kind"] == "moved" and e.get("to") == col)  # noqa: E731
    errors = [e for e in evs if e["kind"] == "error"]
    last = max(errors, key=lambda e: e["_t"], default=None)
    return {"specs_written": moved("Spec ready"), "plans_written": moved("Plan for review"),
            "approvals": sum(1 for e in inside if e["kind"] == "approved"),
            "errors_last": {k: v for k, v in last.items() if k != "_t"} if last else None}


def daily(column, days=14, now=None):
    """Moves into `column` per UTC day for the last `days` days, oldest first; the last item is today."""
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    counts = [0] * days
    for e in _read():
        if e["kind"] == "moved" and e.get("to") == column:
            ago = (today - e["_t"].astimezone(timezone.utc).date()).days
            if 0 <= ago < days:
                counts[days - 1 - ago] += 1
    return counts
