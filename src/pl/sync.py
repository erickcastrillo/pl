"""Replay the outbox of a local board to its GitHub Project, in order, within the shared GraphQL budget."""
import json
import os
from datetime import datetime, timezone

from pl import board, ghquota, outbox
from pl import config as C
from pl.trackers import github

ENTRY_POINTS = 8   # estimate of one entry's cost: a create is the dearest, an update with its re-read the next


class _NoItem(Exception):
    pass


def _ids_path():
    return C.CONFIG_DIR / "sync-ids.json"


def _state():
    try:
        got = json.loads(_ids_path().read_text())
        return {"ids": dict(got["ids"]), "last_sent": int(got["last_sent"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ids": {}, "last_sent": 0}


def _save(state):
    p = _ids_path()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(state))
    os.replace(tmp, p)


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


def replay(limit=None):
    """Send queued entries until done, an error, the reserve, or half of the points left at the start."""
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
