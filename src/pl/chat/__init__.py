"""Messaging and chat connectors for pl."""
from typing import Any

from pl import manager


def load() -> dict[str, dict[str, Any]]:
    conns = manager._machine_toml().get("connectors", {})
    return {name: dict(cfg) for name, cfg in conns.items() if isinstance(cfg, dict) and cfg.get("role") == "messaging"}


def adapter(name: str):
    connectors = load()
    if name in connectors:
        cfg = connectors[name]
        ad_type = cfg.get("adapter", name)
        if ad_type == "telegram":
            from pl.chat.telegram import Telegram
            return Telegram(name, cfg)
        raise KeyError(f"pl chat: unknown adapter {ad_type!r}")
    raise KeyError(f"pl chat: unknown connector {name!r}")
