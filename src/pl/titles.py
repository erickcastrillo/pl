"""pl titles check: plan cards whose 'Plan N slice M:' title and plan/slice metadata disagree. Reads only."""
import json
import re
import sys

from pl import board
from pl.util import short_id

TITLE_RE = re.compile(r"^\s*plan\s+(\d+)\s+slice\s+(\d+)\s*:", re.I)


def title_pair(title):
    m = TITLE_RE.match(title or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _num(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def meta_pair(meta):
    return _num(meta.get("plan")), _num(meta.get("slice"))


def _pair(p, s):
    return f"Plan {'?' if p is None else p} slice {'?' if s is None else s}"


def _has(pair):
    return pair != (None, None)


def drift(cs):
    rows, groups = [], {}
    for c in cs:
        tp, mp = title_pair(c["title"]), meta_pair(c.get("metadata") or {})
        row = {"id": c["id"], "title": c["title"], "plan": mp[0], "slice": mp[1], "title_plan": tp[0],
               "title_slice": tp[1], "reason": ""}
        if not _has(tp) and not _has(mp):
            continue
        if not _has(tp):
            row["reason"] = f"metadata says {_pair(*mp)}, title has no prefix"
        elif not _has(mp):
            row["reason"] = f"title says {_pair(*tp)}, metadata has none"
        elif tp != mp:
            row["reason"] = f"title says {_pair(*tp)}, metadata says {_pair(*mp)}"
        else:
            groups.setdefault(tp, []).append(row)
            continue
        rows.append(row)
    for pair, group in groups.items():
        if len(group) > 1:
            rows += [{**r, "reason": f"{_pair(*pair)} is used by more than one card"} for r in group]
    return sorted(rows, key=lambda r: (str(r["plan"] or r["title_plan"]), str(r["slice"] or r["title_slice"]), r["id"]))


def cmd_titles(a):
    try:
        cs = [c for c in board.cards() if board.col_name(c["list_id"]) != "Done"]
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
        raise SystemExit(2 if e.code else 0)
    rows = drift(cs)
    if a.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"titles: no card needs fixing ({len(cs)} plan cards checked)")
    else:
        print(f"titles: {len(rows)} card(s) need fixing")
        for r in rows:
            print(f"  {short_id(r['id'])}  {r['title'][:56]:<56}  {r['reason']}")
    if rows:
        raise SystemExit(1)
