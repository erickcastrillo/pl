import datetime
import json
import os
import subprocess
import sys
from typing import Any

from pl import audit, manager


class PlResult(tuple):
    @property
    def returncode(self) -> int:
        return self[0]

    @property
    def output(self) -> str:
        return self[1]


def _state():
    return manager.machine_dir() / "chat-state.json"


def load_state() -> dict[str, Any]:
    try:
        d = json.loads(_state().read_text())
        if isinstance(d, dict):
            return {**{"offset": None, "nonces": {}, "prompts": {}, "posted_gates": []}, **d}
    except (OSError, ValueError):
        pass
    return {"offset": None, "nonces": {}, "prompts": {}, "posted_gates": []}


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
    st.setdefault("nonces", {})[nonce] = data
    save_state(st)


def get_nonce(nonce: str) -> dict[str, Any] | None:
    return load_state().get("nonces", {}).get(nonce)


def consume_nonce(nonce: str) -> dict[str, Any] | None:
    st = load_state()
    val = st.setdefault("nonces", {}).pop(nonce, None)
    if val is not None:
        save_state(st)
    return val


def _pl(argv: list[str], env: dict[str, str] | None = None) -> PlResult:
    full_env = {**os.environ, "PL_SOURCE": "chat", **(env or {})}
    proc = subprocess.run(argv, capture_output=True, text=True, env=full_env)
    return PlResult((proc.returncode, (proc.stdout + proc.stderr).strip()))


def _run_approve(adapter: Any, chat_id: str, msg_id: Any, cb_id: str, profile: str, card: str) -> None:
    code, out = _pl([sys.executable, "-m", "pl", "--profile", profile, "approve", card], env={"PL_SOURCE": "chat"})
    time_str = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
    status = f"Approved by you at {time_str}" if code == 0 else f"Approval failed: {out.splitlines()[0] if out else 'error'}"
    if msg_id and hasattr(adapter, "edit"):
        adapter.edit(chat_id, msg_id, status)
    if hasattr(adapter, "answer_callback"):
        adapter.answer_callback(cb_id)
    audit.record("chat_approve", connector=getattr(adapter, "name", "telegram"), card=card, profile=profile, returncode=code)


def _sender_and_chat(update: dict[str, Any]) -> tuple[str, str]:
    if "callback_query" in update:
        cb = update["callback_query"]
        s = str(cb.get("from", {}).get("id", "")) if cb.get("from") else ""
        c = str(cb.get("message", {}).get("chat", {}).get("id", "")) if cb.get("message") else ""
        return s, c
    if "message" in update:
        m = update["message"]
        s = str(m.get("from", {}).get("id", "")) if m.get("from") else ""
        c = str(m.get("chat", {}).get("id", "")) if m.get("chat") else ""
        return s, c
    return "", ""


def handle_update(adapter: Any, update: dict[str, Any]) -> None:
    sender_id, chat_id = _sender_and_chat(update)
    allow = [str(x) for x in getattr(adapter, "allow", [])]
    if allow and (sender_id not in allow and chat_id not in allow):
        audit.record("chat_unknown_sender", connector=getattr(adapter, "name", "telegram"), sender_id=sender_id or chat_id)
        return

    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb.get("id", "")
        msg = cb.get("message") or {}
        c_id = str(msg.get("chat", {}).get("id", "")) if msg.get("chat") else ""
        data = cb.get("data", "")
        nonce_data = get_nonce(data)
        if not nonce_data:
            return
        if nonce_data.get("action") == "approve":
            consume_nonce(data)
            _run_approve(adapter, c_id, msg.get("message_id"), cb_id, nonce_data.get("profile", "default"), nonce_data.get("card", ""))


def _load_adapters() -> list[Any]:
    try:
        from pl import chat
        return [chat.adapter(name) for name in chat.load()]
    except Exception:
        return []


def poll(adapter: Any = None) -> None:
    adapters = [adapter] if adapter is not None else _load_adapters()
    for ad in adapters:
        st = load_state()
        offset = st.get("offset")
        try:
            updates = ad.updates(offset=offset)
        except TypeError:
            updates = ad.updates(offset)
        except Exception:
            continue
        for u in updates:
            up_id = u.get("update_id")
            if up_id is not None:
                st["offset"] = up_id + 1
                save_state(st)
            handle_update(ad, u)
