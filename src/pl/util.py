"""Small helpers shared by every module."""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from pl import config as C

TOKEN_RE = re.compile(r"(?i:bearer\s+)[A-Za-z0-9._~+/=-]{8,}|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                      r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def age(ts):
    d = int(time.time() - ts) if ts else 0
    return f"{d // 60}m" if d < 3600 else (f"{d // 3600}h" if d < 86400 else f"{d // 86400}d")


def card_url(item_id):
    from pl import trackers
    return trackers.get("tracker").url(item_id)


def slug_of(c):
    m = c.get("metadata") or {}
    return m.get("spec_slug") or re.sub(r"[^a-z0-9]+", "-", (c.get("title") or "").lower()).strip("-")[:60]


def notify(title, message):
    if C.ATTENTION and C.ATTENTION.exists():
        subprocess.run([str(C.ATTENTION), "notify", title, message], capture_output=True)


def load_state():
    try:
        return json.loads(C.STATE_FILE.read_text())
    except Exception:
        return {"notified": {}}


def save_state(st):
    C.ATTN.mkdir(exist_ok=True)
    C.STATE_FILE.write_text(json.dumps(st))


def tmux(*args, check=True):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"pl: tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else None


def mask(text):
    """pl's secret masking (every resolved ${VAR} value), plus common token shapes a transcript may hold."""
    from pl.trackers import mcp   # imported here: trackers import this module
    text = mcp._mask(str(text), sorted(mcp._secrets, key=len, reverse=True))
    return TOKEN_RE.sub("***", PEM_RE.sub("***", text))


def _nofollow(p, flags):
    return os.open(p, flags | os.O_NOFOLLOW)
