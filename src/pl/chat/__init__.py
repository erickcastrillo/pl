"""Messaging and chat connectors for pl."""
import re

RX = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)"),
    re.compile(r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b", re.I),
    re.compile(r"\b\d{5,}\b"),
)


def safe_text(s: str) -> str:
    from pl import util
    s = util.mask(str(s))
    for r in RX:
        s = r.sub("…", s)
    return s[:300]
