"""Loop schedules (`every`) and quiet hours (`hours`), in local time."""
import re
from datetime import datetime, timedelta

FORMS = ("30m", "hourly", "daily HH:MM", "weekdays HH:MM", "cron:<expr>")
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_MAX_LOOKAHEAD_MINUTES = 366 * 24 * 60


def _bad(what: str, text) -> ValueError:
    return ValueError(f"{what} {text!r} is not valid; use one of: {', '.join(FORMS)}")


def _clock(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        return int(m[1]), int(m[2])
    return None


def _cron_field(text: str, lo: int, hi: int) -> set[int] | None:
    values: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if not m or (m[2] is not None and int(m[2]) < 1):
            return None
        if m[1] == "*":
            start, end = lo, hi
        else:
            start, _, stop = m[1].partition("-")
            start = int(start)
            end = int(stop) if stop else (hi if m[2] else start)
        if not lo <= start <= end <= hi:
            return None
        values.update(range(start, end + 1, int(m[2] or 1)))
    return values


def parse_every(text) -> dict:
    text = str(text or "").strip()
    if (m := re.fullmatch(r"(\d+)m", text)) and 1 <= int(m[1]) <= 1440:
        return {"kind": "minutes", "minutes": int(m[1])}
    if text == "hourly":
        return {"kind": "minutes", "minutes": 60}
    kind, _, clock = text.partition(" ")
    if kind in ("daily", "weekdays") and (hm := _clock(clock.strip())):
        return {"kind": kind, "at": hm}
    if text.startswith("cron:"):
        fields = text[5:].split()
        sets = [_cron_field(f, *r) for f, r in zip(fields, _CRON_RANGES)]
        if len(fields) == 5 and all(s is not None for s in sets):
            return {"kind": "cron", "fields": sets}
    raise _bad("every", text)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def in_words(text) -> str:
    """A schedule as a person would say it; text that is not a schedule comes back as it is."""
    text = str(text or "").strip()
    if not text:
        return "always on"
    try:
        spec = parse_every(text)
    except ValueError:
        return text
    kind = spec["kind"]
    if kind == "minutes":
        n = spec["minutes"]
        if n == 60:
            return "every hour"
        return f"every {_plural(n // 60, 'hour')}" if n % 60 == 0 else f"every {_plural(n, 'minute')}"
    if kind == "cron":
        return "custom: " + text[5:].strip()
    hour, minute = spec["at"]
    return f"{'every day' if kind == 'daily' else 'weekdays'} at {hour:02d}:{minute:02d}"


def _cron_matches(fields, dt: datetime) -> bool:
    minute, hour, dom, mon, dow = fields
    return (dt.minute in minute and dt.hour in hour and dt.day in dom and dt.month in mon
            and (dt.weekday() + 1) % 7 in dow)


def next_run(spec: dict, after: datetime) -> datetime:
    kind = spec["kind"]
    if kind == "minutes":
        return after.replace(second=0, microsecond=0) + timedelta(minutes=spec["minutes"])
    if kind in ("daily", "weekdays"):
        hour, minute = spec["at"]
        day = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        while day <= after or (kind == "weekdays" and day.weekday() >= 5):
            day += timedelta(days=1)
        return day
    probe = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_LOOKAHEAD_MINUTES):
        if _cron_matches(spec["fields"], probe):
            return probe
        probe += timedelta(minutes=1)
    raise ValueError("cron expression never fires within 366 days")


def parse_hours(text) -> dict:
    m = re.fullmatch(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})(?: (weekdays))?", str(text or "").strip())
    start, end = (_clock(m[1]), _clock(m[2])) if m else (None, None)
    if not (start and end):
        raise ValueError(f"hours {text!r} is not valid; use HH:MM-HH:MM or HH:MM-HH:MM weekdays")
    return {"start": start, "end": end, "weekdays": bool(m[3])}


def in_hours(hours: dict, dt: datetime) -> bool:
    if hours["weekdays"] and dt.weekday() >= 5:
        return False
    now = (dt.hour, dt.minute)
    start, end = hours["start"], hours["end"]
    return start <= now < end if start <= end else now >= start or now < end
