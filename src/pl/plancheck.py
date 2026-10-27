"""pl plan check: slices of one plan that change the same file with no depends_on between them."""
import json
import re
from itertools import combinations

from pl import board
from pl.util import short_id

APPEND_ONLY = ("src/pl/whatsnew.py",)
PATH_RE = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.\w+")
LATER_RE = re.compile(r"\(Plan\s+\d+,\s*([^)\s]+)\s*\)")


def counts(path):
    return path not in APPEND_ONLY and not path.rsplit("/", 1)[-1].startswith("CHANGELOG")


def _paths(text):
    return set(PATH_RE.findall(text))


def plan_files(text):
    wheres = re.findall(r"^\s*-\s*Where:(.*)$", text or "", re.M)
    return set().union(*map(_paths, wheres)) if wheres else set()


def later_files(text):
    block = re.search(r"^## Later slices\s*$(.*?)(?=^## |\Z)", text or "", re.M | re.S)
    out = {}
    for line in block.group(1).splitlines() if block else ():
        m, f = LATER_RE.search(line), re.search(r"\bfiles:(.*)$", line)
        if m and f:
            out.setdefault(m.group(1), set()).update(_paths(f.group(1)))
    return out


def _plan(c):
    desc = c.get("description")
    if desc is None:
        desc = board.card(c["id"]).get("description") or ""
    return board.sections(desc).get("PLAN") or ""


def files_by_card(cs):
    plans = {c["id"]: _plan(c) for c in cs}
    files = {c["id"]: plan_files(plans[c["id"]]) for c in cs}
    for text in plans.values():
        for token, paths in later_files(text).items():
            hits = board.matches(token, cs)
            if len(hits) == 1:
                files[hits[0]["id"]] |= paths
    return files


def _linked(a, b):
    return b["id"] in ((a.get("metadata") or {}).get("depends_on") or [])


def overlaps(cs):
    cs = sorted(cs, key=board.plan_order)
    files = files_by_card(cs)
    rows = []
    for a, b in combinations(cs, 2):
        shared = sorted(f for f in files[a["id"]] & files[b["id"]] if counts(f))
        if shared and not _linked(a, b) and not _linked(b, a):
            rows.append({"a": a["id"], "b": b["id"], "files": shared})
    return rows


def cmd_plan(a):
    cs = [c for c in board.cards() if board.plan_slice(c)[0] == a.n and board.col_name(c["list_id"]) != "Done"]
    if not cs:
        raise SystemExit(f"pl plan check: no open cards of plan {a.n} on the board")
    rows = overlaps(cs)
    if a.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"plan {a.n}: no unordered overlaps among {len(cs)} cards checked")
    for r in rows if not a.json else ():
        print(f"  {short_id(r['a'])}  {short_id(r['b'])}  shared: {', '.join(r['files'])}")
    if rows:
        raise SystemExit(1)
