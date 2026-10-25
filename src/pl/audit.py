import json
import os
import stat
from datetime import datetime, timezone
from typing import Any

from pl import manager


def record(kind: str, *args: Any, **fields: Any) -> None:
    """Append one audit entry to machine_dir()/audit.jsonl, opened 0600 with O_APPEND."""
    data = dict(fields)
    if args:
        if len(args) == 1:
            data.setdefault("target", args[0])
        elif len(args) == 2:
            data.setdefault("connector", args[0])
            data.setdefault("sender_id", args[1])
        else:
            data["args"] = list(args)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
    path = manager.machine_dir() / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
