"""Which agents are alive, and their tmux windows."""
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime

from pl import config as C
from pl import harnesses
from pl.board import col_name
from pl.util import parse_iso

# ---------- claude session registry (which agents are alive) ----------

def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def registry():
    out = {}
    for acct, d in C.PROFILES.items():
        if not harnesses.account_harness(acct).session_registry:
            continue   # pl reads no other file inside an account's config dir
        for f in (d / "sessions").glob("*.json"):
            try:
                j = json.loads(f.read_text())
            except Exception:
                continue
            if j.get("sessionId") and j.get("pid") and alive(j["pid"]):
                out[j["sessionId"]] = j
    return out


def pane_exists(pane):
    return bool(pane) and subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#S"],
                                         capture_output=True).returncode == 0



def _harness(w):
    try:
        return harnesses.get(w.get("harness") or "claude")
    except SystemExit:
        return None


HOLD = {"split": "split \u2014 see child cards", "parked": "parked"}   # card tags: the dispatcher starts no agent
GATE_RE = re.compile(r"GATE: (nothing-runnable|not-approved|before-push|outside-worktree)")   # /run-plan's stop lines
WAIT_IDLE = 600   # a run agent whose pane has not changed for this long is waiting, not working


def hold_reason(c):
    """Why the dispatcher leaves this card alone (tagged split or parked), else None."""
    tags = c.get("tags") or []
    return next((v for k, v in HOLD.items() if k in tags), None)


def run_waiting(w, reg):
    """'waiting (<why>)' when a live run agent is not working: its session is not busy and its screen shows a
    /run-plan GATE line or its pane has been idle WAIT_IDLE seconds. Such an agent does not hold a run slot."""
    rec = reg.get(w.get("session_id")) or next((r for r in reg.values() if w.get("pane") and (r.get("tmux") or "").endswith(w["pane"])), {})
    if rec.get("status") == "busy" or not pane_exists(w.get("pane")):
        return None
    gates = GATE_RE.findall(harnesses._run(["tmux", "capture-pane", "-p", "-t", w["pane"], "-S", "-60"]).stdout or "")
    if gates:
        return f"waiting ({gates[-1]})"
    act = (harnesses._run(["tmux", "display-message", "-p", "-t", w["pane"], "#{window_activity}"]).stdout or "").strip()
    idle = time.time() - int(act) if act.isdigit() else 0
    return f"waiting (idle {int(idle // 60)} min)" if idle >= WAIT_IDLE else None


def worker_view(c, reg):
    """One short phrase describing the card's agent, for pl list."""
    m = c.get("metadata") or {}
    w = m.get("worker") or {}
    if hold_reason(c):
        return hold_reason(c)
    if m.get("pipeline_mode") != "auto":
        return "manual"
    if col_name(c["list_id"]) == "Plan for review":
        return "ready for your review"
    if col_name(c["list_id"]) == "Manual":
        return "manual: yours to do"
    if not w:
        return "waiting"
    if (hit := w.get("limit_hit")) and isinstance(hit, dict):   # set by the dispatcher while the agent sits on a limit screen
        try:
            t = datetime.fromisoformat(str(hit.get("until"))).astimezone()
            when = f"{t:%b} {t.day} {t:%H:%M}"
        except ValueError:
            when = "?"
        return f"limit hit — {hit.get('account')}, resets {when}"
    sid = w.get("session_id")
    rec = reg.get(sid)
    h = _harness(w)
    if h is None:
        return f"{w['stage']} agent: unknown harness {w.get('harness')!r}"
    if w.get("stage") == "run" and worker_status(w, reg)[0] == "alive" and (why := run_waiting(w, reg)):
        return f"run agent {why} ({w.get('profile')})"
    if not h.session_registry:
        return f"{w['stage']} agent {worker_status(w, reg)[0]} ({w.get('profile')})"
    if rec:
        return f"{w['stage']} agent {rec.get('status', '?')} ({w.get('profile')})"
    if pane_exists(w.get("pane")):
        return f"{w['stage']} agent starting"
    return f"{w['stage']} agent gone"


def worker_status(w, reg):
    """alive | starting | dead, plus the session id if it had to be discovered by tmux pane."""
    sid = w.get("session_id")
    pane = w.get("pane")
    h = _harness(w)
    if h is None:
        return "dead", sid   # card metadata is tracker data: an unknown harness is not alive, never a crash
    if not h.session_registry:
        # no session registry: alive while something other than a shell holds the pane
        if pane_exists(pane):
            if harnesses.pane_command(pane) not in ("", *harnesses.SHELLS):
                return "alive", sid
            if time.time() - parse_iso(w.get("started_at") or "") < 180:
                return "starting", sid
        return "dead", sid
    if sid and sid in reg:
        return "alive", sid
    if pane_exists(pane):
        for s, rec in reg.items():
            if (rec.get("tmux") or "").endswith(pane):
                return "alive", s
        if time.time() - parse_iso(w.get("started_at") or "") < 180:
            return "starting", sid
    return "dead", sid


def pane_tail(pane, n):
    """The last n non-empty lines of a tmux pane (an agent's screen), or of the dispatcher when pane is empty."""
    target = pane if pane and pane_exists(pane) else f"{C.TMUX_SESSION}:dispatch"
    out = subprocess.run(["tmux", "capture-pane", "-p", "-t", target, "-S", "-60"], capture_output=True, text=True).stdout
    lines = [l.rstrip() for l in out.splitlines() if l.strip()]
    if target != pane:
        lines = [l for l in lines if not l.strip().startswith(("Traceback", "File ", "KeyboardInterrupt"))]
    return lines[-n:]


NEW_WINDOW_SCRIPT = """on run argv
  tell application "iTerm2"
    set w to (create window with default profile command (item 1 of argv))
    activate
  end tell
end run"""



def jump_to_window(win_id):
    """Show the agent's tmux window: switch this tmux client to it, or open a new iTerm window attached to it."""
    if not win_id:
        return "this card has no agent window"
    if os.environ.get("TMUX"):
        r = subprocess.run(["tmux", "switch-client", "-t", f"{C.TMUX_SESSION}:{win_id}"], capture_output=True, text=True)
        return r.stderr.strip() or f"switched to {win_id}; come back with: tmux switch-client -l"
    q = shlex.quote
    inner = f"tmux attach -t {q(C.TMUX_SESSION)} \\; select-window -t {q(win_id)}"
    cmd = f"/usr/bin/env DISABLE_AUTO_UPDATE=true /bin/zsh -lic {q(inner)}"
    r = subprocess.run(["osascript", "-", cmd], input=NEW_WINDOW_SCRIPT, capture_output=True, text=True)
    return r.stderr.strip()[:120] or "opened the agent window in a new iTerm window"
