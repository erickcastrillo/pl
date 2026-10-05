"""Profile failover: a profile that ran out of usage credits is parked, cards move to the other."""
import contextlib
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pl import config as C
from pl import alerts, events, harnesses
from pl.agents import pane_exists
from pl.board import lists
from pl.util import notify, now_iso, parse_iso


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


def reset_at(screen, now):
    """The local datetime the screen says the limit resets: the next time that date and time come round, or None."""
    m = C.RESET_RE.search(screen)
    if not m:
        return None
    g = m.groupdict()
    if g["h24"] is not None:
        hour, minute = int(g["h24"]), int(g["m24"])
    else:
        hour, minute = int(g["h"]) % 12 + (12 if g["ap"].lower() == "pm" else 0), int(g["m"] or 0)
    mon, day = g["mon"] or g["mon2"], g["day"] or g["day2"]
    try:
        if mon is None:   # no date: the next time this clock time comes round
            t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return t if t > now else t + timedelta(days=1)
        month = MONTHS.index(mon[:3].lower()) + 1
        t = now.replace(month=month, day=int(day), hour=hour, minute=minute, second=0, microsecond=0)
        return t if t > now else t.replace(year=now.year + 1)
    except ValueError:   # 25:00, Feb 30: no usable time
        return None


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def mark_exhausted(profile, screen):
    """Park a profile until the reset time printed on the agent's screen, or LIMIT_COOLDOWN from now."""
    until = time.time() + C.LIMIT_COOLDOWN
    m = C.RESET_RE.search(screen)
    if t := reset_at(screen, datetime.now()):
        until = t.timestamp() + 60
    d = C.STATE_DIR / "limits"   # the screen that tripped it, for diagnosis; only ever in this profile's own state folder
    if not d.resolve().is_relative_to((Path.home() / ".claude-attention").resolve()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        f = d / f"pl-limit-{profile}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        os.close(os.open(f, os.O_WRONLY | os.O_CREAT, 0o600))
        f.write_text(screen)
    hit = C.LIMIT_RE.search(screen) or m
    iv = check_intervals()
    first = None if t or not iv else _iso(time.time() + iv[0])   # a printed reset time is trusted: no check before it
    rec = {"exhausted_at": now_iso(), "reason": hit.group(0) if hit else "usage limit",
           "until": _iso(until), "next_check": first, "checks": 0}
    _update(C.PROFILE_STATE, lambda st: st.__setitem__(profile, rec))
    if profile in C.PROFILES:
        entry = {"until": rec["until"], "reason": rec["reason"], "by_profile": C.PROFILE_NAME, "next_check": first, "checks": 0}
        _update_machine(lambda data: data.__setitem__(_folder(profile), entry))
    return rec["until"]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def parking(name):
    """This account's parking record: the profile's own, with the machine-wide entry's fields over it (the
    machine entry carries the shared health check)."""
    return {**(profile_state().get(name) or {}), **machine_entry(name)}


# ---------- health checks of parked accounts ----------

CHECK_PROMPT = "Reply with the word ok."
CHECK_TIMEOUT = 60      # seconds the check call may take
CHECK_SLACK = 300       # a limited account stays parked this long past its next check, so the check comes first
DEFAULT_CHECKS = [10, 30, 60]   # minutes; [dispatch] account_checks; the last one repeats
# The one cheap non-interactive call per harness that tells a working account from a limited one ({bin}: the
# harness's own program). claude 2.1.289: print mode, no tools, no skills, no saved session. codex 0.150.1: exec in a
# read-only sandbox, no session file. Antigravity prints no limit message pl knows, so it keeps the plain timer.
CHECKS = {"claude": ["{bin}", "-p", "{prompt}", "--no-session-persistence", "--disable-slash-commands", "--tools", ""],
          "codex": ["{bin}", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "{prompt}"]}


def check_intervals():
    """Seconds between checks of a parked account: [dispatch] account_checks (minutes), else 10, 30, 60. [] is off."""
    v = C.DISPATCH.get("account_checks", DEFAULT_CHECKS)
    if not (isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in v)):
        v = DEFAULT_CHECKS
    return [int(x * 60) for x in v]


def checkable(name):
    """True when pl can check this account by itself: checks are on and its harness has a check call."""
    return bool(check_intervals()) and harnesses.account_harness(name).name in CHECKS


def _due(rec, now):
    if "next_check" not in rec:
        return True       # parked before checks existed: check it now
    return bool(rec["next_check"]) and parse_iso(rec["next_check"]) <= now


def _check_lock_path(name):
    from pl.manager import machine_dir
    return machine_dir() / "checks" / (hashlib.sha256(_folder(name).encode()).hexdigest()[:16] + ".lock")


@contextlib.contextmanager
def _check_lock(name):
    """One check per account folder on the machine: yields False when another process is checking it."""
    import fcntl
    f = _check_lock_path(name)
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _probe(argv, env, timeout, cwd):
    """The check call, through this module's runner (harnesses._run). No shell; nothing typed into a terminal."""
    return harnesses._run(argv, env=env, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)


def _call(name, h):
    """Run the check: ("ok" | "limited" | "error", short result text, reset datetime or None). The output itself
    is never stored."""
    argv = [t.replace("{bin}", h.bin).replace("{prompt}", CHECK_PROMPT) for t in CHECKS[h.name]]
    env = dict(os.environ)
    if h.env_var:
        env[h.env_var] = str(C.PROFILES[name])
    try:
        r = _probe(argv, env, CHECK_TIMEOUT, str(C.STATE_DIR))
    except subprocess.TimeoutExpired:
        return "error", f"error: no answer in {CHECK_TIMEOUT} s", None
    except OSError as e:
        return "error", f"error: cannot run {h.bin} ({e.strerror or e})"[:120], None
    out = f"{r.stdout or ''}\n{r.stderr or ''}"
    if harnesses.limit_hit(h, out) or C.LIMIT_RE.search(out):
        t = reset_at(out, datetime.now())
        return "limited", f"still limited, resets {_iso(t.timestamp())[:16].replace('T', ' ')} UTC" if t else "still limited", t
    if r.returncode == 0:
        return "ok", "ok", None
    return "error", f"error: exit {r.returncode}", None


def _record(name, fields):
    """Write the check fields into the machine entry and this profile's own record (when it has one)."""
    folder = _folder(name)

    def machine(data):
        e = data.get(folder) or {"reason": "usage limit", "by_profile": C.PROFILE_NAME}
        data[folder] = {**e, **fields}
    _update_machine(machine)
    if name in profile_state():
        _update(C.PROFILE_STATE, lambda st: st.__setitem__(name, {**st[name], **fields}) if name in st else None)


def check_note(name):
    """One line on this parked account's checks: the last one and its result, and when the next comes."""
    if not checkable(name):
        return "not checked: it waits for its timer"
    r = parking(name)
    last = f"last check {r['checked_at'][11:16]} UTC: {r.get('check_result', '?')}" if r.get("checked_at") else "not checked yet"
    if "next_check" not in r:
        nxt = "next check on the next pass"
    elif r["next_check"]:
        nxt = f"{'next' if r.get('checked_at') else 'first'} check {r['next_check'][11:16]} UTC"
    else:
        nxt = "no check before its reset time"
    return f"{last}; {nxt}"


def check_parked():
    """The dispatcher's health check: for each parked account of this profile whose next check is due, one cheap
    call through its harness. Works: un-parked everywhere. Still limited: parked to the printed reset, else to the
    next check (10, 30, then every 60 min). An error leaves the timer as it was and tries again at the next interval.
    Returns [(account, "ok" | "limited" | "error")] for the checks it ran."""
    out = []
    for name in sorted(exhausted_profiles()):
        if name not in C.PROFILES or not checkable(name) or not _due(parking(name), time.time()):
            continue
        with _check_lock(name) as mine:
            if not mine or name not in exhausted_profiles() or not _due(parking(name), time.time()):
                continue   # another profile's dispatcher is checking it, or just did
            out.append((name, _check(name)))
    return out


def _check(name):
    h = harnesses.account_harness(name)
    kind, result, reset = _call(name, h)
    now, rec = time.time(), parking(name)
    events.emit("account_check", None, account=name, result=kind)
    if kind == "ok":
        unpark_machine([name])
        _update(C.PROFILE_STATE, lambda st: st.pop(name, None))
        alerts.resolve(f"account_parked:{name}")
        print(f"account {name} works again: un-parked")
        return kind
    iv = check_intervals()
    n = int(rec.get("checks") or 0) + 1
    fields = {"checked_at": _iso(now), "check_result": result, "checks": n}
    if reset:
        fields.update(until=_iso(reset.timestamp() + 60), next_check=None)
    else:
        nxt = now + iv[min(n, len(iv) - 1)]
        fields["next_check"] = _iso(nxt)
        if kind == "limited":
            fields["until"] = _iso(max(parse_iso(rec.get("until") or ""), nxt + CHECK_SLACK))
    _record(name, fields)
    until = parking(name).get("until") or ""
    fix = f"parked until {until[11:16]} UTC; {check_note(name)}; pl accounts --reset {name} un-parks it"
    print(f"account {name} checked: {result}")
    if why := alerts.open(f"account_parked:{name}", "warn", f"Account {name} is parked: usage limit", fix):
        notify(alerts.headline(why, f"Account {name} is still parked"), check_note(name))
    return kind


def healthy_profile(preferred, all_cards, harness=None, skip=None):
    """The card's own profile if it is not parked, else the least-loaded profile that is not parked, else None.
    With harness, only accounts that run that harness count; skip is never picked."""
    bad = exhausted_profiles() | {skip}
    if harness:
        bad = bad | {p for p in C.PROFILES if harnesses.account_harness(p).name != harness}
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
