"""Slow-work warnings: one `warning` event per stuck condition, one `info` <kind>_cleared event when it ends.

Episode state lives in memory: after a dispatcher restart an open condition may be logged again."""
import statistics
import sys
import time

from pl import config as C
from pl import events, jsonout
from pl.util import parse_iso

DEFAULTS = {"prompt_minutes": 10, "slow_factor": 2, "slow_minutes": 90, "wait_minutes": 60, "finished_minutes": 5}

KEEP_LENGTHS = 20
MIN_LENGTHS = 5

_since = {}   # key -> when a pass first saw the condition
_open = {}    # key -> (kind, card, detail) of the warning already logged


def _clock():
    return time.time()


def thresholds():
    """(values, errors): a value that is not a positive number keeps its default and adds an error naming the key."""
    values, errors = dict(DEFAULTS), []
    for key in DEFAULTS:
        if key not in C.ACTIVITY:
            continue
        v = C.ACTIVITY[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errors.append(f"[activity] {key} must be a positive number")
        else:
            values[key] = v
    return values, errors


def cmd_activity(a):
    values, errors = thresholds()
    for e in errors:
        print(e, file=sys.stderr)
    if a.json:
        jsonout.emit(values)
    else:
        for k, v in values.items():
            print(f"{k} = {v}")
    if errors:
        raise SystemExit(2)


def observe(seen):
    """`seen`: {key: (kind, card, limit, detail[, since])} for every condition present on this pass.

    `limit` is a thresholds() key or a number of minutes; `since` (epoch seconds) replaces the first-seen clock."""
    now, limits = _clock(), thresholds()[0]
    for key, (kind, card, limit, detail, *since) in seen.items():
        start = since[0] if since else _since.setdefault(key, now)
        minutes = (now - start) / 60
        if minutes > limits.get(limit, limit) and key not in _open:
            events.emit(kind, card, level="warning", minutes=int(minutes), **detail)
            _open[key] = (kind, card, detail)
    for key in [k for k in _open if k not in seen]:
        kind, card, detail = _open.pop(key)
        events.emit(f"{kind}_cleared", card, level="info", **detail)
    for key in [k for k in _since if k not in seen]:
        del _since[key]


def record_length(st, stage, w):
    """Keep how long a finished stage ran, once per worker session; the last KEEP_LENGTHS per stage."""
    if not w.get("started_at"):
        return
    lengths = st.setdefault("stage_lengths", {}).setdefault(stage, [])
    if any(sid == w.get("session_id") for sid, _ in lengths):
        return
    lengths.append([w.get("session_id"), (_clock() - parse_iso(w["started_at"])) / 60])
    del lengths[:-KEEP_LENGTHS]


def stage_slow(st, c, w):
    """{key: entry} for a stage attempt that may be running much longer than usual; the clock is its started_at."""
    started = w.get("started_at")
    if not started:
        return {}
    stage = w["stage"]
    lengths = [m for _, m in st.get("stage_lengths", {}).get(stage, [])]
    if len(lengths) >= MIN_LENGTHS:
        median = statistics.median(lengths)
        limit, detail = thresholds()[0]["slow_factor"] * median, {"stage": stage, "median": round(median)}
    else:
        limit, detail = "slow_minutes", {"stage": stage}
    if w.get("limit_hit"):
        detail["limit_screen"] = True
    return {f"slow:{c['id']}:{stage}:{started}": ("stage_slow", c["id"], limit, detail, parse_iso(started))}
