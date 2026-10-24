"""Retention and data removal policies: pl forget --contact."""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone

from pl import board, config as C, events


def _matches(target, val):
    if not val:
        return False
    t = target.strip().lower()
    if isinstance(val, str):
        v = val.strip().lower()
        return t == v or f"<{t}>" in v or t in [p.strip("<>(),\"'") for p in v.split()]
    if isinstance(val, (list, tuple, set)):
        return any(_matches(target, item) for item in val)
    if isinstance(val, dict):
        return any(_matches(target, k) or _matches(target, v) for k, v in val.items())
    return False


def _card_matches(c, target):
    meta = c.get("metadata") or {}
    refs = meta.get("refs") if isinstance(meta.get("refs"), dict) else {}
    card_refs = c.get("refs") if isinstance(c.get("refs"), dict) else {}
    sources = (meta.get("contact_emails"), c.get("contact_emails"), refs.get("contacts"),
               meta.get("refs.contacts"), card_refs.get("contacts"), meta.get("from"), c.get("from"))
    return any(_matches(target, s) for s in sources)


def _clean_card(c, target):
    meta = dict(c.get("metadata") or {})
    ce = meta.get("contact_emails")
    if isinstance(ce, (list, tuple, set)):
        meta["contact_emails"] = [x for x in ce if not _matches(target, x)]
    if not meta.get("contact_emails"):
        meta.pop("contact_emails", None)

    if "refs" in meta and isinstance(meta["refs"], dict):
        contacts = meta["refs"].get("contacts")
        if isinstance(contacts, (list, tuple, set)):
            meta["refs"]["contacts"] = [x for x in contacts if not _matches(target, x)]
        elif isinstance(contacts, dict):
            meta["refs"]["contacts"] = {k: v for k, v in contacts.items() if not _matches(target, k) and not _matches(target, v)}
        elif contacts and _matches(target, contacts):
            meta["refs"].pop("contacts", None)
        if not meta["refs"].get("contacts"):
            meta["refs"].pop("contacts", None)
        if not meta["refs"]:
            meta.pop("refs", None)
    meta.pop("refs.contacts", None)

    if "from" in meta and _matches(target, meta["from"]):
        meta.pop("from", None)
    meta.pop("FIELDS", None)

    parts = board.sections(c.get("description") or "")
    parts.pop("FIELDS", None); parts.pop("EMAIL", None)
    for k in list(parts):
        s = re.sub(r"```email\b[\s\S]*?```\n?", "", parts[k])
        parts[k] = re.sub(r"(?m)^From:\s+[^\n]+\n(?:(?:To|Subject|Date):\s+[^\n]+\n?)+", "", s)
    return board.render(parts), meta


def forget(contact, *, dry_run=False):
    target, cards_count = contact.strip(), 0
    for c in board.cards():
        if _card_matches(c, target):
            new_desc, new_meta = _clean_card(c, target)
            if not dry_run:
                board.update(c["id"], description=new_desc, metadata=new_meta, verify=False)
            cards_count += 1

    logs_count = events.forget(target, dry_run=dry_run)
    audit_count = 0 if dry_run else 1
    if not dry_run:
        sha = hashlib.sha256(target.encode("utf-8")).hexdigest()
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "forget", "action": "forget",
               "target_sha256": sha, "profile": C.PROFILE_NAME}
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(C.STATE_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return {"cards": cards_count, "logs": logs_count, "memory": 0, "audit": audit_count}


def cmd_forget(a):
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit(2)
        try:
            ok = input(f"Forget contact {a.contact}? [y/N]: ")
        except EOFError:
            ok = ""
        if ok.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 0
    counts = forget(a.contact, dry_run=a.dry_run)
    if a.json:
        return print(json.dumps(counts))
    print(f"cards: {counts['cards']}\nlogs: {counts['logs']}")
    print(f"memory: {counts['memory']} (memory store not present)\naudit: {counts['audit']}")
