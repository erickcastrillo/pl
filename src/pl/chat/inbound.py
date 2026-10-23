"""Inbound message handling for chat connectors."""
import json
import os
import subprocess
import sys
import threading
from typing import Any

from pl import audit, manager


def _state():
    return manager.machine_dir() / "chat-state.json"


def _load():
    try:
        return json.loads(_state().read_text())
    except Exception:
        return {"prompts": {}}


def _save(st):
    p = _state()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(json.dumps(st))


def _pl(argv, env=None):
    proc = subprocess.run(
        [sys.executable, "-m", "pl", *argv],
        capture_output=True,
        text=True,
        env={**os.environ, "PL_SOURCE": "chat", **(env or {})},
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def handle_update(adapter: Any, update: dict[str, Any]) -> None:
    if "callback_query" in update:
        cb = update["callback_query"]
        sender = str(cb.get("from", {}).get("id", ""))
        allowed = [str(x) for x in getattr(adapter, "allow", [])]
        if allowed and sender not in allowed:
            audit.record("chat_unknown_sender", getattr(adapter, "name", "chat"), sender)
            return
        adapter.answer_callback(cb.get("id", ""))
    elif "message" in update:
        _handle_message(adapter, update["message"])


def _handle_message(adapter: Any, msg: dict[str, Any]) -> None:
    sender = str(msg.get("from", {}).get("id", ""))
    allowed = [str(x) for x in getattr(adapter, "allow", [])]
    if allowed and sender not in allowed:
        audit.record("chat_unknown_sender", getattr(adapter, "name", "chat"), sender)
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    reply_to = msg.get("reply_to_message")

    if text == "/stop":
        adapter.send(chat_id, "Kill switch turned on")
        return
    if text.startswith("/stop"):
        adapter.send(chat_id, "Kill switch cannot be turned off from chat; use the console or CLI")
        return

    if reply_to:
        reply_id = str(reply_to.get("message_id", ""))
        st = _load()
        prompt = st.get("prompts", {}).pop(reply_id, None)
        if prompt:
            _save(st)
            _pl(["--profile", prompt.get("profile", "default"), "reject", prompt.get("card", ""), text[:4000]])
            adapter.send(chat_id, f"Sent back {prompt.get('card', '')} with review notes")
            return

    if not manager._machine_toml().get("brain", {}).get("chat", False):
        adapter.send(chat_id, "I only take replies to cards")
        return

    def _worker():
        from pl import brain

        ans = (
            brain.remember(text, conv=chat_id)
            if text.lower().startswith("remember this")
            else brain.ask(text, conv=chat_id)
        )
        reply = brain.chat_reply(ans)
        if reply:
            adapter.send(chat_id, reply)

    threading.Thread(target=_worker, daemon=True).start()
