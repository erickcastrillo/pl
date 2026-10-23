"""pl inbox: list waiting gates across profiles, and persist this profile's gates."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import board, events, manager, profiles
from pl.board import cards, col_name
from pl.util import card_url, cmd_id, mask, parse_iso, short_id


def gate_of(c, col=None):
    """spec or plan when the card waits for a review, else None."""
    if col is None:
        lid = c.get("list_id")
        if lid:
            try:
                col = col_name(lid)
            except (Exception, SystemExit):
                col = lid
    if col and col.startswith("col-"):
        col = col.removeprefix("col-")
    m = c.get("metadata") or {}
    if m.get("pipeline_mode") != "auto":
        return None
    if col == "Spec ready":
        gates = getattr(C, "GATES", {}) or {}
        if gates.get("spec") and not m.get("spec_approved_at"):
            return "spec"
    elif col == "Plan for review":
        return "plan"
    return None


def rows_here(cs):
    """Rows for waiting gates in this profile, computed from cs."""
    evs = events._read()
    now = time.time()
    prof = C.PROFILE_NAME or ""
    rows = []
    for c in cs:
        col = c.get("col")
        if not col and c.get("list_id"):
            try:
                col = col_name(c["list_id"])
            except (Exception, SystemExit):
                col = c["list_id"]
        gate = gate_of(c, col)
        if not gate:
            continue
        cid = c.get("id", "")
        card_events = [e for e in evs if e.get("kind") == "moved" and e.get("card") == cid and e.get("to") == col]
        if card_events:
            newest = max(card_events, key=lambda e: e.get("_t", datetime.min.replace(tzinfo=timezone.utc)))
            since = newest.get("ts") or ""
        else:
            since = c.get("updated_at") or ""
        since_ts = parse_iso(since) if since else 0
        age_seconds = max(0, int(now - since_ts)) if since_ts else 0
        summary = (c.get("title") or "")[:80]
        try:
            url = card_url(cid)
        except (Exception, SystemExit):
            url = ""
        c_hint = cmd_id(cid)
        cmd = f"pl --profile {prof} approve {c_hint}" if prof else f"pl approve {c_hint}"
        rows.append({
            "pipeline": prof,
            "card": cid,
            "gate": gate,
            "summary": summary,
            "since": since,
            "age_seconds": age_seconds,
            "url": url,
            "command": cmd,
        })
    return rows


def write(rows):
    """Save state/inbox.json atomically with mode 0600."""
    board._save(C.STATE_DIR / "inbox.json", {"at": time.time(), "items": rows})


def listing():
    """Merge this profile's live gates with other profiles' saved inbox.json, oldest first."""
    all_rows = list(rows_here(cards()))
    now = time.time()
    cur_prof = C.PROFILE_NAME or ""
    try:
        profs = profiles.list_profiles(check_lock=False)
    except Exception:
        profs = []
    for p in profs:
        p_name = p.get("name")
        if p_name == cur_prof:
            continue
        p_dir = Path(p.get("dir", ""))
        inbox_path = p_dir / "state" / "inbox.json"
        try:
            data = json.loads(inbox_path.read_text())
            if not isinstance(data, dict):
                continue
            at = data.get("at")
            stale = not isinstance(at, (int, float)) or not (0 <= now - at <= manager.WAITS_FRESH)
            items = data.get("items")
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                row = dict(it)
                if stale:
                    row["stale"] = True
                if row.get("since"):
                    ts = parse_iso(row["since"])
                    if ts:
                        row["age_seconds"] = max(0, int(now - ts))
                all_rows.append(row)
        except (OSError, ValueError, TypeError, KeyError):
            continue

    all_rows.sort(key=lambda r: (-(r.get("age_seconds") or 0), r.get("since") or ""))
    return all_rows


def cmd_inbox(a):
    """pl inbox [--json]: list waiting gates across profiles."""
    items = listing()
    if getattr(a, "json", False):
        print(mask(json.dumps({"items": items})))
        return
    for r in items:
        stale = " (stale)" if r.get("stale") else ""
        print(f"{r['pipeline']:<12} {short_id(r['card']):<16} {r['gate']:<6} {r['summary']:<40} {r['command']}{stale}")
