"""What's new: one entry per feature, oldest first. The console shows the entries newer than the last one it showed
(kept in <profile>/state/whatsnew.json) once after an upgrade; the palette shows them all; `pl whatsnew` prints them.
A new feature adds one file to whatsnew.d named with the current UTC date and time (YYYY-MM-DD-HHMM-<slug>.toml)."""
import json
import os
import re
import tomllib
from pathlib import Path

from pl import config as C
from pl import jsonout

DIR = Path(__file__).with_name("whatsnew.d")
LAST_MIGRATED_FILE = "2026-10-26-0086"   # files up to this name carry their old <date>.<n> key as id
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.toml$")
_FIELDS = ("title", "where", "try")


def _entry(path):
    """(entry, None) for a good entry file, else (None, what is wrong)."""
    m = NAME_RE.match(path.name)
    if not m:
        return None, "name must be YYYY-MM-DD-HHMM-<slug>.toml"
    date, hhmm = m.groups()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return None, f"does not parse ({e})"
    for k in data:
        if k not in (*_FIELDS, "id"):
            return None, f"unknown key {k}"
    for k in _FIELDS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip() or "\n" in v:
            return None, f"{k} must be a non-empty one-line string"
    if f"{date}-{hhmm}" <= LAST_MIGRATED_FILE:
        want = f"{date}.{hhmm[2:]}"
        if data.get("id") != want:
            return None, f"a file named {LAST_MIGRATED_FILE} or earlier must carry id = \"{want}\""
    else:
        if "id" in data:
            return None, "id is not allowed on a new file (the file name is the id)"
        if int(hhmm[:2]) > 23 or int(hhmm[2:]) > 59:
            return None, f"{hhmm} is not a time of day"
    return {"key": data.get("id", path.stem), "title": data["title"], "where": data["where"], "try": data["try"]}, None


def _order(key):
    """Sort key that puts an old <date>.<n> key before the files of that day that follow it."""
    return re.sub(r"\.(\d+)$", lambda m: f"-{int(m[1]):04d}", key)


def load(d=DIR):
    """The entries of every good file in d, in file-name order. A bad file is skipped; problems() names it."""
    out = [_entry(p)[0] for p in sorted(d.glob("*.toml"))]
    return [e for e in out if e]


def problems(d=DIR):
    """One "<file name>: <problem>" per bad file or duplicate id."""
    seen = set()
    out = []
    for p in sorted(d.glob("*.toml")):
        e, problem = _entry(p)
        if problem:
            out.append(f"{p.name}: {problem}")
        elif e["key"] in seen:
            out.append(f"{p.name}: duplicate id {e['key']}")
        if e:
            seen.add(e["key"])
    return out


ENTRIES = load()


def _path():
    return C.STATE_DIR / "whatsnew.json"


def last_shown():
    """The key of the newest entry shown, or None (never shown, or a file that does not read)."""
    try:
        v = json.loads(_path().read_text()).get("last")
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return v if isinstance(v, str) else None


def unseen():
    last = last_shown()
    return [e for e in ENTRIES if last is None or _order(e["key"]) > _order(last)]


def mark_seen(key=None):
    """Remember key (default: the newest entry) as shown. A state folder that cannot be written is not an error."""
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        tmp = _path().with_name(f".whatsnew.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"last": key or ENTRIES[-1]["key"]}))
        os.replace(tmp, _path())
    except (OSError, TypeError):
        pass


def text(entries):
    return "\n\n".join(f"{e['title']}\n  where: {e['where']}\n  try:   {e['try']}" for e in entries)


def cmd_whatsnew(argv=None):
    if "--json" in (argv or []):
        jsonout.emit({"entries": ENTRIES})
        return 0
    print("What's new in pl\n\n" + text(ENTRIES))
    return 0
