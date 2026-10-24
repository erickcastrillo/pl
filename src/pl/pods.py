"""Pods: the pod.md of a pipeline, kept as numbered versions with rule add and rollback."""
import hashlib
import json
import os
import sys
from pathlib import Path

from pl import config as C
from pl.util import now_iso

RULES_HEADING = "## Rules"


def pod_file(pod: str) -> Path:
    cfg = (getattr(C, "PODS", {}) or {}).get(pod) or {}
    if cfg.get("file"):
        p = Path(cfg["file"]).expanduser()
        return p if p.is_absolute() else C.CONFIG_DIR / p
    return C.CONFIG_DIR / "pods" / pod / "pod.md"


def _history_dir(pod: str) -> Path:
    return C.CONFIG_DIR / "pods" / ".history" / pod


def _write_private(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def history(pod: str) -> list[dict]:
    try:
        return json.loads((_history_dir(pod) / "versions.json").read_text())
    except (OSError, ValueError):
        return []


def _record(pod: str, data: bytes, note: str, author: str) -> int:
    versions = history(pod)
    n = len(versions) + 1
    d = _history_dir(pod)
    _write_private(d / f"{n}.md", data)
    versions.append({"n": n, "ts": now_iso(), "note": note, "author": author,
                     "sha256": hashlib.sha256(data).hexdigest()})
    _write_private(d / "versions.json", json.dumps(versions, indent=1).encode())
    return n


def save_version(pod: str, text: str | bytes, note: str, author: str) -> int:
    """Write pod.md as a new version; a copy that was changed outside pl is kept as a version first."""
    data = text.encode() if isinstance(text, str) else text
    p = pod_file(pod)
    versions = history(pod)
    if p.is_file():
        current = p.read_bytes()
        if not versions:
            _record(pod, current, "existing pod.md", author)
        elif hashlib.sha256(current).hexdigest() != versions[-1]["sha256"]:
            _record(pod, current, "edited outside pl", author)
    n = _record(pod, data, note, author)
    _write_private(p, data)
    return n


def add_rule(text: str, rule: str) -> str:
    text = text if text.endswith("\n") or not text else text + "\n"
    if RULES_HEADING not in text.splitlines():
        text += ("\n" if text else "") + RULES_HEADING + "\n"
    lines = text.splitlines()
    i = lines.index(RULES_HEADING) + 1
    while i < len(lines) and not lines[i].startswith("## "):
        i += 1
    while i > 0 and not lines[i - 1].strip():
        i -= 1
    lines.insert(i, f"- {rule}")
    return "\n".join(lines) + "\n"


def _confirm(question: str, yes: bool):
    if yes:
        return
    if not sys.stdin.isatty():
        print("pl pods: no terminal to confirm on; add --yes", file=sys.stderr)
        raise SystemExit(2)
    try:
        ok = input(f"{question} [y/N]: ")
    except EOFError:
        ok = ""
    if ok.strip().lower() not in ("y", "yes"):
        print("nothing changed")
        raise SystemExit(0)


def _author() -> str:
    return os.environ.get("USER") or "unknown"


def _cmd_rule(a) -> int:
    _confirm(f"Add this rule to {a.pod}: {a.text}?", a.yes)
    p = pod_file(a.pod)
    current = p.read_text() if p.is_file() else ""
    n = save_version(a.pod, add_rule(current, a.text), a.note or f"rule: {a.text}", _author())
    print(f"added the rule to {a.pod} (version {n})")
    return 0


def _cmd_rollback(a) -> int:
    d = _history_dir(a.pod)
    if a.to not in {v["n"] for v in history(a.pod)}:
        print(f"pl pods: {a.pod} has no version {a.to}", file=sys.stderr)
        raise SystemExit(2)
    _confirm(f"Roll {a.pod} back to version {a.to}?", a.yes)
    n = save_version(a.pod, (d / f"{a.to}.md").read_bytes(), f"rollback to {a.to}", _author())
    print(f"{a.pod} is back to version {a.to}, saved as version {n}")
    return 0


def _cmd_history(a) -> int:
    versions = history(a.pod)
    if a.json:
        print(json.dumps(versions, indent=1))
    for v in ([] if a.json else versions):
        print(f"{v['n']:>3}  {v['ts']}  {v['author']}  {v['note']}")
    return 0


def cmd_pods(a) -> int:
    """pl pods rule add POD "TEXT" [--yes] [--note N] | rollback POD --to N [--yes] | history POD [--json]."""
    return {"rule": _cmd_rule, "rollback": _cmd_rollback, "history": _cmd_history}[a.pods_cmd](a)
