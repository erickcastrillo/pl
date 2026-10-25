"""Copy a board's columns and cards to another board. Every copied card carries its source id in metadata.migrated_from,
so a run that stopped halfway picks up where it was, even when the map file is lost."""
import importlib
import json
import os
import sys
import time
from urllib.parse import urlparse

import tomlkit

from pl import agents, board, boardcheck, trackers
from pl import config as C
from pl.boardserve import _is_loopback
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


def target_cfg(to, a):
    if to == "local":
        return {"type": "local"}
    if to.startswith(("http://", "https://")):
        server = {"url": to}
        if a.token_env:
            server["headers"] = {"Authorization": f"Bearer ${{{a.token_env}}}"}
        elif not _is_loopback(urlparse(to).hostname or ""):
            raise SystemExit("pl board migrate: a non-loopback URL needs --token-env VAR")
        cfg = {"type": "mcp", "protocol": "pl", "server": server}
    else:
        config = a.mcp_config or C.TRACKER.get("mcp_config")
        if not config:
            raise SystemExit(f"pl board migrate: give --mcp-config PATH to find server {to!r}")
        cfg = {"type": "mcp", "protocol": "pl", "mcp_config": str(config), "server": to}
    if a.board:
        cfg["board_id"] = a.board
    return cfg


def _build(cfg):
    mod, cls = trackers.TYPES[cfg["type"]].split(":")
    return getattr(importlib.import_module(mod), cls)(cfg)


def _live_cards(cards):
    reg = agents.registry()
    return [c["id"] for c in cards if isinstance(c["metadata"].get("worker"), dict)
            and agents.worker_status(c["metadata"]["worker"], reg)[0] in ("alive", "starting")]


def switch(cfg):
    path = C.path()
    old = path.read_bytes()
    doc = tomlkit.parse(old.decode())
    bak = path.with_name(f"config.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with os.fdopen(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(old)
    if "tracker" in doc:
        doc["tracker_previous"] = doc["tracker"]
        del doc["tracker"]
    doc["tracker"] = cfg
    C.save(doc, path)


def _fail(message, code):
    print(f"pl board migrate: {message}", file=sys.stderr)
    raise SystemExit(code)


def _rule_lines(results):
    return [f"{'PASS' if r['ok'] else 'FAIL'} {r['id']} {r['text']}" + (f": {r['reason']}" if r["reason"] and not r["ok"] else "")
            for r in results]


def cmd_migrate(a):
    if not a.to:
        _fail("give --to local, a URL, or a server name", 2)
    dst_cfg = target_cfg(a.to, a)
    src = trackers.get("tracker")
    cards = src.cards()
    if live := _live_cards(cards):
        _fail(f"agents are working on {', '.join(live)}: wait for these agents or pl restart them", 1)
    dst = _build(dst_cfg)
    log = (lambda line: print(line, file=sys.stderr)) if a.json else print
    if not a.skip_check:
        results = boardcheck.check(dst)
        failed = [r for r in results if not r["ok"]]
        if a.dry_run:
            for line in _rule_lines(results):
                log(line)
        elif failed:
            _fail(f"{a.to} fails pl board check; nothing copied:\n  " + "\n  ".join(_rule_lines(failed)), 1)
    state_file = C.STATE_DIR / "board-migrate.json"
    if not (a.dry_run or a.yes):
        if not sys.stdin.isatty():
            _fail("this copies every card; pass --yes to confirm", 2)
        if input(f"Copy {len(cards)} cards to {a.to} and switch? [y/N]: ").strip().lower() not in ("y", "yes"):
            _fail("nothing copied", 1)
    todo = copy(src, dst, state_path=state_file, dry=a.dry_run, progress=log, src_key=str(C.TRACKER.get("type", "")))
    switched = False
    if not a.dry_run:
        if diffs := verify(src, dst, todo["mapping"]):
            _fail("the copy differs from the old board; [tracker] is unchanged:\n  " + "\n  ".join(diffs), 1)
        switch(dst_cfg)
        switched = True
        log(f"[tracker] now points at {a.to}; the old board is unchanged")
    if a.json:
        print(json.dumps({"columns_created": len(todo["columns"]), "cards_copied": len(todo["cards"]),
                          "cards_refreshed": len(todo["refresh"]), "switched": switched, "state_file": str(state_file)}))
