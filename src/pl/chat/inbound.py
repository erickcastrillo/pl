import datetime
import json
import os
import subprocess
import sys
import time
from typing import Any
import uuid

from pl import manager
from pl import outward


def _state():
    return manager.machine_dir() / "chat-state.json"


def load_state() -> dict[str, Any]:
    try:
        d = json.loads(_state().read_text())
        if isinstance(d, dict):
            return {**{"offset": None, "nonces": {}, "prompts": {}}, **d}
    except (OSError, ValueError):
        pass
    return {"offset": None, "nonces": {}, "prompts": {}}


def save_state(st: dict[str, Any]) -> None:
    path = _state()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(st))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def register_nonce(nonce: str, data: dict[str, Any]) -> None:
    st = load_state()
    st["nonces"][nonce] = data
    save_state(st)


def get_nonce(nonce: str) -> dict[str, Any] | None:
    return load_state().get("nonces", {}).get(nonce)


def consume_nonce(nonce: str) -> dict[str, Any] | None:
    st = load_state()
    val = st.get("nonces", {}).pop(nonce, None)
    if val is not None:
        save_state(st)
    return val


def _pl(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    full_env = {**os.environ, **(env or {}), "PL_SOURCE": "chat"}
    proc = subprocess.run([sys.executable, "-m", "pl", *argv], capture_output=True, text=True, env=full_env)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def handle_update(adapter: Any, update: dict[str, Any]) -> None:
    if "callback_query" in update:
        _handle_callback(adapter, update["callback_query"])
    elif "message" in update:
        _handle_message(adapter, update["message"])


def _run_approve(adapter: Any, chat_id: str, msg_id: Any, cb_id: str, profile: str, card: str) -> None:
    code, out = _pl(["--profile", profile, "approve", card])
    time_str = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
    status = f"Approved by you at {time_str}" if code == 0 else f"Approval failed: {out.splitlines()[0] if out else 'error'}"
    adapter.edit(chat_id, msg_id, status)
    adapter.answer_callback(cb_id)


def _handle_callback(adapter: Any, cb: dict[str, Any]) -> None:
    cb_id, sender_id = cb.get("id", ""), str(cb.get("from", {}).get("id", ""))
    if adapter.allow and sender_id not in [str(x) for x in adapter.allow]:
        return

    msg = cb.get("message") or {}
    chat_id, msg_id = str(msg.get("chat", {}).get("id", "")), msg.get("message_id")
    data = cb.get("data", "")
    nonce_data = get_nonce(data)
    if not nonce_data:
        adapter.answer_callback(cb_id)
        return

    action, card, profile = nonce_data.get("action"), nonce_data.get("card", ""), nonce_data.get("profile", "default")
    if action == "approve":
        if outward.risky(nonce_data.get("card_data") or {}):
            conf = f"conf-{uuid.uuid4().hex[:10]}"
            register_nonce(conf, {"action": "confirm_approve", "card": card, "profile": profile, "created_at": time.time(), "chat_id": chat_id, "message_id": msg_id})
            adapter.edit(chat_id, msg_id, "This sends money, deletes, or mails more than 10 people. Tap Confirm to approve", buttons=[[("Confirm", conf)]])
            adapter.answer_callback(cb_id)
            return
        consume_nonce(data)
        _run_approve(adapter, chat_id, msg_id, cb_id, profile, card)

    elif action == "confirm_approve":
        created = nonce_data.get("created_at", 0)
        consume_nonce(data)
        if time.time() - created > 600:
            adapter.edit(chat_id, msg_id, "Confirmation expired")
            adapter.answer_callback(cb_id, "Confirmation expired")
            return
        _run_approve(adapter, chat_id, msg_id, cb_id, profile, card)

    elif action == "send_back":
        res = adapter.send(chat_id, "Reply to this message with your notes", force_reply=True)
        prompt_id = res.get("message_id") if isinstance(res, dict) else res
        st = load_state()
        st["prompts"][str(prompt_id)] = {"nonce": data, "card": card, "profile": profile, "chat_id": chat_id}
        save_state(st)
        adapter.answer_callback(cb_id)


def _handle_message(adapter: Any, msg: dict[str, Any]) -> None:
    sender_id = str(msg.get("from", {}).get("id", ""))
    if adapter.allow and sender_id not in [str(x) for x in adapter.allow]:
        return

    chat_id, text, reply_to = str(msg.get("chat", {}).get("id", "")), (msg.get("text") or "").strip(), msg.get("reply_to_message")
    if text == "/stop":
        outward.set(True, by="chat")
        adapter.send(chat_id, "Kill switch turned on")
        return
    if text.startswith("/stop"):
        adapter.send(chat_id, "Kill switch cannot be turned off from chat; use the console or CLI")
        return

    if reply_to:
        reply_id = str(reply_to.get("message_id", ""))
        st = load_state()
        prompt_data = st.get("prompts", {}).pop(reply_id, None)
        if prompt_data:
            save_state(st)
            _pl(["--profile", prompt_data.get("profile", "default"), "reject", prompt_data.get("card", ""), text[:4000]])
            adapter.send(chat_id, f"Sent back {prompt_data.get('card', '')} with review notes")
            return

    adapter.send(chat_id, "I only take replies to cards")
