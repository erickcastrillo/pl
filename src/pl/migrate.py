"""Copy a board's columns and cards to another board. Every copied card carries its source id in metadata.migrated_from,
so a run that stopped halfway picks up where it was, even when the map file is lost."""
from pl import board
from pl.util import parse_iso

MARK = "migrated_from"


def _titles(tracker):
    return {i: n for n, i in tracker.columns().items()}


def _copies(dst):
    return {c["metadata"][MARK]["id"]: c for c in dst.cards() if isinstance(c.get("metadata", {}).get(MARK), dict)}


def plan(src, dst):
    have = dst.columns()
    copies = _copies(dst)
    cards = src.cards()
    return {"columns": [n for n in src.columns() if n not in have],
            "cards": [c for c in cards if c["id"] not in copies],
            "refresh": [c for c in cards if c["id"] in copies
                        and parse_iso(c["updated_at"]) > parse_iso(copies[c["id"]]["metadata"][MARK].get("updated_at", ""))]}


def _fields(card, src_key):
    return {"title": card["title"], "description": card["description"], "tags": list(card["tags"]),
            "assigned_to": card.get("assigned_to"),
            "metadata": {**card["metadata"], MARK: {"board": src_key, "id": card["id"], "updated_at": card["updated_at"]}}}


def copy(src, dst, *, state_path, dry=False, progress=print, src_key=""):
    todo = plan(src, dst)
    if dry:
        for name in todo["columns"]:
            progress(f"would create column {name}")
        for c in todo["cards"]:
            progress(f"would copy card {c['id']} {c['title']}")
        return todo
    for name in todo["columns"]:
        dst.ensure_column(name)
    names = _titles(src)
    copies = _copies(dst)
    mapping = {sid: c["id"] for sid, c in copies.items()}
    board._save(state_path, mapping)
    for c in todo["refresh"]:
        dst.update(copies[c["id"]]["id"], verify=True, **_fields(c, src_key))
    total = len(todo["cards"])
    for n, c in enumerate(todo["cards"], 1):
        made = dst.create(names[c["list_id"]], **_fields(c, src_key))
        mapping[c["id"]] = made["id"]
        board._save(state_path, mapping)
        progress(f"copied {n}/{total} {c['id'][-8:]} {c['title']}")
    todo["mapping"] = mapping
    return todo


def verify(src, dst, mapping):
    src_cols, dst_cols = _titles(src), _titles(dst)
    out = []
    for c in src.cards():
        if c["id"] not in mapping:
            out.append(f"{c['id']}: not copied")
            continue
        got = dst.card(mapping[c["id"]])
        label = f"{c['id']} -> {mapping[c['id']]}"
        want = {**{k: c.get(k) for k in ("title", "description", "assigned_to")}, "tags": sorted(c["tags"]),
                "column": src_cols[c["list_id"]]}
        have = {**{k: got.get(k) for k in ("title", "description", "assigned_to")}, "tags": sorted(got["tags"]),
                "column": dst_cols.get(got["list_id"])}
        out += [f"{label}: {k} differs" for k in want if want[k] != have[k]]
        theirs = {k: v for k, v in got["metadata"].items() if k != MARK}
        out += [f"{label}: metadata.{k} differs" for k in c["metadata"] if theirs.get(k) != c["metadata"][k]]
        out += [f"{label}: metadata.{k} added" for k in theirs if k not in c["metadata"]]
    return out
