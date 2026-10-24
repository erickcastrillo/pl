"""pl question and pl answer: stage agents ask the owner questions; the owner answers through inbox/chat."""
import hashlib
import json
import os
import sys
import time
from typing import Any

from pl import config as C
from pl import board, events, move_agent, util


def _send_chat(card: dict[str, Any], question_text: str, stage: str | None = None) -> int:
    """The one chat delivery seam of this module; tests fake it."""
    try:
        from pl import chat
        conns = chat.load()
        q_conns = [(n, cfg) for n, cfg in conns.items() if "questions" in cfg.get("routes", ["questions", "gates"])]
        if not q_conns:
            return 0
        cid = card.get("id") or ""
        short = util.short_id(cid)
        title = card.get("title") or ""
        text = f"Question on {short} ({title}):\n{question_text}"
        buttons = [[("Reply", f"reply:{cid}")]]
        sent = 0
        for name, cfg in q_conns:
            try:
                ad = chat.adapter(name)
            except Exception:
                continue
            for chat_id in cfg.get("allow", []):
                try:
                    ad.send(str(chat_id), text, buttons=buttons)
                    sent += 1
                except Exception:
                    pass
        return sent
    except Exception:
        return 0


def cmd_question(a) -> None:
    """pl question <id> "text" [--json]: ask the owner a question; ends the agent's step."""
    text = (getattr(a, "text", "") or "").strip()
    if not text:
        raise SystemExit("pl question: question text cannot be empty")
    c = board.find_card(a.id)
    cid = c["id"]
    m = c.get("metadata") or {}
    w = m.get("worker") or {}
    stage = w.get("stage") or os.environ.get("PL_STAGE")
    if not stage:
        from pl import dispatch
        stage = dispatch.stage_for(c, board.col_name(c.get("list_id"))) or "spec"

    parts = board.sections(c.get("description"))
    q_sec = parts.get("QUESTIONS", "").strip()
    new_entry = f"Q: {text}"
    parts["QUESTIONS"] = f"{q_sec}\n\n{new_entry}" if q_sec else new_entry

    asked_at = util.now_iso()
    text_sha = hashlib.sha256(text.encode()).hexdigest()
    q_meta = {"asked_at": asked_at, "stage": stage, "text_sha": text_sha}

    new_meta: dict[str, Any] = {"question": q_meta, "worker": None}
    if w.get("window"):
        new_meta["finished_workers"] = list(m.get("finished_workers") or []) + [w]

    if w.get("pane") and os.environ.get("TMUX_PANE") != w.get("pane"):
        try:
            move_agent.stop(w["pane"], w.get("session_id"))
        except Exception:
            pass

    board.update(cid, description=board.check_size(board.render(parts)), metadata=new_meta)
    events.emit("question", cid, stage=stage)
    _send_chat(c, text, stage=stage)

    if getattr(a, "json", False):
        print(util.mask(json.dumps({"card": cid, "stage": stage, "question": text, "asked_at": asked_at, "text_sha": text_sha})))
    else:
        print(f"asked question for {util.short_id(cid)}: {text}")


def cmd_answer(a) -> None:
    """pl answer <id> "text" [--json]: answer a card's open question so its stage restarts."""
    text = (getattr(a, "text", "") or "").strip()
    if not text:
        raise SystemExit("pl answer: answer text cannot be empty")
    c = board.find_card(a.id)
    cid = c["id"]
    m = c.get("metadata") or {}
    if not m.get("question"):
        print(f"pl answer: card {util.short_id(cid)} has no open question", file=sys.stderr)
        raise SystemExit(2)

    parts = board.sections(c.get("description"))
    q_sec = parts.get("QUESTIONS", "").strip()
    new_entry = f"A: {text}"
    parts["QUESTIONS"] = f"{q_sec}\n{new_entry}" if q_sec else new_entry

    new_meta: dict[str, Any] = {"question": None, "answered": 1}
    board.update(cid, description=board.check_size(board.render(parts)), metadata=new_meta)
    events.emit("answered", cid)

    if getattr(a, "json", False):
        print(util.mask(json.dumps({"card": cid, "answered": text})))
    else:
        print(f"answered question for {util.short_id(cid)}")


def prompt_arg(c: dict[str, Any]) -> str:
    """Restart arg for prompt when a question was answered."""
    return " answered=1" if (c.get("metadata") or {}).get("answered") else ""


def inbox_items(cs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return inbox items for cards with open questions in this profile."""
    if cs is None:
        cs = board.cards()
    items = []
    now = time.time()
    prof = C.PROFILE_NAME or ""
    for c in cs:
        m = c.get("metadata") or {}
        q = m.get("question")
        if not q or not isinstance(q, dict):
            continue
        cid = c.get("id", "")
        desc = c.get("description") or ""
        secs = board.sections(desc)
        q_sec = secs.get("QUESTIONS", "")
        asked_at = q.get("asked_at") or ""
        since_ts = util.parse_iso(asked_at) if asked_at else 0
        age_seconds = max(0, int(now - since_ts)) if since_ts else 0
        c_hint = util.cmd_id(cid)
        cmd = f"pl --profile {prof} answer {c_hint} \"\"" if prof else f"pl answer {c_hint} \"\""
        try:
            url = util.card_url(cid)
        except Exception:
            url = ""
        items.append({
            "kind": "question",
            "pipeline": prof,
            "card": cid,
            "stage": q.get("stage"),
            "summary": (c.get("title") or "")[:80],
            "question": q_sec.strip(),
            "since": asked_at,
            "age_seconds": age_seconds,
            "url": url,
            "command": cmd,
        })
    return items
