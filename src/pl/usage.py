"""Token counts from Claude Code transcripts, bucketed by hour, account, model and session.

Only message.usage numbers and message.model are taken from a transcript line; no text is kept. Files are read
from where the last read stopped (offsets in <profile>/state/usage.json), with the Background tab's checks:
no hard-linked credential file, no file outside projects, and a cap per read. A projects folder that is a link is read
only when it points at another configured account's own folder; each folder is read once, and its sessions are split
by account from each account's sessions/*.json (a session no account names is counted under "<a>+<b>")."""
import contextlib
import fcntl
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import events, harnesses
from pl.util import _nofollow, load_state, mask, parse_iso

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
HEAD = ("input", "output", "cache read", "cache write", "total")
CHUNK = 4 * 1024 * 1024       # at most this much of one file per scan
BUDGET = 64 * 1024 * 1024     # at most this much in all per scan; the rest waits for the next one
KEEP = 14 * 86400             # buckets older than this are dropped; files older than this are not read
RECENT_IDS = 200              # per session: one API reply is written as several lines with the same message id
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,79}")
MID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SID_RE = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOUR_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d")
CONTEXT_WINDOWS = {"claude-": 200_000}   # model id prefix -> context window in tokens; [usage] windows adds to it
DEFAULT_WINDOW = 200_000
BIG_WINDOW = 1_000_000        # a session that went past DEFAULT_WINDOW runs a 1M-context model (the id does not say so)
LOCK_WAIT = 5                 # seconds to wait for the other scanner, then skip this scan
META_BYTES = 64 * 1024        # sessions/*.json larger than this are not read
UNIT = {"m": 60, "h": 3600, "d": 86400}


def _path():
    return C.STATE_DIR / "usage.json"


def _load():
    """The saved state with every entry of the wrong shape dropped."""
    try:
        st = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    st = st if isinstance(st, dict) else {}
    ok = {"files": lambda k, v: isinstance(v, dict),
          "buckets": lambda k, v: k.count("|") >= 3 and isinstance(v, list) and len(v) == 4 and all(type(x) is int for x in v),
          "last": lambda k, v: isinstance(v, dict) and isinstance(v.get("model"), str) and type(v.get("context")) is int
          and isinstance(v.get("hour"), str),
          "ids": lambda k, v: isinstance(v, dict) and isinstance(v.get("ids"), list) and isinstance(v.get("hour"), str)}
    return {name: {k: v for k, v in st[name].items() if f(k, v)} for name, f in ok.items() if isinstance(st.get(name), dict)}


def _save(st):
    tmp = _path().with_name(f".usage.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, _path())


@contextlib.contextmanager
def _locked():
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(C.STATE_DIR / "usage.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    deadline, got = time.monotonic() + LOCK_WAIT, False
    try:
        while True:   # the console and the dispatcher both scan
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        os.close(fd)


def claude_accounts():
    return [n for n in C.ACCOUNTS if ((C.ACCOUNTS.get(n) or {}).get("harness") or "claude") == "claude"]


def _groups():
    """[(resolved projects folder, [(account, config dir)])], each folder once. A linked projects folder counts only
    when it points at another account's own (unlinked) one: any other link would widen what may be read."""
    own, linked = {}, {}
    for acct in claude_accounts():
        if not C.PROFILES.get(acct):
            continue
        root = Path(C.PROFILES[acct]).expanduser()
        try:
            if (root / "projects").is_dir():
                (linked if (root / "projects").is_symlink() else own).setdefault((root / "projects").resolve(), []).append((acct, root))
        except (OSError, RuntimeError):
            continue
    for real, xs in linked.items():
        if real in own:
            own[real] += xs
    return list(own.items())


def _owners(members):
    """session id -> account, from each account's own session registry (sessions/*.json) and pl's loop registry."""
    out, names = {}, {a for a, _ in members}
    for acct, root in members:
        if not harnesses.account_harness(acct).session_registry:
            continue
        for f in (root / "sessions").glob("*.json"):
            try:
                if f.is_symlink() or f.stat().st_size > META_BYTES:
                    continue
                sid = json.loads(f.read_text()).get("sessionId")
            except (OSError, ValueError, AttributeError, RecursionError):
                continue
            if isinstance(sid, str) and SID_RE.fullmatch(sid):
                out.setdefault(sid, acct)
    for name, s in (load_state().get("services") or {}).items():
        prof = (C.SERVICES.get(name) or {}).get("profile") or (s or {}).get("profile")
        if prof in names:
            for sid in (s or {}).get("sessions") or []:
                out.setdefault(sid, prof)
    return out


def _files(projects, roots, now):
    """(path, stat, is_subagent) for every transcript under the resolved projects folder written in the last KEEP seconds."""
    for pattern, sub in (("*/*.jsonl", False), ("*/*/subagents/agent-*.jsonl", True)):
        for p in projects.glob(pattern):
            try:
                st = p.stat()
                if (now - st.st_mtime > KEEP or not p.resolve().is_relative_to(projects)
                        or any(harnesses.is_secret_file(r, st) for r in roots)):
                    continue
            except (OSError, RuntimeError):
                continue
            yield p, st, sub


def _read_new(p, st, rec, cap):
    """Whole new lines since rec's offset, at most cap bytes; rec is updated in place. A replaced or shorter file starts over."""
    ident = [st.st_dev, st.st_ino]
    if rec.get("ident") != ident or st.st_size < rec.get("offset", 0):
        rec.clear()
        rec.update(ident=ident, offset=0)
    try:
        with open(p, "rb", opener=_nofollow) as f:
            fst = os.fstat(f.fileno())
            if [fst.st_dev, fst.st_ino] != ident:
                return []
            f.seek(rec["offset"])
            data = f.read(cap)
    except OSError:
        return []
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) == CHUNK:   # one line longer than a whole read: skip it
            rec["offset"] += len(data)
            rec["skip"] = True
        return []
    rec["offset"] += end + 1
    lines = data[:end].split(b"\n")
    if rec.pop("skip", False):
        lines = lines[1:]
    return lines


def _hour(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H")


def _turn(line):
    """(message id, model, hour, session id, [in, out, cache read, cache write]) of an assistant line, else None."""
    if b'"usage"' not in line:
        return None
    try:
        e = json.loads(line)
    except (ValueError, RecursionError):
        return None
    m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
    u = m.get("usage") if isinstance(m, dict) else None
    if not isinstance(u, dict):
        return None
    n = [v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0 for v in (u.get(k) for k in FIELDS)]
    if not any(n):
        return None
    model = m.get("model")
    model = model if isinstance(model, str) and MODEL_RE.fullmatch(model) and mask(model) == model else "unknown"
    ts, sid = e.get("timestamp"), e.get("sessionId")
    mid = next((x for x in (m.get("id"), e.get("requestId")) if isinstance(x, str) and MID_RE.fullmatch(x)), None)
    hour = ts[:13] if isinstance(ts, str) and HOUR_RE.fullmatch(ts[:13]) else _hour(time.time())
    sid = sid if isinstance(sid, str) and SID_RE.fullmatch(sid) else "unknown"
    return mid, model, hour, sid, n


def scan(save=True):
    """Read what was added to every Claude account's transcripts since the last scan; return the state (saved
    unless save is False). When the other scanner holds the lock for LOCK_WAIT seconds, return the saved state."""
    now = time.time()
    with _locked() as got:
        st = _load()
        if not got:
            return st
        files, buckets, last, ids = (st.get(k) or {} for k in ("files", "buckets", "last", "ids"))
        left, seen = BUDGET, set()
        for projects, members in _groups():
            names = [a for a in claude_accounts() if a in {x for x, _ in members}]
            owners = _owners(members) if len(names) > 1 else {}
            for p, fst, sub in _files(projects, [r for _, r in members], now):
                key = str(p)
                seen.add(key)
                rec = files.setdefault(key, {})
                if left <= 0 or (rec.get("ident") == [fst.st_dev, fst.st_ino] and rec.get("offset") == fst.st_size):
                    continue
                lines = _read_new(p, fst, rec, min(CHUNK, left))
                left -= sum(len(x) + 1 for x in lines)
                for line in lines:
                    t = _turn(line)
                    if t is None:
                        continue
                    mid, model, hour, sid, n = t
                    recent = ids.setdefault(sid, {"hour": hour, "ids": []})   # per session: a copied reply counts once
                    if mid and mid in recent["ids"]:
                        continue
                    if mid:
                        recent["ids"] = (recent["ids"] + [mid])[-RECENT_IDS:]
                    recent["hour"] = max(recent["hour"], hour)
                    acct = names[0] if len(names) == 1 else owners.get(sid, "+".join(names))
                    b = buckets.setdefault(f"{hour}|{acct}|{model}|{sid}", [0, 0, 0, 0])
                    for i in range(4):
                        b[i] += n[i]
                    if not sub:   # a sub-agent's turn is not the parent session's context
                        prev, ctx = last.get(sid) or {}, n[0] + n[2] + n[3]
                        last[sid] = {"model": model, "context": ctx, "hour": hour, "first": prev.get("first", ctx),
                                     "big": bool(prev.get("big")) or ctx > DEFAULT_WINDOW}
        cut = _hour(now - KEEP)
        st = {"files": {k: v for k, v in files.items() if k in seen},
              "buckets": {k: v for k, v in buckets.items() if k[:13] >= cut},
              "last": {k: v for k, v in last.items() if v.get("hour", "") >= cut},
              "ids": {k: v for k, v in ids.items() if v.get("hour", "") >= cut}}
        if save:
            _save(st)
    return st


def _lookup(table, model):
    """table[model], else the value of the longest key that model starts with, else None."""
    if model in table:
        return table[model]
    keys = [k for k in table if model.startswith(k)]
    return table[max(keys, key=len)] if keys else None


def window(model):
    return _lookup({**CONTEXT_WINDOWS, **((C.USAGE or {}).get("windows") or {})}, model) or DEFAULT_WINDOW


def context_pct(st, sid, which="context"):
    """The session's last turn (input + cache read + cache write) as a whole percent of its model's window, or None.
    which="first": its first turn. A session that ever went past 200k is measured against 1M from then on."""
    x = (st.get("last") or {}).get(sid)
    if not x or type(x.get(which)) is not int:
        return None
    w = max(window(x["model"]), BIG_WINDOW) if x.get("big") else window(x["model"])
    return round(100 * x[which] / w)


def loop_every(prompt):
    """Seconds between a /loop prompt's fires, or None."""
    m = re.match(r"/loop (\d+)([mhd]) ", (prompt or "") + " ")
    return int(m[1]) * UNIT[m[2]] if m else None


def cost(model, tokens):
    """Dollars at the user's [usage] prices (per million tokens, every kind alike), or None when no price is set."""
    price = _lookup((C.USAGE or {}).get("prices") or {}, model)
    return tokens / 1_000_000 * price if isinstance(price, (int, float)) and not isinstance(price, bool) else None


def session_map(cards=None):
    """session id -> ("card", "<id8> <stage>") or ("loop", name): from 'started' events, cards' metadata.worker
    and the loop registry the dispatcher keeps (state services.<name>.sessions)."""
    out = {}
    for e in events._read():
        if e.get("kind") == "started" and e.get("session") and e.get("card"):
            out[e["session"]] = ("card", f"{str(e['card'])[:8]} {e.get('stage') or ''}".strip())
    for c in cards or []:
        w = (c.get("metadata") or {}).get("worker") or {}
        if w.get("session_id"):
            out[w["session_id"]] = ("card", f"{str(c.get('id'))[:8]} {w.get('stage') or ''}".strip())
    for name, s in (load_state().get("services") or {}).items():
        for sid in (s or {}).get("sessions") or []:
            out[sid] = ("loop", name)
    return out


def totals(st, since, by="account", cards=None):
    """{group: [in, out, cache read, cache write, cost, some model unpriced]} for buckets from the hour holding
    `since` on: buckets are whole hours, so a window can include up to an hour more than asked."""
    cut, smap = _hour(since), session_map(cards) if by in ("card", "loop") else {}
    out = {}
    for key, n in (st.get("buckets") or {}).items():
        hour, acct, model, sid = key.split("|", 3)
        if hour < cut:
            continue
        kind, name = smap.get(sid, (None, None))
        g = {"account": acct, "model": model, "session": sid,
             "card": name if kind == "card" else f"loop {name}" if kind == "loop" else "other",
             "loop": name if kind == "loop" else "not a loop"}[by]
        row = out.setdefault(g, [0, 0, 0, 0, 0.0, False])
        for i in range(4):
            row[i] += n[i]
        c = cost(model, sum(n))
        row[4] += c or 0
        row[5] = row[5] or c is None
    return out


def human(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else str(n)


def table(st, since, by="account", cards=None):
    """The `pl usage` table: one row per group, biggest first; other harnesses' accounts show n/a."""
    priced = bool((C.USAGE or {}).get("prices"))
    out = [[by, *HEAD, *(["cost"] if priced else [])]]
    for g, r in sorted(totals(st, since, by, cards).items(), key=lambda kv: -sum(kv[1][:4])):
        money = [("-" if r[5] and not r[4] else f"${r[4]:.2f}")] if priced else []
        out.append([g, *(human(x) for x in r[:4]), human(sum(r[:4])), *money])
    if by == "account":
        out += [[n, *(["n/a"] * (len(out[0]) - 1))] for n in C.ACCOUNTS if n not in claude_accounts()]
    if len(out) == 1:
        return "no tokens in this window"
    w = [max(len(r[i]) for r in out) for i in range(len(out[0]))]
    return "\n".join("  ".join(v.ljust(w[i]) if i == 0 else v.rjust(w[i]) for i, v in enumerate(r)).rstrip() for r in out)


def _spent(st, sids, since):
    cut = _hour(since)
    return sum(sum(n[:4]) for k, n in (st.get("buckets") or {}).items() if k.rsplit("|", 1)[-1] in sids and k[:13] >= cut)


def summary(st, now=None):
    """What the console shows: tokens per account in each Dashboard window, and per loop its context, tokens in
    the last hour and whether it looks dead (a Claude loop with recorded sessions, on for 3 fires of its /loop
    interval with no tokens spent)."""
    now = now or time.time()
    by_window = {k: {a: sum(r[:4]) for a, r in totals(st, now - s).items()} for k, s in (("1h", 3600), ("24h", 86400), ("7d", 604800))}
    loops = {}
    for name, s in (load_state().get("services") or {}).items():
        sids = (s or {}).get("sessions") or []
        every = loop_every((C.SERVICES.get(name) or {}).get("prompt"))
        three = 3 * every if every else None
        started = parse_iso((s or {}).get("started_at") or "")
        acct = (C.SERVICES.get(name) or {}).get("profile") or (s or {}).get("profile")
        claude = ((C.ACCOUNTS.get(acct) or {}).get("harness") or "claude") == "claude"
        dead = bool(claude and sids and three and started and now - started >= three and _spent(st, sids, now - three) == 0)
        loops[name] = {"context": context_pct(st, sids[-1]) if sids else None, "tokens_1h": _spent(st, sids, now - 3600), "dead": dead}
    return {"by_window": by_window, "loops": loops, "accounts": list(C.ACCOUNTS)}


def cmd_usage(a):
    from pl.standup import parse_since
    since = parse_since(a.since or "24h").timestamp()
    cards = None
    if a.by == "card":   # titles and current workers; offline, card ids come from the event log only
        try:
            from pl.board import cards as board_cards
            cards = board_cards()
        except (Exception, SystemExit):  # noqa: BLE001
            cards = None
    print(table(scan(), since, a.by or "account", cards))
