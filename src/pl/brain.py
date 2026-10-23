"""Central brain: ask questions and save decisions."""
import json
import os
import re
import time
from typing import Any
from pl import config as C, local_model, recall
from pl.manager import machine_dir
from pl.util import short_id

DISTIL_SYSTEM = (
    "Extract at most 5 durable decisions, facts, commitments, or user preferences from the user's messages. "
    "Only use statements made by the user. Do not include tool results, temporary details, or queries. "
    "Output ONLY a JSON array of objects with keys 'kind' (one of: 'decision', 'fact', 'commitment', 'preference') "
    "and 'text' (a concise statement). If nothing durable was decided or stated, return []."
)


def _conv_path(conv: str):
    d = machine_dir() / "brain" / "conversations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{conv}.jsonl"


def _read_conv(conv: str) -> list[dict]:
    p = _conv_path(conv)
    if not p.exists():
        return []
    try:
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:
        return []


def _append_conv(conv: str, item: dict):
    p = _conv_path(conv)
    if not p.exists():
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)
    with open(p, "a") as f:
        f.write(json.dumps(item) + "\n")


def _distil_model(user_text: str) -> list[dict]:
    ans = local_model.ask(DISTIL_SYSTEM, user_text)
    if not ans:
        return []
    try:
        m = re.search(r"\[.*\]", ans, re.DOTALL)
        items = json.loads(m.group(0) if m else ans)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    valid = []
    for it in items[:5]:
        if isinstance(it, dict) and it.get("text"):
            k = str(it.get("kind", "decision")).lower()
            valid.append({
                "kind": k if k in ("decision", "fact", "commitment", "preference") else "decision",
                "text": str(it["text"]),
            })
    return valid


def end(conv: str) -> str:
    turns = _read_conv(conv)
    user_turns = [str(t.get("text", "")).strip() for t in turns if t.get("role") in ("user", "owner") and t.get("text")]
    saved_ids = []
    if user_turns:
        for it in _distil_model("\n".join(user_turns))[:5]:
            try:
                eid = recall.add(text=it["text"], kind=it["kind"], source=f"brain:{conv}", link=f"pl brain show {conv}")
                if eid:
                    saved_ids.append(eid)
            except Exception:
                continue
    if saved_ids:
        _append_conv(conv, {"role": "saved", "saved_ids": saved_ids})
    return f"saved {len(saved_ids)} · undo: pl brain undo {conv}"


def remember(conv: str, text: str | None = None) -> str:
    if text is None and ("remember this" in conv.lower() or " " in conv):
        text, conv = conv, "default"
    clean = re.sub(r"^remember\s+this[:\s]*", "", text or "", flags=re.I).strip() or (text or "").strip()
    kind = "decision" if "decision" in clean.lower() else "preference"
    eid = recall.add(clean, kind=kind, source=f"brain:{conv}", link=f"pl brain show {conv}")
    _append_conv(conv, {"role": "saved", "saved_ids": [eid]})
    return f"saved 1 · undo: pl brain undo {conv}"


def undo(conv: str) -> str:
    turns = _read_conv(conv)
    saved, undone = [], set()
    for t in turns:
        if t.get("role") == "saved" and isinstance(t.get("saved_ids"), list):
            saved.extend(t["saved_ids"])
        elif t.get("role") == "undo" and isinstance(t.get("undone_ids"), list):
            undone.update(t["undone_ids"])
    to_undo = [eid for eid in saved if eid not in undone]
    for eid in to_undo:
        recall.forget(eid)
    if to_undo:
        _append_conv(conv, {"role": "undo", "undone_ids": to_undo})
    return f"undone {len(to_undo)}"


def check_idle(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    idle = C.BRAIN.get("idle_end", 30) * 60 if hasattr(C, "BRAIN") and isinstance(C.BRAIN, dict) else 1800
    d = machine_dir() / "brain" / "conversations"
    ended = []
    if d.exists():
        for p in d.glob("*.jsonl"):
            turns = _read_conv(p.stem)
            if turns and not any(t.get("role") in ("saved", "undo") for t in turns) and now - p.stat().st_mtime >= idle:
                end(p.stem)
                ended.append(p.stem)
    return ended


def cmd_brain(a):
    if a.brain_cmd in ("end", "undo"):
        print(end(a.conv) if a.brain_cmd == "end" else undo(a.conv))
    else:
        raise SystemExit("usage: pl brain {end,undo} <conv>")


def ask(text: str, conv: str | None = None) -> dict[str, Any]:
    return {"text": f"Answer for: {text}", "sources": [], "conversation": conv or f"ask-{short_id()}"}
