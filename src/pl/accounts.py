"""Profile failover: a profile that ran out of usage credits is parked, cards move to the other."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pl import config as C
from pl import harnesses
from pl.agents import pane_exists
from pl.board import lists
from pl.util import now_iso, parse_iso


def profile_state():
    try:
        return json.loads(C.PROFILE_STATE.read_text())
    except Exception:
        return {}


def _folder(name):
    return str(Path(C.PROFILES[name]).expanduser().resolve())


def _load(f):
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _machine():
    """The machine-wide parking file of pl manager's folder: {resolved account folder: {until, reason, by_profile}}."""
    from pl.manager import machine_dir
    return _load(machine_dir() / "accounts.json")


def _update(f, change):
    """Read, change and atomically rewrite the JSON file f under a flock on f.lock, so two profiles never lose a write."""
    import fcntl
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(f.parent / f"{f.name}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(f)
        change(data)
        tmp = f.parent / f".{f.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, f)


def _update_machine(change):
    from pl.manager import machine_dir
    _update(machine_dir() / "accounts.json", change)


def machine_entry(name):
    """The machine-wide parking entry of this account's folder ({until, reason, by_profile}), or {}."""
    return (_machine().get(_folder(name)) or {}) if name in C.PROFILES else {}


def unpark_machine(names):
    """pl accounts --reset: clear these accounts' folders machine-wide and in every profile's own file."""
    from pl.profiles import list_profiles
    folders = {_folder(n) for n in names if n in C.PROFILES}
    if not folders:
        return
    if _machine():
        _update_machine(lambda data: [data.pop(f, None) for f in folders])
    for row in list_profiles():
        same = [n for n, d in row["accounts"].items() if str(Path(d).resolve()) in folders]
        f = Path(row["dir"]) / "state" / "pl-profiles.json"
        if any(n in _load(f) for n in same):
            _update(f, lambda st: [st.pop(n, None) for n in same])


def exhausted_profiles():
    """This profile's parked accounts, plus each account whose folder another profile parked machine-wide."""
    now = time.time()
    own = {p for p, r in profile_state().items() if parse_iso(r.get("until") or "") > now}
    m = _machine()
    return own | {n for n in C.PROFILES if parse_iso((m.get(_folder(n)) or {}).get("until") or "") > now}


def mark_exhausted(profile, screen):
    """Park a profile until the reset time printed on the agent's screen, or LIMIT_COOLDOWN from now."""
    until = time.time() + C.LIMIT_COOLDOWN
    m = C.RESET_RE.search(screen)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        t = datetime.now().replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if t.timestamp() < time.time():
            t += timedelta(days=1)
        until = t.timestamp() + 60
    C.ATTN.mkdir(exist_ok=True)
    (C.ATTN / f"pl-limit-{profile}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt").write_text(screen)  # the screen that tripped it, for diagnosis
    hit = C.LIMIT_RE.search(screen) or m
    rec = {"exhausted_at": now_iso(), "reason": hit.group(0) if hit else "usage limit",
           "until": datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")}
    _update(C.PROFILE_STATE, lambda st: st.__setitem__(profile, rec))
    if profile in C.PROFILES:
        entry = {"until": rec["until"], "reason": rec["reason"], "by_profile": C.PROFILE_NAME}
        _update_machine(lambda data: data.__setitem__(_folder(profile), entry))
    return rec["until"]


def healthy_profile(preferred, all_cards):
    """The card's own profile if it is not parked, else the least-loaded profile that is not parked, else None."""
    bad = exhausted_profiles()
    if preferred in C.PROFILES and preferred not in bad:
        return preferred
    ok = [p for p in C.PROFILES if p not in bad]
    return next_profile(all_cards, allowed=ok) if ok else None


def screen_hit_limit(pane, harness=None):
    """The agent's last screen lines when they show the harness's own usage-limit message (default Claude), else None."""
    if not pane_exists(pane):
        return None
    out = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-60"]).stdout
    return out if harnesses.limit_hit(harness or harnesses.get("claude"), out) else None



def next_profile(all_cards, allowed=None):
    counts = {p: 0 for p in C.PROFILES}
    done = lists().get("Done", {}).get("id")
    for c in all_cards:
        m = c.get("metadata") or {}
        if m.get("pipeline_mode") == "auto" and c.get("list_id") != done and m.get("profile") in counts:
            counts[m["profile"]] += 1
    pool = allowed or list(C.PROFILES)
    if not pool:
        raise SystemExit(f"pl: no harness accounts: add one under [accounts.<name>] in {C.path() or 'config.toml'}")
    return min(pool, key=lambda p: counts.get(p, 0))   # ties: the first account listed
