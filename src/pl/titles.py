"""pl titles: check plan cards whose 'Plan N slice M:' title and plan/slice metadata disagree (--fix repairs the
one-sided ones), and number plan cards (next, set)."""
import json
import re
import sys

from pl import board, ideas
from pl.util import short_id

TITLE_RE = re.compile(r"^\s*plan\s+(\d+)\s+slice\s+(\d+)\s*:", re.I)


def title_pair(title):
    m = TITLE_RE.match(title or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


PREFIX_RE = re.compile(r"^\s*plan\s+\d+\s+slice\s+\d+\s*:\s*", re.I)


def strip_prefix(title):
    return PREFIX_RE.sub("", title or "", count=1).strip()


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


def _holders(cs):
    held = {}
    for c in cs:
        for pair in {title_pair(c["title"]), meta_pair(c.get("metadata") or {})}:
            if _has(pair) and None not in pair:
                held.setdefault(pair, set()).add(c["id"])
    return held


def fix_rows(rows, cs, dry_run):
    held, fixed = _holders(cs), []
    for r in rows:
        meta, title = (r["plan"], r["slice"]), (r["title_plan"], r["title_slice"])
        pair = title if _has(title) else meta
        one_sided = _has(title) != _has(meta) and None not in pair
        row = {**r, "action": "skipped", "new_title": None}
        if one_sided and held.get(pair, set()) <= {r["id"]}:
            if _has(meta):
                row.update(action="rename", new_title=ideas.plan_title(*meta, strip_prefix(r["title"])))
                if not dry_run:
                    board.update(r["id"], title=row["new_title"])
            else:
                row["action"] = "write metadata"
                if not dry_run:
                    board.update(r["id"], metadata={"plan": title[0], "slice": title[1]})
        elif one_sided:
            row["reason"] = f"{_pair(*pair)} is used by more than one card"
        fixed.append(row)
    return fixed


def fix_line(r, dry_run):
    who = short_id(r["id"])
    if r["action"] == "rename":
        return f"{'would rename' if dry_run else 'renamed'} {who}: {r['title']!r} -> {r['new_title']!r}"
    if r["action"] == "write metadata":
        return (f"{'would write' if dry_run else 'wrote'} metadata {_pair(r['title_plan'], r['title_slice'])} on {who} "
                "(title unchanged)")
    return f"skipped {who} {r['title']!r}: {r['reason']}; fix by hand with pl titles set"


def _read_cards():
    try:
        return board.cards()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
        raise SystemExit(2 if e.code else 0)


def cmd_titles(a):
    if a.dry_run and not a.fix:
        raise SystemExit("pl titles check: --dry-run goes with --fix")
    cs = [c for c in _read_cards() if board.col_name(c["list_id"]) != "Done"]
    rows = drift(cs)
    if a.fix:
        rows = fix_rows(rows, cs, a.dry_run)
        if a.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print(f"titles: no card needs fixing ({len(cs)} plan cards checked)")
        for r in rows if not a.json else ():
            print(fix_line(r, a.dry_run))
        if any(r["action"] == "skipped" for r in rows):
            raise SystemExit(1)
        return
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
