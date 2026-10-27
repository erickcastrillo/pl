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
No subprocess here: github._gh calls check() before and record() after each gh call; other modules pass their own
runner to gh(), which records after the call.
"""
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from pl import config as C
from pl import events



def _clock():
    """github's clock, which tests fake: the back-off and the budget agree on what time it is."""
    from pl.trackers import github
    return github._clock()


FLOOR = 50              # points: below this, nothing; GitHub would refuse it anyway
RESERVE = 0.05          # share of the limit kept for writes: commands and agents stop below it
PERIODIC = 0.20         # periodic reads stop below this share of the limit
LOW = 0.50              # below this share left, each profile keeps to its fair share for periodic reads
GAP = 0.10             # share of the limit: a bigger difference between pl and GitHub is flagged
STALE = 60              # seconds: an older reading is checked against GitHub before a call is held for it
PERIODIC_ROLES = ("dispatcher", "console")
ROLE = {"name": "command"}   # who this process is: "dispatcher", "console", or the pl command it runs ("pl card")


def set_role(name, profile=None):
    ROLE["name"], ROLE["profile"] = name, profile


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


_TOKENS = {}   # "<folder>@<host>" -> short hash of the token gh uses there, or None; never the token itself


def _ask_token():
    """The token gh uses, from `gh auth token` (which applies GH_TOKEN and the profile's folder); "" when it has none."""
    from pl.trackers import github
    r = github._run(["auth", "token"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _token_hash(folder=None):
    """12 hex characters of SHA-256 of this process's token, asked of gh once per sign-in folder; None without one."""
    if folder is not None and _folder(folder) != _folder():
        return None   # another profile's sign-in: this process cannot see its token
    key = f"{_folder()}@{_host()}"
    if key not in _TOKENS:
        try:
            tok = _ask_token()
        except (SystemExit, OSError):   # gh missing or timed out: the login ledger, not an error
            tok = ""
        _TOKENS[key] = hashlib.sha256(tok.encode()).hexdigest()[:12] if tok else None
    return _TOKENS[key]


def account(folder=None):
    """The ledger's key: the GitHub login seen for this sign-in folder (else a hash of the folder), then the short hash
    of the token, since each token has its own budget. Without a token: the login alone."""
    where = f"{_folder(folder)}@{_host()}"
    login = _load(_dir() / "logins.json").get(where)
    base = login if isinstance(login, str) and login.replace("-", "").isalnum() \
        else "dir-" + hashlib.sha256(where.encode()).hexdigest()[:12]
    return f"{base}.{h}@{_host()}" if (h := _token_hash(folder)) else f"{base}@{_host()}"


def _label(acct):
    """"acme-bot.0123abcd4567@github.com" -> "acme-bot@github.com (token 0123abcd4567)"; a key with no hash as it is."""
    name, _, host = acct.partition("@")
    login, dot, tok = name.partition(".")
    return f"{login}@{host} (token {tok})" if dot else acct


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


def record(points=1, rate=None, sub=None, acct=None):
    """Count points spent by this profile and role; rate is a GraphQL rateLimit object ({cost, remaining, resetAt,
    limit}) when the answer carried one, and then its cost is what was spent. Without one, an estimate counts only
    against a reading of this window. A failure to write never fails the call."""
    prof, role = ROLE.get("profile") or C.PROFILE_NAME or "-", ROLE["name"]
    try:
        with _locked():
            led = ledger(acct)
            if isinstance(rate, dict) and isinstance(rate.get("remaining"), int):
                reset = _epoch(rate.get("resetAt")) or led.get("reset")
                points = int(rate.get("cost") or 0)
                led.update(limit=int(rate.get("limit") or led.get("limit") or 5000), remaining=rate["remaining"],
                           reset=reset, at=_clock())
            elif isinstance(led.get("remaining"), int) and (led.get("reset") or 0) > _clock():
                led["remaining"] = max(0, led["remaining"] - points)
            else:
                return   # no reading this window: nothing to count against yet
            if led.get("window") != led.get("reset") or (led.get("reset") or 0) <= _clock():
                led.update(window=led.get("reset"), spent={}, by={}, subs={}, seen={}, rest={})
            spent, by = led.setdefault("spent", {}), led.setdefault("by", {})
            spent[prof] = spent.get(prof, 0) + points
            by[f"{prof} {role}"] = by.get(f"{prof} {role}", 0) + points
            if sub:
                subs = led.setdefault("subs", {}).setdefault(f"{prof} {role}", {})
                subs[sub] = subs.get(sub, 0) + points
            _write(_path(acct), led)
    except OSError:
        pass


def _rate(out):
    """The rateLimit object in a gh answer, or None."""
    if not isinstance(out, str) or not out.strip():
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    return d.get("rateLimit") or (d.get("data") or {}).get("rateLimit")


def _count(args, out, folder=None):
    from pl import ghwrap   # lazy: ghwrap imports this module
    from pl.trackers import github
    if ghwrap.kind(args) != "graphql" or github._is_search(args):
        return
    words = [a for a in args if re.fullmatch(r"[a-z][a-z-]*", a)]
    record(ghwrap.ESTIMATE, rate=_rate(out), sub=" ".join(words[:2]) or None, acct=account(folder) if folder else None)


def gh(run, args, **kw):
    """Run `gh ARGS` through the caller's own runner and count the call; the caller gets what the runner returned or raised."""
    r = None
    try:
        r = run(["gh", *args], **kw)
        return r
    finally:
        try:
            _count(args, getattr(r, "stdout", None), (kw.get("env") or {}).get("GH_CONFIG_DIR"))
        except Exception:
            pass   # counting never changes what the caller's gh call returned


def tracker_gh(args):
    """The tracker's own gh runner (timeout, env, SystemExit messages) with the call counted."""
    from pl.trackers import github
    return gh(lambda argv, **kw: github._run(argv[1:]), args)


def record_agent(acct, caller, sub, points, remaining, reset, rest=False):
    """Count an agent's gh call under its caller key and subcommand, against the reading taken after it; `rest` also counts it as answered through REST. A reading of
    another window starts the counts over. A failure to write never fails the call."""
    try:
        with _locked():
            led = ledger(acct)
            if led.get("window") != reset or (led.get("reset") or 0) <= _clock():
                led.update(window=reset, spent={}, by={}, subs={}, seen={}, rest={})
            led.update(remaining=remaining, reset=reset, at=_clock(), limit=led.get("limit") or 5000)
            prof = C.PROFILE_NAME or "-"
            spent, by = led.setdefault("spent", {}), led.setdefault("by", {})
            spent[prof] = spent.get(prof, 0) + points
            by[caller] = by.get(caller, 0) + points
            subs = led.setdefault("subs", {}).setdefault(caller, {})
            subs[sub] = subs.get(sub, 0) + points
            if rest:
                counts = led.setdefault("rest", {}).setdefault(caller, {})
                counts[sub] = counts.get(sub, 0) + 1
            _write(_path(acct), led)
    except OSError:
        pass


def stale(acct=None):
    """True when the ledger holds a reading of this window that is older than STALE seconds."""
    led, now = ledger(acct), _clock()
    return (led.get("reset") or 0) > now and now - (led.get("at") or 0) > STALE


def _top_callers(led, n=3):
    """The n callers that spent the most since the previous reading, as [(caller, points)]."""
    seen = led.get("seen") or {}
    gained = ((k, v - seen.get(k, 0)) for k, v in (led.get("by") or {}).items())
    return sorted(((k, v) for k, v in gained if v > 0), key=lambda kv: (-kv[1], kv[0]))[:n]


def refresh(remaining, reset, acct=None):
    """Take GitHub's own reading (free) over the ledger's, spending nothing. A gap of more than 10% of the limit is
    logged, with where this process signs in (never a token), so a second token can be traced."""
    try:
        with _locked():
            led = ledger(acct)
            limit = led.get("limit") or 5000
            if isinstance(led.get("remaining"), int) and abs(led["remaining"] - remaining) > 0.1 * limit:
                top = _top_callers(led)
                events.emit("github_budget_gap", None, ledger=led["remaining"], github=remaining, role=ROLE["name"],
                            profile=C.PROFILE_NAME, folder=_folder(),
                            token_env=bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")),
                            top_callers=[{"caller": k, "points": v} for k, v in top],
                            message=f"pl {led['remaining']} vs GitHub {remaining}; top callers: "
                                    + (", ".join(f"{k} {v}" for k, v in top) or "none recorded"))
            if led.get("window") != reset:
                led.update(window=reset, spent={}, by={}, subs={}, seen={}, rest={})
            led["seen"] = dict(led.get("by") or {})
            led.update(limit=limit, remaining=remaining, reset=reset, at=_clock())
            _write(_path(acct), led)
    except OSError:
        pass


def exhausted(until):
    """GitHub refused a call for the rate limit until `until`: every profile of this user waits for that reset."""
    try:
        with _locked():
            led = ledger()
            if (led.get("reset") or 0) <= _clock():   # a new window: what was spent before it no longer counts
                led.update(spent={}, by={}, subs={}, seen={}, rest={})
            led.update(remaining=0, reset=until, window=until, at=_clock(), limit=led.get("limit") or 5000)
            _write(_path(), led)
    except OSError:
        pass


def fair_share(led):
    """Points each profile may spend on periodic reads this window: the limit split among the profiles that spent."""
    n = max(1, len([p for p, v in (led.get("spent") or {}).items() if v]))
    return int((led.get("limit") or 5000) / n)


def check(kind="read", acct=None):
    """None when this call may go ahead, else (until, why). kind "write" for a change on GitHub, else "read"."""
    led, now = ledger(acct), _clock()
    rem, lim, reset = led.get("remaining"), led.get("limit") or 5000, led.get("reset") or 0
    if not isinstance(rem, int):
        return None
    if reset <= now:   # a new window; after a spent one, each process starts a random few seconds late (no herd)
        from pl.trackers import github
        late = reset + github._jitter(reset) if rem < FLOOR else reset
        return (late, "waiting a few seconds past the reset so every process does not call at once") if now < late else None
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
    who, _, token = _label(acct or account()).partition(" ")
    name = f"{who.split('@')[0]} {token}".strip()
    return (f"GitHub {name}: {led['remaining']} of {led.get('limit') or 5000} GraphQL "
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
    if not isinstance(led.get("remaining"), int) and "." not in acct.split("@")[0]:   # another folder's token is unseen
        base = acct.split("@")[0]
        led = max((l for a, l in accounts() if a.split("@")[0].split(".")[0] == base), default={},
                  key=lambda l: l.get("at") or 0)
    if not isinstance(led.get("remaining"), int) or (led.get("reset") or 0) <= _clock():
        return None
    return {"account": acct, "spent": (led.get("spent") or {}).get(profile, 0), "share": fair_share(led),
            "remaining": led["remaining"], "limit": led.get("limit") or 5000, "reset": led["reset"]}


def _github_side(acct, led, fresh, current):
    """The GitHub half of a report line: a fresh reading and any gap from pl's, or why there is none."""
    if acct != current:
        return "GitHub: no fresh reading: token not used by this process"
    if isinstance(fresh, str):
        return f"GitHub: no reading ({fresh})"
    rem, reset = fresh
    lim = (led or {}).get("limit") or 5000
    side = f"GitHub {rem} of {lim} left, resets {time.strftime('%H:%M', time.localtime(reset))}"
    if led and abs(led["remaining"] - rem) > GAP * lim:
        side += f" | GAP {abs(led['remaining'] - rem)} points, over {GAP:.0%} of {lim}"
    return side


def callers(led, fresh, mine):
    """([(caller, points)] most first, points spent outside pl or None) for one ledger. `fresh` is GitHub's reading
    (remaining, reset) of this process's token, or why there is none; `mine` says the ledger is that token's."""
    reading = mine and isinstance(fresh, tuple)
    rows = sorted(((k, v) for k, v in ((led or {}).get("by") or {}).items() if v > 0), key=lambda kv: (-kv[1], kv[0]))
    if reading and led and fresh[1] != led.get("window"):
        rows = []   # the window reset since these records
    if not reading:
        return rows, None
    limit = (led or {}).get("limit") or 5000
    return rows, max(0, (limit - fresh[0]) - sum(v for _, v in rows))


def _outside_line(outside):
    return "  outside pl: no GitHub reading" if outside is None else f"  outside pl {outside}"


def report_json(rows, fresh, current):
    """`pl usage --github --json`: {"accounts": [...]} with the same callers and outside-pl points as the text."""
    accts = list(rows) + ([] if any(a == current for a, _ in rows) else [(current, None)])
    out = []
    for acct, led in accts:
        mine = acct == current
        by, outside = callers(led, fresh, mine)
        reading = fresh if mine and isinstance(fresh, tuple) else None
        out.append({"account": _label(acct), "limit": (led or {}).get("limit") or 5000,
                    "remaining": reading[0] if reading else (led or {}).get("remaining"),
                    "reset": reading[1] if reading else (led or {}).get("reset"),
                    "callers": [{"caller": k, "points": v} for k, v in by], "outside_pl": outside,
                    "github": {"remaining": reading[0], "reset": reading[1]} if reading else None,
                    "why": None if reading else _github_side(acct, led, fresh, current).removeprefix("GitHub: ")})
    return {"accounts": out}


def report(rows=None, fresh=None, current=None):
    """`pl usage --github`: every GitHub user's budget this hour, per profile and per caller. With `fresh` (GitHub's own
    reading of `current`'s token, or why there is none), each line shows pl's reading beside GitHub's."""
    rows = accounts() if rows is None else rows
    if not rows and fresh is None:
        return "GitHub: no GraphQL reading this hour (pl records one with each board read)"
    out = []
    if fresh is not None and all(acct != current for acct, _ in rows):
        out.append(f"{_label(current)}: pl: no reading yet | {_github_side(current, None, fresh, current)}")
        out.append(_outside_line(callers(None, fresh, True)[1]))
    for acct, led in rows:
        reset = time.strftime("%H:%M", time.localtime(led["reset"]))
        age = int(_clock() - (led.get("at") or 0))
        ours = "pl " if fresh is not None else ""
        head = (f"{_label(acct)}: {ours}{led['remaining']} of {led.get('limit') or 5000} GraphQL points left, "
                f"resets {reset} (read {age} s ago)")
        out.append(head if fresh is None else f"{head} | {_github_side(acct, led, fresh, current)}")
        for p, (v, share) in profile_rows(led).items():
            out.append(f"  {p:<20} spent {v:>5}   fair share {share}")
        by, outside = callers(led, fresh, acct == current)
        out.extend(f"  {k} {v}" for k, v in by)
        if fresh is not None and acct == current:
            out.append(_outside_line(outside))
    return "\n".join(out)
