"""Metadata stored in a marked fenced JSON block at the end of a card's description."""
import json
import re

MARK = "<!-- pl:metadata -->"
MARK_RE = re.compile(rf"^{re.escape(MARK)}(?=\n|\Z)", re.M)
BLOCK_RE = re.compile(r"\A\n```json\n(.*?)\n```\n?\Z", re.S)


def join(text, meta):
    return (text or "").rstrip("\n") + "\n\n" + MARK + "\n```json\n" + json.dumps(meta or {}, sort_keys=True, ensure_ascii=False) + "\n```\n"


def split(desc):
    desc = desc or ""
    ms = list(MARK_RE.finditer(desc))
    if not ms:
        return desc, {}, None
    m = ms[-1]
    head = desc[:m.start()]
    text_above = head[:-2] if head.endswith("\n\n") else head[:-1] if head.endswith("\n") else head
    if bm := BLOCK_RE.match(desc[m.end():]):
        try:
            meta = json.loads(bm.group(1))
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            return text_above, meta, None
    return text_above, {}, desc[m.start():]
