"""The GitHub GraphQL budget, shared by every pl process of every profile signed in as the same GitHub user.

GitHub gives each user 5,000 GraphQL points an hour, whatever profile, console or agent spends them. pl's own GraphQL
queries ask for rateLimit{cost remaining resetAt limit} (free), so every answer says how much is left. That reading and
what each profile spent in the current window live in one machine-wide ledger per GitHub user and host:
<machine dir>/github/<login>@<host>.json. Other users and other trackers never read it.

The rules, from most to least protected:
- writes (issue edits, stage moves, new cards) stop only below FLOOR points;
- commands and agents (pl card, pl section, pl move) stop below RESERVE of the limit;
- periodic reads (the dispatcher's pass, the console's refresh) stop below PERIODIC of the limit, and below LOW of the
  limit each profile keeps to its fair share: the limit split evenly among the profiles that spent points this window.
A stop lasts until GitHub's reset: before it the points left only go down, so an old reading is never too strict.
No subprocess here: github._gh calls check() before and record() after each gh call.
"""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from pl import config as C



def _clock():
    """github's clock, which tests fake: the back-off and the budget agree on what time it is."""
    from pl.trackers import github
    return github._clock()


FLOOR = 50              # points: below this, nothing; GitHub would refuse it anyway
RESERVE = 0.05          # share of the limit kept for writes: commands and agents stop below it
PERIODIC = 0.20         # periodic reads stop below this share of the limit
LOW = 0.50              # below this share left, each profile keeps to its fair share for periodic reads
PERIODIC_ROLES = ("dispatcher", "console")
ROLE = {"name": "command"}   # who this process is: "dispatcher", "console", or the pl command it runs ("pl card")


def set_role(name):
    ROLE["name"] = name


def _dir():
    from pl import manager
    return manager.machine_dir() / "github"


def _folder(d=None):
    """The gh sign-in folder this profile uses: its gh_config_dir, else GH_CONFIG_DIR, else gh's default."""
    d = d or getattr(C, "GH_CONFIG_DIR", None) or os.environ.get("GH_CONFIG_DIR")
    return str(Path(d).expanduser()) if d else "default"


def _host():
    return os.environ.get("GH_HOST") or "github.com"


def _load(path):
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def account(folder=None):
    """The ledger's key: the GitHub login seen for this sign-in folder and host, else a hash of the folder."""
    where = f"{_folder(folder)}@{_host()}"
    login = _load(_dir() / "logins.json").get(where)
    if isinstance(login, str) and login.replace("-", "").isalnum():
        return f"{login}@{_host()}"
    return "dir-" + hashlib.sha256(where.encode()).hexdigest()[:12] + f"@{_host()}"


def learn(login):
    """A GraphQL answer named the signed-in user: from now on this folder's ledger is that user's."""
    if not (isinstance(login, str) and login.replace("-", "").isalnum()):
        return
    key, path = f"{_folder()}@{_host()}", _dir() / "logins.json"
    try:
        with _locked():
            got = _load(path)
            if got.get(key) != login:
                _write(path, {**got, key: login})
    except OSError:
        pass


def _path(acct=None):
    return _dir() / f"{acct or account()}.json"


class _locked:
    """One writer at a time for the ledgers (processes of every profile write them)."""

    def __enter__(self):
        _dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        self.f = open(_dir() / ".lock", "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        self.f.close()
        return False


def _epoch(iso):
    from pl.util import parse_iso
    try:
        return parse_iso(iso) or None
    except (TypeError, ValueError):
        return None


def ledger(acct=None):
    """This user's ledger: {limit, remaining, reset, at, window, spent: {profile: points}, by: {"profile role": points}}."""
    return _load(_path(acct))


def record(points=1, rate=None):
    """Count points spent by this profile and role; rate is a GraphQL rateLimit object ({cost, remaining, resetAt,
    limit}) when the answer carried one, and then its cost is what was spent. A failure to write never fails the call."""
    prof, role = C.PROFILE_NAME or "-", ROLE["name"]
    try:
        with _locked():
            led = ledger()
            if isinstance(rate, dict) and isinstance(rate.get("remaining"), int):
                reset = _epoch(rate.get("resetAt")) or led.get("reset")
                points = int(rate.get("cost") or 0)
                led.update(limit=int(rate.get("limit") or led.get("limit") or 5000), remaining=rate["remaining"],
                           reset=reset, at=_clock())
            elif isinstance(led.get("remaining"), int):
                led["remaining"] = max(0, led["remaining"] - points)
            if led.get("window") != led.get("reset") or (led.get("reset") or 0) <= _clock():
                led.update(window=led.get("reset"), spent={}, by={})
            spent, by = led.setdefault("spent", {}), led.setdefault("by", {})
            spent[prof] = spent.get(prof, 0) + points
            by[f"{prof} {role}"] = by.get(f"{prof} {role}", 0) + points
            _write(_path(), led)
    except OSError:
        pass


def exhausted(until):
    """GitHub refused a call for the rate limit until `until`: every profile of this user waits for that reset."""
    try:
        with _locked():
            led = ledger()
            if led.get("window") != until:
                led.update(window=until, spent={}, by={})
            led.update(remaining=0, reset=until, at=_clock(), limit=led.get("limit") or 5000)
            _write(_path(), led)
    except OSError:
        pass


def fair_share(led):
    """Points each profile may spend on periodic reads this window: the limit split among the profiles that spent."""
    n = max(1, len([p for p, v in (led.get("spent") or {}).items() if v]))
    return int((led.get("limit") or 5000) / n)


def check(kind="read"):
    """None when this call may go ahead, else (until, why). kind "write" for a change on GitHub, else "read"."""
    led, now = ledger(), _clock()
    rem, lim, reset = led.get("remaining"), led.get("limit") or 5000, led.get("reset") or 0
    if not isinstance(rem, int) or reset <= now:
        return None
    if rem < FLOOR:
        return reset, f"GitHub GraphQL budget spent ({rem} of {lim} left)"
    if kind == "write":
        return None
    if rem < RESERVE * lim:
        return reset, f"{rem} of {lim} GraphQL points left: kept for writes"
    if ROLE["name"] not in PERIODIC_ROLES:
        return None
    if rem < PERIODIC * lim:
        return reset, f"{rem} of {lim} GraphQL points left: kept for agents and writes"
    mine, share = (led.get("spent") or {}).get(C.PROFILE_NAME or "-", 0), fair_share(led)
    if rem < LOW * lim and mine >= share:
        return reset, f"{C.PROFILE_NAME or 'this profile'} used {mine} of its {share}-point share"
    return None


def pace():
    """How much slower periodic reads go: 1 with half the budget or more left, 2 above a quarter, else 4."""
    led, now = ledger(), _clock()
    rem, lim = led.get("remaining"), led.get("limit") or 5000
    if not isinstance(rem, int) or (led.get("reset") or 0) <= now:
        return 1
    return 1 if rem >= LOW * lim else 2 if rem >= 0.25 * lim else 4


def summary(acct=None):
    """One line about this user's budget, or None before pl saw a reading: left, reset, this window's top spender."""
    led = ledger(acct)
    if not isinstance(led.get("remaining"), int) or (led.get("reset") or 0) <= _clock():
        return None
    by = led.get("by") or {}
    top = max(by, key=by.get) if by else None
    reset = time.strftime("%H:%M", time.localtime(led["reset"]))
    return (f"GitHub {(acct or account()).split('@')[0]}: {led['remaining']} of {led.get('limit') or 5000} GraphQL "
            f"points left, resets {reset}" + (f"; top this hour: {top} ({by[top]})" if top else ""))


def accounts():
    """Every ledger on this machine whose window has not ended: [(account, ledger)]."""
    out = []
    for p in sorted(_dir().glob("*@*.json")) if _dir().is_dir() else []:
        led = _load(p)
        if isinstance(led.get("remaining"), int) and (led.get("reset") or 0) > _clock():
            out.append((p.stem, led))
    return out


def profile_rows(acct_led):
    """{profile: (spent, share)} for one ledger: what each profile spent this window and its fair share."""
    led = acct_led
    return {p: (v, fair_share(led)) for p, v in sorted((led.get("spent") or {}).items())}


def usage_for(folder, profile):
    """What profile spent of its GitHub user's budget this window, for pl manager status; None before any reading."""
    acct = account(folder)
    led = ledger(acct)
    if not isinstance(led.get("remaining"), int) or (led.get("reset") or 0) <= _clock():
        return None
    return {"account": acct, "spent": (led.get("spent") or {}).get(profile, 0), "share": fair_share(led),
            "remaining": led["remaining"], "limit": led.get("limit") or 5000, "reset": led["reset"]}


def report():
    """`pl usage --github`: every GitHub user's budget this hour, per profile and per caller."""
    rows = accounts()
    if not rows:
        return "GitHub: no GraphQL reading this hour (pl records one with each board read)"
    out = []
    for acct, led in rows:
        reset = time.strftime("%H:%M", time.localtime(led["reset"]))
        age = int(_clock() - (led.get("at") or 0))
        out.append(f"{acct}: {led['remaining']} of {led.get('limit') or 5000} GraphQL points left, resets {reset} "
                   f"(read {age} s ago)")
        for p, (v, share) in profile_rows(led).items():
            out.append(f"  {p:<20} spent {v:>5}   fair share {share}")
        by = sorted((led.get("by") or {}).items(), key=lambda kv: -kv[1])
        if by:
            out.append("  by caller: " + ", ".join(f"{k} {v}" for k, v in by[:8]))
    return "\n".join(out)
