"""Small helpers shared by every module."""
import json
import re
import subprocess
import time
from datetime import datetime, timezone

from pl import config as C


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
