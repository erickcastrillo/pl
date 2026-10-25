import json
import time
import uuid
from pathlib import Path
from typing import Any

from pl import chat
from pl.chat import inbound, safe_text


def pending_gates() -> list[dict[str, Any]]:
    """Return waiting gates across profiles."""
    try:
        from pl import inbox
        return inbox.listing()
    except (ImportError, AttributeError):
        pass
    rows: list[dict[str, Any]] = []
    try:
        from pl import profiles
        profs = profiles.list_profiles(check_lock=False)
    except Exception:
        profs = []
    for p in profs:
        try:
            items = json.loads((Path(p.get("dir", "")) / "state" / "inbox.json").read_text()).get("items")
            if isinstance(items, list):
                rows.extend(it for it in items if isinstance(it, dict))
        except (OSError, ValueError):
            pass
    return rows


def post_gates() -> int:
    """Inspect waiting gates across profiles and post unposted gates to chat connectors."""
    conns = chat.load()
    gate_conns = [(n, c) for n, c in conns.items() if "gates" in c.get("routes", ["gates"])]
    if not gate_conns or not (gates := pending_gates()):
        return 0

    st = inbound.load_state()
    posted, nonces = set(st.get("posted_gates") or []), st.setdefault("nonces", {})
    sent, now = 0, time.time()

    for row in gates:
        pipe, card, gate = row.get("pipeline") or "", row.get("card") or "", row.get("gate") or ""
        key = f"{pipe}:{card}:{gate}"
        if key in posted:
            continue
        summary, url = safe_text(row.get("summary") or ""), row.get("url") or ""
        text = f"{gate.title()} gate: {summary}\n{url}".strip() if url else f"{gate.title()} gate: {summary}"

        for name, cfg in gate_conns:
            try:
                ad = chat.adapter(name)
            except Exception:
                continue
            for chat_id in cfg.get("allow", []):
                app_nonce, sb_nonce = f"app-{uuid.uuid4().hex[:10]}", f"sb-{uuid.uuid4().hex[:10]}"
                nonces[app_nonce] = {"action": "approve", "card": card, "profile": pipe, "chat_id": str(chat_id), "created_at": now}
                nonces[sb_nonce] = {"action": "send_back", "card": card, "profile": pipe, "chat_id": str(chat_id), "created_at": now}
                buttons = [[("Approve", app_nonce), {"text": "Edit", "url": url or "#"}, ("Send back", sb_nonce)]]
                try:
                    ad.send(str(chat_id), text, buttons=buttons)
                    sent += 1
                except Exception:
                    pass
        posted.add(key)

    st["posted_gates"] = list(posted)
    inbound.save_state(st)
    return sent
