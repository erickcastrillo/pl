"""Pods: pipeline context, knowledge, rules and learning from draft edits."""
import hashlib
import json
import os
import re
from pathlib import Path

from pl import config as C
from pl.util import now_iso, short_id

RULE_MIN_SEEN = 3
RULE_WINDOW_DAYS = 30
NORM_NUM_RE = re.compile(r"\d+")
NORM_SPACE_RE = re.compile(r"\s+")
MARKDOWN_LEAD_RE = re.compile(r"^([-*•#]+|\d+[.)])\s+")


def draft_path(card, section) -> Path:
    """The local snapshot file of a draft when the card reached a gate column."""
    name = f"{str(card).replace('/', '_')}-{section}.md"
    p = C.STATE_DIR / "drafts" / name
    if p.is_file():
        return p
    short = f"{short_id(str(card)).replace('/', '_')}-{section}.md"
    p_short = C.STATE_DIR / "drafts" / short
    return p_short if p_short.is_file() else p


def save_draft(card, section, text: str) -> Path:
    """Snapshot a draft section (0600) when reaching a gate column."""
    d = C.STATE_DIR / "drafts"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / f"{str(card).replace('/', '_')}-{section}.md"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text or "")
    return p


def split_sentences(text: str) -> list[str]:
    """Split text into sentences or list items."""
    if not text:
        return []
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    out = []
    for chunk in chunks:
        s = chunk.strip()
        s = MARKDOWN_LEAD_RE.sub("", s).strip()
        if s:
            out.append(s)
    return out


def normalize_sentence(sentence: str) -> str:
    """Normalize a sentence: lowercase, whitespace collapsed, numbers replaced with '#'."""
    s = sentence.lower().strip()
    s = NORM_NUM_RE.sub("#", s)
    s = NORM_SPACE_RE.sub(" ", s)
    return s.strip()


def sentence_hash(norm: str) -> str:
    """Hash of a normalized sentence."""
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def pod_of(card=None, explicit_pod=None) -> str:
    """The pod name for a card or current pipeline."""
    if explicit_pod:
        return explicit_pod
    if isinstance(card, dict):
        m = card.get("metadata") or {}
        if m.get("pod"):
            return m["pod"]
    pipe = getattr(C, "PIPELINE", {}) or {}
    if pipe.get("pod"):
        return pipe["pod"]
    return getattr(C, "PROFILE_NAME", None) or "engineering"


def _edits_path() -> Path:
    return C.STATE_DIR / "pod-edits.jsonl"


def _record_edit(pod: str, s_hash: str, sentence: str, card, ts: str):
    C.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = _edits_path()
    line = json.dumps({"pod": pod, "sentence_hash": s_hash, "sentence": sentence, "card": str(card), "ts": ts})
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def learn(card, section, draft, approved, pod=None):
    """Compare draft and approved text, recording removed sentences."""
    pod_name = pod_of(card, explicit_pod=pod)
    draft_sentences = split_sentences(draft)
    approved_sentences = split_sentences(approved)
    approved_norm = {normalize_sentence(s) for s in approved_sentences}

    seen_norm = set()
    ts = now_iso()
    for s in draft_sentences:
        norm = normalize_sentence(s)
        if norm and norm not in approved_norm and norm not in seen_norm:
            seen_norm.add(norm)
            h = sentence_hash(norm)
            _record_edit(pod_name, h, s, card, ts)
