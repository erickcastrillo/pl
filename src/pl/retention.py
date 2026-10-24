"""Plan 15 slice 5: Retention of email content.

Once a day, the dispatcher finds cards created by triggers older than [retention] email_days (90),
replaces email content, message snippets and summary with thread: <link>, and sets metadata.retained_at.
"""
import json
import re
import time

from pl import board, events
from pl import config as C
from pl.board import cards
from pl.util import now_iso, parse_iso, short_id

RETENTION_EVERY = 86400   # seconds between daily retention passes
_LAST = {"at": None}

FENCED_RE = re.compile(r"```(?:email[^\n]*\n|[a-z0-9_-]*\n|\n)?.*?```", re.DOTALL | re.IGNORECASE)
MESSAGES_RE = re.compile(r"(?ms)(^## Messages\s*\n)(.*?)(?=(^## |\Z))")


def _card_created_at(c):
    meta = c.get("metadata") or {}
    val = meta.get("created_at") or c.get("created_at") or meta.get("submitted_at")
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        return parse_iso(val)
    return 0


def extract_thread_link(c):
    meta = c.get("metadata") or {}
    if meta.get("thread_link"):
        return str(meta["thread_link"])
    if meta.get("thread"):
        return str(meta["thread"])
    if isinstance(meta.get("refs"), dict) and meta["refs"].get("thread"):
        return str(meta["refs"]["thread"])
    desc = c.get("description") or ""
    m = re.search(r"(?i)\bthread(?:\s+link)?:\s*(\S+)", desc)
    if m:
        return m.group(1).rstrip(")>]")
    m = re.search(r"https?://\S+", desc)
    if m:
        return m.group(0).rstrip(")>]")
    return "<link>"


def transform_card_content(c, link):
    desc = c.get("description") or ""
    meta = dict(c.get("metadata") or {})
    link_line = f"thread: {link}"

    parts = board.sections(desc)
    replaced = False

    if "FIELDS" in parts:
        parts["FIELDS"] = re.sub(r"(?m)^summary:.*$", f"summary: {link_line}", parts["FIELDS"])
        replaced = True
    if isinstance(meta.get("fields"), dict) and "summary" in meta["fields"]:
        meta["fields"] = {**meta["fields"], "summary": link_line}
    if "summary" in meta:
        meta["summary"] = link_line

    if "INPUT" in parts:
        inp = parts["INPUT"]
        if FENCED_RE.search(inp):
            parts["INPUT"] = FENCED_RE.sub(link_line, inp)
            replaced = True
        if MESSAGES_RE.search(inp):
            parts["INPUT"] = MESSAGES_RE.sub(rf"\1{link_line}\n", parts["INPUT"])
            replaced = True

    if parts and replaced:
        desc = board.render(parts)
    else:
        # Fallback if not partitioned cleanly
        if FENCED_RE.search(desc):
            desc = FENCED_RE.sub(link_line, desc)
            replaced = True
        if MESSAGES_RE.search(desc):
            desc = MESSAGES_RE.sub(rf"\1{link_line}\n", desc)
            replaced = True
        if re.search(r"(?m)^summary:.*$", desc):
            desc = re.sub(r"(?m)^summary:.*$", f"summary: {link_line}", desc)
            replaced = True

    if not replaced:
        if "INPUT" in parts:
            parts["INPUT"] = link_line
            desc = board.render(parts)
        else:
            desc = f"{link_line}\n"

    return desc, meta


def sweep(all_cards, dry, force=False):
    """Find cards from triggers older than email_days without retained_at, and retain them."""
    now = time.time()
    if not force and _LAST["at"] is not None and now - _LAST["at"] < RETENTION_EVERY:
        return []

    email_days = 90
    try:
        email_days = int((C.RETENTION or {}).get("email_days", 90))
    except (TypeError, ValueError):
        email_days = 90

    cutoff_seconds = email_days * 86400
    retained = []

    for c in all_cards:
        meta = c.get("metadata") or {}
        source = str(meta.get("source") or "")
        if not source.startswith("trigger:"):
            continue
        if meta.get("retained_at"):
            continue
        created_ts = _card_created_at(c)
        if not created_ts or (now - created_ts) < cutoff_seconds:
            continue

        link = extract_thread_link(c)
        new_desc, new_meta = transform_card_content(c, link)
        new_meta["retained_at"] = now_iso()

        if not dry:
            try:
                board.update(c["id"], description=new_desc, metadata=new_meta)
                fresh = board.card(c["id"])
                if not (fresh.get("metadata") or {}).get("retained_at"):
                    raise ValueError(f"card {c['id']} verification failed: retained_at missing")
                try:
                    from pl import audit
                    if hasattr(audit, "append"):
                        audit.append("card.retain", card=c["id"])
                except Exception:
                    pass
            except Exception as e:
                events.emit("error", card_id=c["id"], message=f"retention: {e}")
                continue

        retained.append(c)

    _LAST["at"] = now
    return retained


def cmd_retention(a):
    """pl retention run [--dry-run] [--json]"""
    dry = getattr(a, "dry_run", False)
    as_json = getattr(a, "json", False)
    results = sweep(cards(), dry=dry, force=True)
    if as_json:
        print(json.dumps([{"id": c["id"], "title": c.get("title", ""), "retained": True} for c in results]))
    else:
        if not results:
            print("no cards due for retention")
        for c in results:
            act = "would retain" if dry else "retained"
            print(f"{short_id(c['id'])}  {act}")
