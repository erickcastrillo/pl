"""Writes pl's own GitHub budget hold refused, saved until the reset. This is pl's budget hold only, not GitHub's
rate limit: a RateLimited that is not an OverBudget is never queued here. One JSON line per write in
<profile>/state/held-writes.jsonl; the dispatcher replays them in order after the reset."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from pl import config as C
from pl import alerts, events
from pl.trackers.github import RateLimited
from pl.util import now_iso, short_id


def path() -> Path:
    return C.STATE_DIR / "held-writes.jsonl"


def _now():
    return time.time()


def _hhmm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


@contextmanager
def _lock():
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(C.STATE_DIR / "held-writes.jsonl.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def entries() -> list[dict]:
    p = path()
    if not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            if i == len(lines) - 1:
                break
    return out


def _write(keep):
    p = path()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, "".join(json.dumps(e, default=str) + "\n" for e in keep).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def cached_list(card):
    """The column id the shared board save has for card, or None: nothing is read from GitHub while the budget is held."""
    from pl import board
    got = board._load(C.STATE_DIR / "board-cache.json")
    if got.get("key") != board._key():
        return None
    return next((c.get("list_id") for c in got.get("cards") or [] if c.get("id") == card), None)


def add(card, op, args, until, was=None) -> dict:
    """was: the card's column id when it was queued, if known; a replay leaves the card alone when it has moved since."""
    with _lock():
        cur = entries()
        entry = {"seq": (cur[-1]["seq"] + 1) if cur else 1, "card": str(card), "op": op, "args": args,
                 "at": now_iso(), "until": until}
        if was is not None:
            entry["was"] = was
        _write(cur + [entry])
    events.emit("write_queued", card, op=op, args=args, until=until)
    return entry


def _drop(seq):
    with _lock():   # re-read under the lock: a command may have queued another write since the replay began
        _write([e for e in entries() if e["seq"] != seq])


def target(entry) -> str:
    """What a queued write does to its card, in words: the doctor line and the line the dispatcher prints."""
    a = entry["args"]
    if entry["op"] == "rework":
        return f"In progress (round {a['round']})"
    if entry["op"] == "give_up":
        return "failed label on " + ", ".join(f"#{u.rsplit('/', 1)[-1]}" for u in a["prs"])
    if a.get("column"):
        return a["column"]
    if a.get("tags"):
        return "labels " + ", ".join(a["tags"])
    if urls := (a.get("metadata") or {}).get("pr_urls"):
        return f"link {urls[-1]}"
    return "update"


def _apply(entry):
    """True when the write landed; False, or a reason, when it was skipped: the card moved, or the rework no longer applies."""
    from pl import board, commands, rework   # commands imports the dispatcher, which imports this module
    op = entry["op"]
    if op == "update":
        board.update(entry["card"], **entry["args"])
        return True
    if op == "rework":
        return rework.replay_send_back(entry) or True
    if op == "give_up":
        rework.replay_give_up(entry)
        return True
    if op != "move":
        raise ValueError(f"unknown queued write {op!r}")
    if entry.get("was") is not None and board.card(entry["card"])["list_id"] != entry["was"]:
        return False
    commands.move_card(entry["card"], entry["args"]["column"], entry["args"].get("pr"))
    return True


def replay(notify):
    """Apply the queued writes in order once the reset has passed. A write held again stays queued, with every later
    one, and opens nothing; a write that fails is dropped and alerted. notify(title, body) is the dispatcher's."""
    for entry in entries():
        if _now() < entry["until"]:
            return
        card, op, args = entry["card"], entry["op"], entry["args"]
        try:
            landed = _apply(entry)
        except RateLimited:   # OverBudget too: nothing failed, the write waits
            return
        except (Exception, SystemExit) as e:
            err = str(e)[:300]
            _drop(entry["seq"])
            events.emit("queued_write_failed", card, op=op, args=args, error=err)
            title = f"Card {short_id(card)}: queued {op} to {target(entry)} failed on replay"
            if why := alerts.open(f"queued_write_failed:{card}:{entry['seq']}", "warn", title,
                                  f"error: {err}; redo it by hand with pl move", [card]):
                notify(alerts.headline(why, title), err)
            continue
        _drop(entry["seq"])
        if landed is True:
            events.emit("write_replayed", card, op=op, args=args)
        else:
            events.emit("write_replay_skipped", card, op=op, args=args,
                        why=landed or "the card was moved after the write was queued")


def doctor_lines() -> list[str]:
    rows = entries()
    return [f"{short_id(e['card'])}  {e['op']} -> {target(e)}  queued {e['at']}  "
            f"waits for reset {_hhmm(e['until'])}" for e in rows] or ["none"]
