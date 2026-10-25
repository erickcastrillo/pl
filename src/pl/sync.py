"""Replay the outbox of a local board to its GitHub Project, in order, within the shared GraphQL budget."""
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from pl import alerts, board, dispatch, ghquota, outbox
from pl import config as C
from pl.trackers.local import Local
from pl.trackers import github
from pl.util import now_iso

ENTRY_POINTS = 8   # estimate of one entry's cost: a create is the dearest, an update with its re-read the next


class _NoItem(Exception):
    pass


class _Busy(Exception):
    pass


def _ids_path():
    return C.CONFIG_DIR / "sync-ids.json"


def _state():
    try:
        got = json.loads(_ids_path().read_text())
        return {**got, "ids": dict(got["ids"]), "last_sent": int(got["last_sent"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ids": {}, "last_sent": 0}


def _write_json(p, data):
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.replace(tmp, p)


def _save(state):
    _write_json(_ids_path(), state)


PULLED = ("column", "title", "description", "tags", "assigned_to")   # pipeline metadata is pl's own: never pulled


def _norm(field, v):
    if field == "tags":
        return sorted(str(t) for t in v or ())
    if field == "assigned_to":
        return v or None
    return str(v or "").strip()


def _values(card, names):
    return {f: _norm(f, names.get(card.get("list_id"), "") if f == "column" else card.get(f)) for f in PULLED}


def _base_path():
    return C.CONFIG_DIR / "sync-base.json"


def _load_base():
    try:
        return dict(json.loads(_base_path().read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def _save_base(base):
    _write_json(_base_path(), base)


def _remember(card, fields):
    """Record what GitHub now holds for these fields, so the next pull does not take it for a person's edit."""
    fields = dict(fields)
    if "list_id" in fields and "column" not in fields:
        fields["column"] = _column(fields.pop("list_id"))
    got = {f: _norm(f, v) for f, v in fields.items() if f in PULLED}
    if got:
        base = _load_base()
        base.setdefault(str(card), {}).update(got)
        _save_base(base)


@contextmanager
def _replay_lock():
    fd = os.open(_ids_path().with_name("sync-ids.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Busy from None
        yield
    finally:
        os.close(fd)


def target():
    owner, _, number = C.SYNC["github"].split("/")
    return github.GitHubProject({"owner": owner, "number": number, "repo": C.SYNC.get("repo"),
                                 "status_field": C.SYNC.get("status_field") or github.STAGE_FIELD})


def _reading():
    led = ghquota.ledger()
    rem = led.get("remaining")
    if isinstance(rem, int) and (led.get("reset") or 0) > ghquota._clock():
        return rem, led.get("limit") or 5000, led["reset"]
    return None, None, None


def _column(name_or_id):
    name = board.col_name(name_or_id)
    return name_or_id if name == "?" else name


def _send(gh, e, state):
    card, op, fields = e["card"], e["op"], dict(e["fields"])
    if op == "create":
        if card in state["ids"]:
            return
        if not C.SYNC.get("repo"):
            raise SystemExit('set [sync] repo = "owner/name": the repo where new cards become issues')
        column = _column(fields.pop("column"))
        state["ids"][card] = gh.create(column, **fields)["id"]
        _remember(card, {**fields, "column": column})
        return
    gid = state["ids"].get(card)
    if gid is None:
        raise _NoItem(card)
    if op == "delete":
        gh.delete(gid)
        state["ids"].pop(card)
        return
    if "list_id" in fields:
        fields["column"] = _column(fields.pop("list_id"))
    gh.update(gid, verify=True, **fields)
    _remember(card, fields)


def replay(limit=None):
    """Send queued entries until done, an error, the reserve, or half of the points left at the start.

    One replay at a time: a second caller sends nothing and reports stopped_because "busy".
    """
    try:
        with _replay_lock():
            return _replay(limit)
    except _Busy:
        return {"sent": 0, "remaining": outbox.count(), "stopped_because": "busy", "next_window": None,
                "error": None}


def _replay(limit):
    state = _state()
    outbox.drop_through(state["last_sent"])
    rem0, lim, reset = _reading()
    stopped, error, until, sent = "done", None, None, 0
    if rem0 is not None and rem0 < ghquota.RESERVE * lim:
        stopped, until = "reserve", reset
    else:
        gh = target()
        for e in outbox.entries():
            if limit is not None and sent >= limit:
                break
            rem, lim_now, reset_now = _reading()
            if rem is not None:
                lim, reset = lim_now, reset_now
                rem0 = rem if rem0 is None else rem0   # no reading at the start: the first call's is the base
                if rem < ghquota.RESERVE * lim:
                    stopped, until = "reserve", reset
                    break
                if (rem0 - rem) + ENTRY_POINTS > rem0 / 2:
                    stopped, until = "half-budget", reset
                    break
            try:
                _send(gh, e, state)
            except _NoItem:
                stopped, error = "error", f"card {e['card']} has no GitHub item yet: run pl sync init --from-github"
                break
            except github.RateLimited as hit:   # OverBudget too: nothing failed, the budget said wait
                stopped, until = "reserve", hit.until
                break
            except SystemExit as ex:
                stopped, error = "error", f"entry {e['seq']} (card {e['card']}): {ex}"
                break
            state["last_sent"] = e["seq"]
            _save(state)
            if outbox.drop_through(e["seq"]) == 0:
                state["last_sent"] = 0   # the queue restarts at seq 1 once empty
                _save(state)
            sent += 1
    _, _, reset_end = _reading()
    until = until or reset_end
    window = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds") if until else None
    return {"sent": sent, "remaining": outbox.count(), "stopped_because": stopped, "next_window": window,
            "error": error}


def pull():
    """Take the GitHub edits no local change competes with; keep the local value, save GitHub's and alert on a clash."""
    out = {"pulled": 0, "conflicts": [], "stopped_because": "done", "error": None}
    rem, lim, _ = _reading()
    if rem is not None and rem < ghquota.RESERVE * lim:
        return {**out, "stopped_because": "reserve"}
    try:
        with _replay_lock():
            return _pull(out)
    except _Busy:
        return {**out, "stopped_because": "busy"}


def _pull(out):
    gh = target()
    try:
        gcards = gh.cards()
        gnames = {v: k for k, v in gh.columns().items()}
    except github.RateLimited:   # OverBudget too
        return {**out, "stopped_because": "reserve"}
    except SystemExit as e:
        return {**out, "stopped_because": "error", "error": f"reading {C.SYNC['github']} failed: {e}"}
    local = Local(C.TRACKER)   # not trackers.get: the outbox wrapper must not see what GitHub said
    lnames = {v: k for k, v in local.columns().items()}
    by_gid = {gid: lid for lid, gid in _state()["ids"].items()}
    base = _load_base()
    pending = {}
    for e in outbox.entries():
        pending.setdefault(e["card"], set()).update("column" if k == "list_id" else k for k in e["fields"])
    try:
        for g in gcards:
            lid = by_gid.get(g["id"])
            if lid is not None:
                _pull_card(local, lid, g, gnames, lnames, base.setdefault(lid, {}), pending.get(lid, ()), out)
    finally:
        _save_base(base)
    return out


def _pull_card(local, lid, g, gnames, lnames, b, pending, out):
    cur = local.card(lid)
    gv, lv = _values(g, gnames), _values(cur, lnames)
    held = (cur.get("metadata") or {}).get("remote_edit") or {}
    take, clash, settled = {}, {}, set()
    for f in PULLED:
        if gv[f] == lv[f]:
            b[f] = gv[f]
            if f in held:
                settled.add(f)
        elif f not in held and f in b and gv[f] == b[f]:
            continue   # only the local side changed: the replay sends it
        elif f in held or f in pending or (f in b and lv[f] != b[f]):
            clash[f] = gv[f]
        else:
            take[f] = b[f] = gv[f]
    if take:
        if "column" in take:
            local.ensure_column(take["column"])
        local.update(lid, verify=True, **take)
        out["pulled"] += 1
    new = {f: {"value": v, "at": now_iso()} for f, v in clash.items() if (held.get(f) or {}).get("value") != v}
    still = {f: v for f, v in {**held, **new}.items() if f not in settled}
    if new or settled:
        local.update(lid, verify=True, metadata={"remote_edit": still or None})
    if new:
        out["conflicts"].append(lid)
        outbox.strip(lid, set(new))
    if not still:
        alerts.resolve(f"sync-conflict:{lid}")
        return
    alerts.open(f"sync-conflict:{lid}", "high",
                f"card {lid} changed on GitHub and here: {', '.join(sorted(still))}",
                "edit the card here to match GitHub, or leave it to keep yours; the alert clears at the next pl sync now")


def local_id(token):
    token = token.strip()
    return next((lid for lid, gid in _state()["ids"].items() if gid == token), None)


def _github():
    owner, _, number = C.SYNC["github"].partition("/projects/")
    return github.GitHubProject({"type": "github-project", "owner": owner, "number": number,
                                 "status_field": C.SYNC.get("status_field") or github.STAGE_FIELD})


def _refuse(why):
    raise SystemExit(f"pl sync init: {why}")


def cmd_init(a):
    """Copy the GitHub board into the empty local board. Every GitHub read comes before the first local write."""
    shown = C.SYNC["github"]
    if C.TRACKER.get("type") != "local":
        _refuse('needs [tracker] type = "local"')
    if not a.from_github:
        _refuse("say where from: --from-github")
    if dispatch.dispatcher_running() and not dispatch.paused():
        _refuse("the dispatcher is running; run pl pause first")
    if _ids_path().exists():
        _refuse("sync-ids.json already exists")
    local = Local(C.TRACKER)   # not trackers.get: the outbox wrapper must not see the import
    if local.cards():
        _refuse("the local board is not empty")
    try:
        gh = _github()
        columns, cards = gh.columns(), gh.cards()
    except SystemExit as e:
        _refuse(f"reading {shown} failed: {e}; nothing was imported")
    if a.dry_run:
        print(f"would import {len(cards)} cards into {len(columns)} columns from {shown}")
        return
    names = {cid: name for name, cid in columns.items()}
    made, state = [], {"ids": {}, "items": {}, "last_sent": 0}
    try:
        for name in columns:
            local.ensure_column(name)
        for c in cards:
            new = local.create(names.get(c.get("list_id"), "Inbox"), title=c["title"], description=c.get("description") or "",
                               tags=c.get("tags") or [], metadata=c.get("metadata") or {}, assigned_to=c.get("assigned_to"))
            made.append(new["id"])
            state["ids"][new["id"]] = c["id"]
            state["items"][new["id"]] = c.get("item_id")
    except BaseException as e:
        for cid in made:
            local.delete(cid)
        raise SystemExit(f"pl sync init: {e}; nothing was imported") from e
    _save(state)
    print(f"imported {len(cards)} cards into {len(columns)} columns from {shown}")
