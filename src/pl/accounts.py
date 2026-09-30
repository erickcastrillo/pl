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


def _machine():
    """The machine-wide parking file of pl manager's folder: {resolved account folder: {until, reason, by_profile}}."""
    from pl.manager import machine_dir
    try:
        d = json.loads((machine_dir() / "accounts.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _update_machine(change):
    """Read, change and atomically rewrite accounts.json under a flock, so two profiles never lose a write."""
    import fcntl
    from pl.manager import machine_dir
    d = machine_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / "accounts.json.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _machine()
        change(data)
        tmp = d / f".accounts.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, d / "accounts.json")


def unpark_machine(names):
    """Clear the machine entries of these accounts' folders (pl accounts --reset)."""
    folders = {_folder(n) for n in names if n in C.PROFILES}
    if folders and _machine():
        _update_machine(lambda data: [data.pop(f, None) for f in folders])


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
    st = profile_state()
    hit = C.LIMIT_RE.search(screen) or m
    st[profile] = {"exhausted_at": now_iso(), "reason": hit.group(0) if hit else "usage limit",
                   "until": datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")}
    C.ATTN.mkdir(exist_ok=True)
    C.PROFILE_STATE.write_text(json.dumps(st, indent=1))
    if profile in C.PROFILES:
        entry = {"until": st[profile]["until"], "reason": st[profile]["reason"], "by_profile": C.PROFILE_NAME}
        _update_machine(lambda data: data.__setitem__(_folder(profile), entry))
    return st[profile]["until"]


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
