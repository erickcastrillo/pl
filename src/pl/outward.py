"""Machine-wide kill switch and outward actions."""
import builtins
import json
import os
import threading
from pl import manager
from pl.util import now_iso


def is_outward(c: dict) -> bool:
    return bool(isinstance(c, dict) and isinstance((c.get("metadata") or {}).get("outward"), dict) and (c.get("metadata") or {}).get("outward"))


def risky(c: dict) -> bool:
    if not is_outward(c):
        return False
    out = (c.get("metadata") or {}).get("outward") or {}
    rec = out.get("recipients")
    rec_over = len(rec) > 10 if isinstance(rec, (list, tuple, builtins.set)) else (rec > 10 if isinstance(rec, (int, float)) else False)
    return bool(out.get("amount") or out.get("deletes") or rec_over)


def state() -> dict:
    try:
        data = json.loads((manager.machine_dir() / "kill-switch.json").read_text())
        if isinstance(data, dict):
            return {"on": bool(data.get("on", False)), "since": data.get("since"), "by": data.get("by")}
    except (OSError, ValueError):
        pass
    return {"on": False, "since": None, "by": None}


def set(on: bool, by: str | None = None) -> dict:
    st = {"on": bool(on), "since": now_iso() if on else None, "by": by}
    path = manager.machine_dir() / "kill-switch.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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
    return st
