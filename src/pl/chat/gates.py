import json
from pl import manager


def post_outbox() -> int:
    """Read pending alerts from chat-outbox.jsonl, send to connectors routed for alerts, and truncate the file."""
    from pl import chat

    outbox = manager.machine_dir() / "chat-outbox.jsonl"
    if not outbox.exists():
        return 0

    try:
        with open(outbox, "r+", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
            f.seek(0)
            f.truncate()
    except OSError:
        return 0

    if not lines:
        return 0

    conns = chat.load()
    alert_conns = [
        (name, cfg)
        for name, cfg in conns.items()
        if "alerts" in cfg.get("routes", [])
    ]
    if not alert_conns:
        return 0

    sent = 0
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        text = chat.safe_text(entry.get("headline", ""))
        for name, cfg in alert_conns:
            try:
                ad = chat.adapter(name)
                for chat_id in cfg.get("allow", []):
                    ad.send(str(chat_id), text)
                    sent += 1
            except Exception:
                pass
    return sent
