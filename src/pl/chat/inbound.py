from dataclasses import dataclass, field
import json
import os
import subprocess
import sys
import time
from typing import Any


@dataclass
class InboundEvent:
    kind: str
    user_id: str
    action_id: str = ""
    value: str = ""
    notes: str = ""
    chat_id: str = ""
    message_id: str = ""
    trigger_id: str = ""
    adapter: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _pl(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "PL_SOURCE": "chat", **(env or {})})


def audit_record(kind: str, **fields: Any) -> None:
    try:
        from pl import audit
        audit.record(kind, **fields)
    except (ImportError, AttributeError):
        from pl import manager
        p = manager.machine_dir() / "audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"kind": kind, "ts": time.time(), **fields}) + "\n")


def parse_action_target(target: str) -> tuple[str, str]:
    parts = target.split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("default", parts[1] if len(parts) == 2 else target)


def dispatch_action(adapter_inst: Any, event: InboundEvent) -> Any:
    val = event.action_id or event.value
    if event.kind == "action" and (val.startswith("approve:") or val.startswith("app:")):
        profile, card = parse_action_target(val)
        res = _pl([sys.executable, "-m", "pl", "--profile", profile, "approve", card])
        if res.returncode == 0 and event.chat_id and event.message_id and hasattr(adapter_inst, "edit"):
            adapter_inst.edit(event.chat_id, event.message_id, f"Approved by you at {time.strftime('%H:%M UTC', time.gmtime())}")
        return res
    if event.kind == "modal_submission":
        profile, card = parse_action_target(event.value or event.action_id)
        return _pl([sys.executable, "-m", "pl", "--profile", profile, "reject", card, event.notes])
    return None
