from datetime import datetime
import json
import os
from pathlib import Path
import sys

from pl import config as C
from pl.trackers.mcp import Mcp


def _call(tracker, tool_name, args):
    res, _ = tracker._run(lambda s: s.call_tool(tool_name, args))
    texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
    return res, texts[0] if texts else ""


def _call_json(tracker, tool_name, args):
    res, text = _call(tracker, tool_name, args)
    if res.isError:
        raise ValueError(f"{tool_name} failed: {text}")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        raise ValueError(f"{tool_name} returned non-JSON: {text[:100]}")


def _ensure_col(tracker):
    col_id = getattr(tracker, "_check_col_id", None)
    if col_id:
        return col_id
    res = _call_json(tracker, "pl_ensure_column", {"board_id": tracker.board_id, "title": "pl check"})
    cid = (res.get("column") or {}).get("id")
    if not cid:
        raise ValueError("could not ensure column 'pl check'")
    tracker._check_col_id = cid
    return cid


def _create(tracker, title="card", **kw):
    cid = _ensure_col(tracker)
    res = _call_json(tracker, "pl_create", {"board_id": tracker.board_id, "column_id": cid, "title": title, **kw})
    card = res.get("card") or {}
    if card.get("id"):
        tracker._check_cards_to_delete.add(card["id"])
    return card


def r1(tracker):
    res = _call_json(tracker, "pl_protocol", {})
    if res.get("version") != 1:
        raise ValueError(f"expected version 1, got {res.get('version')}")
    boards = res.get("boards")
    if not isinstance(boards, list) or not boards:
        raise ValueError("boards list missing or empty")
    for b in boards:
        if not isinstance(b, dict) or not b.get("id") or not b.get("title"):
            raise ValueError("board row missing id or title")
    if not tracker.board_id:
        tracker.board_id = boards[0]["id"]


def r2(tracker):
    cols = _call_json(tracker, "pl_columns", {"board_id": tracker.board_id}).get("columns")
    if not isinstance(cols, list) or not cols:
        raise ValueError("columns list missing or empty")
    for c in cols:
        if not isinstance(c, dict) or "id" not in c or "title" not in c:
            raise ValueError("column row missing id or title")


def r3(tracker):
    card = _create(tracker, title="Rule 3 Card", description="desc", tags=["r3"],
                   metadata={"check": "r3"}, assigned_to="tester")
    missing = {"id", "title", "description", "tags", "metadata", "list_id", "updated_at", "assigned_to"} - set(card)
    if missing:
        raise ValueError(f"card missing keys: {', '.join(sorted(missing))}")
    up = card.get("updated_at") or ""
    dt = datetime.fromisoformat(up.replace("Z", "+00:00")) if up else None
    if not dt or dt.tzinfo is None:
        raise ValueError(f"updated_at has no timezone offset: {up}")


def r4(tracker):
    meta = {f"k_{i}": f"v_{i}" for i in range(40)}
    meta["nested"] = {"sub1": 1, "sub2": [1, 2, "three"], "sub3": {"deep": True}}
    meta["unicode"] = "日本語 / café / 🚀 / €"
    card = _create(tracker, title="Rule 4 Card", metadata=meta)
    read_meta = (_call_json(tracker, "pl_card", {"board_id": tracker.board_id, "id": card["id"]}).get("card") or {}).get("metadata")
    if read_meta != meta:
        dropped = set(meta) - set(read_meta) if isinstance(read_meta, dict) else set(meta)
        raise ValueError(f"metadata dropped keys: {', '.join(sorted(dropped))}" if dropped else "metadata round-trip mismatch")


def r5(tracker):
    cid = _create(tracker, title="R5 Old Title", description="R5 Old Desc", metadata={"old1": "v1", "old2": "v2"})["id"]
    _call_json(tracker, "pl_update", {"board_id": tracker.board_id, "id": cid, "fields": {"title": "R5 New Title", "metadata": {"new1": "v3"}}})
    read = (_call_json(tracker, "pl_card", {"board_id": tracker.board_id, "id": cid}).get("card") or {})
    if read.get("title") != "R5 New Title" or read.get("description") != "R5 Old Desc":
        raise ValueError(f"fields not merged: title={read.get('title')!r}, desc={read.get('description')!r}")
    if read.get("metadata") != {"new1": "v3"}:
        raise ValueError(f"metadata not replaced whole: {read.get('metadata')!r}")


def r6(tracker):
    created = [_create(tracker, title=f"R6 Card {i}")["id"] for i in range(1, 6)]
    seen, cursor, pages = [], None, 0
    while True:
        args = {"board_id": tracker.board_id, "limit": 2, **({"cursor": cursor} if cursor else {})}
        res = _call_json(tracker, "pl_cards", args)
        cards = res.get("cards") or []
        pages += 1
        if len(cards) > 2:
            raise ValueError(f"page returned {len(cards)} cards, exceeding limit=2")
        seen.extend(cards)
        cursor = res.get("next_cursor")
        if not cursor:
            break
        if pages > 50:
            raise ValueError("paging exceeded 50 pages")
    seen_ids = [c["id"] for c in seen]
    if len(seen_ids) != len(set(seen_ids)):
        raise ValueError(f"duplicate cards in paging: {set(seen_ids)}")
    if any(cid not in seen_ids for cid in created):
        raise ValueError("not all created cards found in paged results")


def r7(tracker):
    cols = _call_json(tracker, "pl_columns", {"board_id": tracker.board_id}).get("columns") or []
    cur_col = _ensure_col(tracker)
    other = next((c["id"] for c in cols if c["id"] != cur_col), None)
    if not other:
        other = (_call_json(tracker, "pl_ensure_column", {"board_id": tracker.board_id, "title": "pl check alt"}).get("column") or {}).get("id")
    cid = _create(tracker, title="R7 Move Card")["id"]
    _call_json(tracker, "pl_move", {"board_id": tracker.board_id, "id": cid, "column_id": other})
    read = (_call_json(tracker, "pl_card", {"board_id": tracker.board_id, "id": cid}).get("card") or {})
    if read.get("list_id") != other:
        raise ValueError(f"list_id after move is {read.get('list_id')!r}, expected {other!r}")


def r8(tracker):
    res, text = _call(tracker, "pl_card", {"board_id": tracker.board_id, "id": "unknown-nonexistent-id-99999"})
    if not res.isError:
        raise ValueError("pl_card succeeded for unknown id")
    if "not_found" not in text.lower():
        raise ValueError(f"expected error containing not_found, got: {text}")


def r9(tracker):
    res, text = _call(tracker, "pl_create", {"board_id": tracker.board_id, "column_id": "unknown-nonexistent-col-99999", "title": "Bad Col"})
    if not res.isError:
        raise ValueError("pl_create succeeded for unknown column")
    if "invalid" not in text.lower():
        raise ValueError(f"expected error containing invalid, got: {text}")


def r10(tracker):
    c1 = (_call_json(tracker, "pl_ensure_column", {"board_id": tracker.board_id, "title": "pl check"}).get("column") or {}).get("id")
    c2 = (_call_json(tracker, "pl_ensure_column", {"board_id": tracker.board_id, "title": "pl check"}).get("column") or {}).get("id")
    if not c1 or c1 != c2:
        raise ValueError(f"pl_ensure_column not idempotent: {c1!r} != {c2!r}")


def r11(tracker):
    cid = _create(tracker, title="R11 Delete Card")["id"]
    del_res = _call_json(tracker, "pl_delete", {"board_id": tracker.board_id, "id": cid})
    if del_res.get("deleted") is not True:
        raise ValueError(f"pl_delete did not return {{'deleted': True}}: {del_res}")
    tracker._check_cards_to_delete.discard(cid)
    read_res, text = _call(tracker, "pl_card", {"board_id": tracker.board_id, "id": cid})
    if not read_res.isError or "not_found" not in text.lower():
        raise ValueError(f"card not gone after delete: {text}")


RULES = [
    ("R1", "pl_protocol returns version 1 and boards", r1),
    ("R2", "pl_columns returns {id, title} rows", r2),
    ("R3", "pl_create returns a card with every key and an updated_at that carries an offset", r3),
    ("R4", "metadata round-trips exactly (nested, unicode, 40 keys)", r4),
    ("R5", "pl_update merges fields and replaces metadata whole", r5),
    ("R6", "paging returns every card once with limit=2 and 5 cards", r6),
    ("R7", "pl_move changes list_id", r7),
    ("R8", "an unknown id errors with not_found", r8),
    ("R9", "an unknown column errors with invalid", r9),
    ("R10", "pl_ensure_column is idempotent", r10),
    ("R11", "pl_delete returns {deleted: true} and the card is gone", r11),
]


def check(tracker) -> list[dict]:
    tracker._check_cards_to_delete = set()
    tracker._check_col_id = None
    results = []
    try:
        for rid, rtext, rfn in RULES:
            try:
                rfn(tracker)
                results.append({"id": rid, "text": rtext, "ok": True, "reason": None})
            except Exception as e:
                results.append({"id": rid, "text": rtext, "ok": False, "reason": str(e) or type(e).__name__})
    finally:
        for cid in list(getattr(tracker, "_check_cards_to_delete", [])):
            try:
                _call(tracker, "pl_delete", {"board_id": tracker.board_id, "id": cid})
            except Exception:
                pass
    return results


def cmd_check(a):
    server = getattr(a, "server", None)
    if not server:
        print("pl board check: give local, a URL, or a server name in MCP config", file=sys.stderr)
        sys.exit(2)

    board_id = getattr(a, "board", None) or ""

    if server == "local":
        env = dict(os.environ)
        if C.CONFIG_DIR:
            env["PL_CONFIG_DIR"] = str(C.CONFIG_DIR)
        cfg = {"type": "mcp", "protocol": "pl",
               "server": {"command": sys.executable, "args": ["-m", "pl", "board", "serve", "--stdio"], "env": env}}
        if board_id:
            cfg["board_id"] = board_id

    elif server.startswith("http://") or server.startswith("https://"):
        headers = {}
        for item in (getattr(a, "header_env", None) or []):
            if "=" not in item:
                print(f"pl board check: --header-env must be HEADER=VAR, got {item!r}", file=sys.stderr)
                sys.exit(2)
            hdr, var = item.split("=", 1)
            headers[hdr] = f"${{{var}}}"
        cfg = {"type": "mcp", "protocol": "pl", "server": {"url": server, "headers": headers}}
        if board_id:
            cfg["board_id"] = board_id

    else:
        config_path = getattr(a, "mcp_config", None) or (C.TRACKER.get("mcp_config") if getattr(C, "TRACKER", None) else None)
        if not config_path:
            print(f"pl board check: give --mcp-config PATH to find server {server!r}", file=sys.stderr)
            sys.exit(2)
        p = Path(config_path).expanduser()
        if not p.is_file():
            print(f"pl board check: no such MCP config file {p}", file=sys.stderr)
            sys.exit(2)
        try:
            data = json.loads(p.read_text())
            servers = data.get("mcpServers") or {}
            if not isinstance(servers, dict) or server not in servers:
                print(f"pl board check: {p} has no MCP server {server!r}", file=sys.stderr)
                sys.exit(2)
        except Exception as e:
            print(f"pl board check: cannot read MCP config {p}: {e}", file=sys.stderr)
            sys.exit(2)

        cfg = {"type": "mcp", "protocol": "pl", "mcp_config": str(p), "server": server}
        if board_id:
            cfg["board_id"] = board_id

    try:
        tracker = Mcp(cfg)
    except SystemExit as e:
        print(f"pl board check: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"pl board check: {e}", file=sys.stderr)
        sys.exit(2)

    results = check(tracker)

    if getattr(a, "json", False):
        out = {"server": server, "rules": [{"id": r["id"], "ok": r["ok"], "reason": r["reason"]} for r in results]}
        print(json.dumps(out))
    else:
        for r in results:
            if r["ok"]:
                print(f"PASS {r['id']} {r['text']}")
            else:
                reason = f": {r['reason']}" if r["reason"] else ""
                print(f"FAIL {r['id']} {r['text']}{reason}")

    sys.exit(0 if all(r["ok"] for r in results) else 1)
