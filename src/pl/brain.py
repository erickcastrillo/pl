"""Machine-wide brain conversation store and retention."""
import json
import os
import time
import uuid
from pathlib import Path

from pl.manager import machine_dir
from pl.util import mask, now_iso, parse_iso

DEFAULT_KEEP_DAYS = 180


def _conv_dir() -> Path:
    d = machine_dir() / "brain" / "conversations"
    d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d.parent, 0o700)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def conv_new(conv: str | None = None) -> str:
    """Create a new conversation, returning its id. The file is created with 0600 permissions."""
    cid = conv or uuid.uuid4().hex[:12]
    d = _conv_dir()
    p = d / f"{cid}.jsonl"
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.chmod(p, 0o600)
    return cid


def conv_append(conv: str, role: str, text: str) -> dict:
    """Append one turn to the conversation. Each turn's text is masked before writing."""
    d = _conv_dir()
    p = d / f"{conv}.jsonl"
    turn = {"ts": now_iso(), "role": role, "text": mask(text)}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(p, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(turn) + "\n")
    except Exception:
        os.close(fd)
        raise
    return turn


def conv_show(conv: str) -> list[dict]:
    """Read the conversation's turns, oldest first; [] when not found."""
    p = _conv_dir() / f"{conv}.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def prune(now=None, dry_run: bool = False) -> list[Path]:
    """Delete conversations older than [brain] keep_days (default 180). Return deleted paths."""
    if now is None:
        now = time.time()
    from pl.manager import _machine_toml
    try:
        keep_days = int(_machine_toml().get("brain", {}).get("keep_days", DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError):
        keep_days = DEFAULT_KEEP_DAYS

    cutoff = now - (keep_days * 86400)
    d = _conv_dir()
    if not d.exists():
        return []

    deleted = []
    for p in sorted(d.glob("*.jsonl")):
        try:
            mtime = p.stat().st_mtime
            t = mtime
            lines = p.read_text().splitlines()
            if lines:
                last_turn = json.loads(lines[-1])
                if "ts" in last_turn:
                    parsed = parse_iso(last_turn["ts"])
                    if parsed:
                        t = min(mtime, parsed)
            if t < cutoff:
                deleted.append(p)
                if not dry_run:
                    p.unlink()
        except OSError:
            continue
    return deleted


def cmd_brain(a):
    """pl brain show <conv> [--json] | pl brain prune [--dry-run]."""
    cmd = getattr(a, "brain_cmd", None)
    if cmd == "show":
        turns = conv_show(a.conv)
        if getattr(a, "json", False):
            print(json.dumps(turns, indent=2))
            return 0
        if not turns:
            print(f"pl: no conversation {a.conv}")
            return 1
        for t in turns:
            print(f"{t.get('role', '')}: {t.get('text', '')}")
        return 0
    if cmd == "prune":
        res = prune(dry_run=getattr(a, "dry_run", False))
        verb = "would delete" if getattr(a, "dry_run", False) else "deleted"
        print(f"{verb} {len(res)} conversation{'s' if len(res) != 1 else ''}")
        return 0
    return 0
