"""state/summary.json: what a dispatcher pass publishes. Counts and ids only, never card text."""
import json
import time

from pl import alerts, board
from pl import config as C
from pl.util import mask, parse_iso

VERSION = 1
STALE_AFTER = 3   # intervals without a pass before a reader calls the dispatcher stopped


def _path():
    return C.STATE_DIR / "summary.json"


def _failed_cards():
    return {k.split(":")[1] for a in alerts.listing() if (k := a["key"]).startswith("stage_failed:")}


def _needs_you(c, col, failed):
    gated = col == C.STAGE_DONE_AT["plan"] or col == "Manual" or c["id"] in failed
    spec = col == "Spec ready" and (C.GATES or {}).get("spec") and not (c.get("metadata") or {}).get("spec_approved_at")
    return bool(gated or spec)


def build(all_cards, reg, ok_at):
    from pl import dispatch   # dispatch imports this module
    cols = {c["id"]: dispatch.col_name(c["list_id"]) for c in all_cards}
    failed = _failed_cards()
    waiting = [c for c in all_cards if _needs_you(c, cols[c["id"]], failed)]
    oldest = min(waiting, key=lambda c: parse_iso(c.get("updated_at") or ""), default=None)
    return {
        "version": VERSION,
        "name": C.PROFILE_NAME,
        "pipeline": (getattr(C, "PIPELINE", None) or {}).get("name", "engineering"),
        "running": True,
        "paused": bool(dispatch.paused()),
        "at": time.time(),
        "last_good_refresh": ok_at,
        "columns": [{"title": t, "cards": sum(1 for v in cols.values() if v == t)} for t in C.COLUMNS],
        "needs_you": len(waiting),
        "live_agents": dispatch.busy_agents(reg),
        "oldest_wait": {"card": oldest["id"], "since": oldest.get("updated_at")} if oldest else None,
        "alerts": len(alerts.listing()),
    }


def write(d):
    board._save(_path(), d)


def mark_failed(message):
    d = read_file(_path()) or {}
    write({**d, "version": VERSION, "name": C.PROFILE_NAME, "running": True, "at": time.time(), "error": mask(message)[:200]})


def read_file(path):
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def read(profile_dir, interval=None, now=None):
    """The profile's summary, or None. With the dispatcher's interval, an old one reads as stopped."""
    d = read_file(profile_dir / "state" / "summary.json")
    if d and interval and (now or time.time()) - (d.get("at") or 0) > STALE_AFTER * interval:
        d["running"] = False
    return d
