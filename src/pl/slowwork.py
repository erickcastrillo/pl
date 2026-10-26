"""Slow-work warnings: one `warning` event per stuck condition, one `info` <kind>_cleared event when it ends.

Episode state lives in memory: after a dispatcher restart an open condition may be logged again."""
import sys
import time

from pl import config as C
from pl import events, jsonout

DEFAULTS = {"prompt_minutes": 10, "slow_factor": 2, "slow_minutes": 90, "wait_minutes": 60, "finished_minutes": 5}

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
    """`seen`: {key: (kind, card, limit_key, detail)} for every condition present on this pass."""
    now, limits = _clock(), thresholds()[0]
    for key, (kind, card, limit_key, detail) in seen.items():
        start = _since.setdefault(key, now)
        minutes = (now - start) / 60
        if minutes > limits[limit_key] and key not in _open:
            events.emit(kind, card, level="warning", minutes=int(minutes), **detail)
            _open[key] = (kind, card, detail)
    for key in [k for k in _open if k not in seen]:
        kind, card, detail = _open.pop(key)
        events.emit(f"{kind}_cleared", card, level="info", **detail)
    for key in [k for k in _since if k not in seen]:
        del _since[key]
