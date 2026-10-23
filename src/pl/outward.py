"""Machine-wide kill switch and outward actions.

Holds approved cards with outward actions while the switch is on.
"""
import builtins
import json
import os
import threading
from pathlib import Path

from pl import config as C
from pl import manager
from pl.util import now_iso


def is_outward(c: dict) -> bool:
    """True when a card has an outward action in metadata."""
    if not isinstance(c, dict):
        return False
    outward = (c.get("metadata") or {}).get("outward")
    return bool(isinstance(outward, dict) and outward)


def risky(c: dict) -> bool:
    """True when an outward action involves money, deletion, or more than 10 recipients."""
    if not is_outward(c):
        return False
    out = (c.get("metadata") or {}).get("outward") or {}
    if bool(out.get("amount")):
        return True
    if bool(out.get("deletes")):
        return True
    rec = out.get("recipients")
    if isinstance(rec, (list, tuple, builtins.set)) and len(rec) > 10:
        return True
    if isinstance(rec, (int, float)) and rec > 10:
        return True
    return False


def state() -> dict:
    """Current machine-wide kill-switch state: {on: bool, since: str|None, by: str|None}."""
    path = manager.machine_dir() / "kill-switch.json"
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return {
                "on": bool(data.get("on", False)),
                "since": data.get("since"),
                "by": data.get("by"),
            }
    except (OSError, ValueError):
        pass
    return {"on": False, "since": None, "by": None}


def set(on: bool, by: str | None = None) -> dict:
    """Set machine-wide kill switch on or off. Atomic write with mode 0600."""
    st = {
        "on": bool(on),
        "since": now_iso() if on else None,
        "by": by,
    }
    path = manager.machine_dir() / "kill-switch.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(st))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return st


def held_cards() -> list[dict]:
    """Cards held by the kill switch across profiles."""
    if not state().get("on"):
        return []
    held = []
    try:
        from pl import profiles
        for prof in profiles.list_profiles(check_lock=False):
            inbox_path = Path(prof["dir"]) / "state" / "inbox.json"
            try:
                data = json.loads(inbox_path.read_text())
                for item in data.get("held", []):
                    if isinstance(item, dict):
                        held.append(item)
                    elif isinstance(item, str):
                        held.append({"id": item, "pipeline": prof["name"]})
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    if not held and C.CONFIG_DIR is not None:
        try:
            from pl.board import cards, col_name
            for c in cards():
                if is_outward(c) and col_name(c.get("list_id")) == "Approved":
                    held.append({"id": c["id"], "title": c.get("title", ""), "pipeline": C.PROFILE_NAME})
        except Exception:
            pass
    return held


def cmd_kill_switch(a) -> None:
    """Handle pl kill-switch on|off|status [--json]."""
    action = a.action
    is_json = getattr(a, "json", False)

    if action == "on":
        user = getattr(C, "USER_EMAIL", None) or os.environ.get("USER") or "user"
        st = set(True, by=user)
        try:
            from pl import events
            events.emit("kill_switch", state="on", action="on", by=user)
        except Exception:
            pass
        if is_json:
            print(json.dumps({**st, "held": held_cards()}))
        else:
            print("kill switch on")

    elif action == "off":
        cur = state()
        if not cur.get("on"):
            if is_json:
                print(json.dumps({**cur, "held": []}))
            else:
                print("kill switch is already off")
            return
        user = getattr(C, "USER_EMAIL", None) or os.environ.get("USER") or "user"
        st = set(False, by=user)
        try:
            from pl import events
            events.emit("kill_switch", state="off", action="off", by=user)
        except Exception:
            pass
        if is_json:
            print(json.dumps({**st, "held": []}))
        else:
            print("kill switch off")

    elif action == "status":
        st = state()
        held = held_cards()
        if is_json:
            print(json.dumps({**st, "held": held}))
        else:
            if st.get("on"):
                by_str = f" by {st['by']}" if st.get("by") else ""
                since_str = f" since {st['since']}" if st.get("since") else ""
                print(f"kill switch is on{since_str}{by_str}")
                if held:
                    print(f"held cards ({len(held)}):")
                    for c in held:
                        cid = c.get("id") or c.get("card", "")
                        pipe = c.get("pipeline", "")
                        title = c.get("title", "")
                        print(f"  {pipe + ' ' if pipe else ''}{cid}  {title}".rstrip())
                else:
                    print("no cards held")
            else:
                print("kill switch is off")

