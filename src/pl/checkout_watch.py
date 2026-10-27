"""Report stray changes in the main checkout (work_dir). Read-only: pl never cleans, stashes or moves them."""
import os
import subprocess

from pl import alerts, events
from pl import config as C
from pl.agents import worker_status
from pl.util import short_id

KEY = "main_checkout_dirty"


def _git(argv):
    try:
        r = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except OSError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return r.returncode, r.stdout


def dirty_paths():
    """Changed paths outside .pl/, or None when the checkout cannot be read."""
    if not C.WORK_DIR or not C.WORK_DIR.is_dir():
        return None
    rc, out = _git(["-C", str(C.WORK_DIR), "--no-optional-locks", "status", "--porcelain", "-z"])
    if rc != 0:
        return None
    paths, fields = set(), iter(out.split("\0"))
    for entry in fields:
        if len(entry) < 4:
            continue
        if "R" in entry[:2] or "C" in entry[:2]:
            next(fields, None)   # the old name
        path = entry[3:]
        if path != ".pl" and not path.startswith(".pl/"):
            paths.add(path)
    return paths


def live_runs(all_cards, reg):
    return [c for c in all_cards
            if (w := (c.get("metadata") or {}).get("worker") or {}).get("stage") == "run"
            and worker_status(w, reg)[0] in ("alive", "starting")]


def check(all_cards, reg, st, dry, send):
    paths = dirty_paths()
    if paths is None:
        return
    if not paths:
        if not dry:
            alerts.resolve(KEY)
            st["main_dirty"] = []
        return
    new = sorted(paths - set(st.get("main_dirty") or []))
    runs = live_runs(all_cards, reg)
    who = ", ".join(f"{short_id(c['id'])} {c['title'][:50]}" for c in runs) or "no live agent is known"
    listed = sorted(paths)
    title = (f"Main checkout has {len(paths)} changed path(s): {', '.join(listed[:5])}{' …' if len(listed) > 5 else ''}; "
             f"live run agents: {who}")
    fix = f"pl never cleans it: git -C {C.WORK_DIR} status shows the changes"
    if dry:
        if new:
            print(f"would alert: {title}")
        return
    ids = [c["id"] for c in runs]
    why = alerts.open(KEY, "high", title, fix, cards=ids)
    if new:
        events.emit(KEY, paths=new, agents=ids)
        send(f"Main checkout changed: {', '.join(new[:5])}", f"live run agents: {who}")
    elif why in ("1 h", "4 h"):
        send(alerts.headline(why, title), fix)
    st["main_dirty"] = listed
