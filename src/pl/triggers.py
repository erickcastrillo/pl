"""Triggers: automations that turn events or timers into pipeline cards."""
import re
import time

from pl import board, trackers
from pl import config as C
from pl.util import parse_iso

UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_after(val):
    if isinstance(val, (int, float)):
        return float(val)
    m = re.fullmatch(r"(\d+)([smhd])", str(val).strip()) if val else None
    return float(int(m.group(1)) * UNIT[m.group(2)]) if m else 0.0


def call_tool(connector, tool, args, mode="trigger"):
    try:
        from pl import connectors
        if hasattr(connectors, "call"):
            return connectors.call(connector, tool, args, mode=mode)
    except ImportError:
        pass
    raise NotImplementedError(f"no connector runner for {connector}:{tool}")


def _extract_time(msg):
    if not isinstance(msg, dict):
        return None
    val = msg.get("date") or msg.get("timestamp") or msg.get("created_at") or msg.get("time") or msg.get("at")
    return float(val) if isinstance(val, (int, float)) else parse_iso(val) if isinstance(val, str) else None


def last_inbound_message(res):
    if not res:
        return None
    if isinstance(res, dict) and "last_inbound" in res:
        return _extract_time(res["last_inbound"])
    msgs = res.get("messages") or res.get("items") if isinstance(res, dict) else res if isinstance(res, list) else None
    if msgs is None and isinstance(res, dict):
        msgs = [res]
    times = [_extract_time(m) for m in (msgs or []) if isinstance(m, dict) and
             (m.get("direction") in ("inbound", "in", "received") or m.get("inbound") is True or
              (m.get("from") and not m.get("outbound") and m.get("direction") != "outbound"))]
    valid = [t for t in times if t is not None]
    return max(valid) if valid else None


def entered_column_at(card):
    meta = card.get("metadata") or {}
    for k in ("entered_column_at", "entered_at", "moved_at"):
        if meta.get(k):
            return parse_iso(meta[k]) if isinstance(meta[k], str) else float(meta[k])
    return parse_iso(card["updated_at"]) if card.get("updated_at") else None


def get_triggers():
    if hasattr(C, "TRIGGERS") and isinstance(C.TRIGGERS, dict):
        return C.TRIGGERS
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg_file := p / "config.toml").is_file():
        try:
            import tomllib
            return tomllib.loads(cfg_file.read_text()).get("triggers", {})
        except Exception:
            pass
    return {}


def run_silence(name, cfg, cards=None, dry=False, now=None):
    now = time.time() if now is None else float(now)
    after_seconds = parse_after(cfg.get("after", "5d"))
    watch_col = cfg.get("column", "Proposal sent")
    target_col = cfg.get("target_column") or cfg.get("skill") or "Inbox"
    connector = cfg.get("connector", "email")
    cards = board.cards() if cards is None else cards

    col_dest = target_col
    try:
        col_dest = next((k for k in board.lists() if k.lower() == str(target_col).lower()), target_col)
    except Exception:
        pass

    created, existing = [], {m["followup_of"] for c in cards if (m := c.get("metadata") or {}).get("followup_of")}

    for c in cards:
        if (board.col_name(c["list_id"]) if "list_id" in c else c.get("column")) != watch_col:
            continue
        cid, meta = c.get("id"), dict(c.get("metadata") or {})
        if meta.get("followup_of") or cid in existing:
            continue
        entered = entered_column_at(c)
        if entered is None or (now - entered) < after_seconds:
            continue

        refs = meta.get("refs") or {}
        thread_id = refs.get("thread") or meta.get("thread_id") or meta.get("thread") or refs.get("email") or cid
        try:
            res = call_tool(connector, "get_thread", {"thread_id": thread_id}, mode="trigger")
        except Exception:
            res = None

        if (last_in := last_inbound_message(res)) is not None and last_in > entered:
            continue

        title = f"Follow-up: {c.get('title', '')}".strip()
        desc = board.render({"INPUT": f"Follow-up on {c.get('title', '')}\nProposal card: {cid}\n"})
        new_meta = {
            "pipeline_mode": "auto", "followup_of": cid, "source": f"trigger:{name}",
            "links": [{"card": cid, "relation": "followup-of"}], "refs": dict(refs),
        }
        if dry:
            created.append({"id": f"dry-{len(created)+1}", "title": title, "column": col_dest, "metadata": new_meta})
            existing.add(cid)
            continue

        new_c = trackers.get("tracker").create(col_dest, title=title, description=board.check_size(desc),
                                               tags=["followup"], metadata=new_meta)
        created.append(new_c)
        existing.add(cid)
        links = list(meta.get("links") or []) + [{"card": new_c["id"], "relation": "followed-up-by"}]
        board.update(cid, metadata={"followup_of": new_c["id"], "links": links})

    return created


def run_due(dry=False, now=None):
    return [c for n, cfg in get_triggers().items() if cfg.get("kind") == "silence"
            for c in run_silence(n, cfg, dry=dry, now=now)]
