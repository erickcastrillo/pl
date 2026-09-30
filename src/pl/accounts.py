"""Profile failover: a profile that ran out of usage credits is parked, cards move to the other."""
import json
import time
from datetime import datetime, timedelta, timezone

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


def exhausted_profiles():
    now = time.time()
    return {p for p, r in profile_state().items() if parse_iso(r.get("until") or "") > now}


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
