import json
import os
from typing import Any

from pl import manager


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
    st["nonces"][nonce] = data
    save_state(st)


def get_nonce(nonce: str) -> dict[str, Any] | None:
    return load_state().get("nonces", {}).get(nonce)
