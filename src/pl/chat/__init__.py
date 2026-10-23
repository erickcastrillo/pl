import re
from typing import Any

from pl import manager

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MONEY_RE = re.compile(r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)")
ADDRESS_RE = re.compile(r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b", re.IGNORECASE)
DIGITS_RE = re.compile(r"\b\d{5,}\b")


def safe_text(s: str) -> str:
    from pl import util
    s = util.mask(str(s))
    for rx in (EMAIL_RE, MONEY_RE, ADDRESS_RE, DIGITS_RE):
        s = rx.sub("…", s)
    return s[:300]


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
    if name == "telegram":
        from pl.chat.telegram import Telegram
        return Telegram
    raise KeyError(f"pl chat: unknown connector or adapter {name!r}")
