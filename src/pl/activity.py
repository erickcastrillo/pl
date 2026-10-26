"""pl activity: the activity feed from the command line, and sample entries to try the Activity tab's filters."""
import json

from pl import events
from pl import config as C  # noqa: F401  (profile settings are loaded before commands run)
from pl import trackers
from pl.standup import parse_since
from pl.util import BadInput

TAB_EVENTS = 500       # the Activity tab loads this many, so counts here match it
DETAIL_CHARS = 80
DEMO = (("demo_info", "info"), ("demo_warning", "warning"), ("demo_error", "error"))


def select(evs, level=None, kind=None, since=None):
    return [e for e in evs if (not level or events.level(e) == level) and (not kind or e["kind"] == kind)
            and (since is None or e["_t"] >= since)]


def _detail(e):
    key = next((k for k in ("message", "error", "reason", "text") if e.get(k)), None)
    return str(e[key])[:DETAIL_CHARS].replace("\n", " ") if key else ""


def _line(e):
    cells = [e["ts"][:19].replace("T", " "), events.level(e), e["kind"], e.get("card") or "-", _detail(e)]
    return " ".join(c for c in cells if c) + (" demo" if e.get("demo") else "")


def _since(text):
    try:
        return parse_since(text)
    except SystemExit as e:
        raise BadInput(str(e.code).replace("pl standup:", "pl activity:", 1)) from None


def _demo(clear):
    if clear:
        print(f"removed {events.clear_demo()} demo entries")
    elif trackers.dry_run():
        print("pl activity: dry-run is on; no demo entries written")
    else:
        for kind, level in DEMO:
            events.emit(kind, level=level, demo=True, message=f"sample {level} entry")
        print(f"wrote {len(DEMO)} demo entries")


def cmd_activity(a):
    if a.action == "demo":
        return _demo(a.clear)
    if a.action == "thresholds":
        from pl import slowwork
        return slowwork.cmd_activity(a)
    if a.level and a.level not in events.LEVELS:
        raise BadInput(f"pl activity: --level takes info, warning or error, not {a.level!r}")
    since = _since(a.since) if a.since else None
    evs = events._read(demo=True)
    if since is None:
        evs = evs[-TAB_EVENTS:]
    for e in select(evs, a.level, a.kind, since)[::-1]:
        if a.json:
            print(json.dumps({**{k: v for k, v in e.items() if k != "_t"}, "level": events.level(e)}))
        else:
            print(_line(e))
