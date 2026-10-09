"""Which agents are alive, and their tmux windows."""
import hashlib
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
from pl.util import mask, parse_iso

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


HOLD = {"split": "split \u2014 see child cards", "parked": "held"}   # card tags: the dispatcher starts no agent
GATE_RE = re.compile(r"GATE: (nothing-runnable|not-approved|before-push|outside-worktree)")   # /run-plan's stop lines
WAIT_IDLE = 600   # a run agent whose pane has not changed for this long is waiting, not working


def hold_reason(c):
    """Why the dispatcher leaves this card alone (tagged split or parked), else None. A card held with pl hold
    shows its reason (card data: plain text only)."""
    tags = c.get("tags") or []
    why = next((v for k, v in HOLD.items() if k in tags), None)
    reason = (c.get("metadata") or {}).get("hold_reason")
    return f"held: {str(reason)[:60]}" if why == "held" and reason else why


def _on_pane(rec, w):
    """True when a registry entry runs in this worker's own pane: its exact <session>:<window>.<pane>."""
    return bool(w.get("pane")) and rec.get("tmux") == f"{C.TMUX_SESSION}:{w.get('window')}.{w['pane']}"


def screen_quiet(st, pane):
    """Seconds this pane's screen text has stayed the same across dispatcher passes (0 when new or changed), kept in
    the dispatcher state. A harness with no session registry (agy) redraws its screen every second, so tmux's
    window_activity never ages: its idle time is read from the screen instead."""
    text = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane]).stdout or ""
    digest, now = hashlib.sha256(text.encode()).hexdigest(), time.time()
    rec = st.setdefault("screens", {}).get(pane) or {}
    if rec.get("hash") != digest or not isinstance(rec.get("since"), (int, float)):
        rec = {"hash": digest, "since": now}
    st["screens"][pane] = {**rec, "seen": now}
    return now - rec["since"]


def run_waiting(w, reg, st=None):
    """'waiting (<why>)' when a live run agent is not working: its session is not busy and its screen shows a
    /run-plan GATE line or its pane has been idle WAIT_IDLE seconds. Such an agent does not hold a run slot.
    With the dispatcher state st, a harness with no session registry (agy) is idle by an unchanged screen."""
    rec = reg.get(w.get("session_id")) or next((r for r in reg.values() if _on_pane(r, w)), {})
    if rec.get("status") == "busy" or not pane_exists(w.get("pane")):
        return None
    gates = GATE_RE.findall(harnesses._run(["tmux", "capture-pane", "-p", "-t", w["pane"], "-S", "-60"]).stdout or "")
    if gates:
        return f"waiting ({gates[-1]})"
    h = _harness(w)
    if st is not None and h is not None and not h.session_registry:
        idle = screen_quiet(st, w["pane"])
    else:
        act = (harnesses._run(["tmux", "display-message", "-p", "-t", w["pane"], "#{window_activity}"]).stdout or "").strip()
        idle = time.time() - int(act) if act.isdigit() else 0
    return f"waiting (idle {int(idle // 60)} min)" if idle >= WAIT_IDLE else None


PR_RE = re.compile(r"(?<![\w&#])#(\d{1,7})\b")   # a PR or issue reference like #1757
# card metadata of a released run agent (dispatch.release_run): why, when it may start again, how many releases, and
# the card's column and description at release (a change clears the hold)
RELEASE_KEYS = ("run_released_at", "run_released_why", "run_retry_at", "run_releases", "run_released_on")


def gate_pr(pane):
    """The first PR reference like "#1757" within 3 lines of the last /run-plan GATE line on the pane's screen, or
    None. Read from the screen only; nothing is looked up."""
    lines = (harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-60"]).stdout or "").splitlines()
    at = next((i for i in range(len(lines) - 1, -1, -1) if GATE_RE.search(lines[i])), None)
    if at is None:
        return None
    m = next((m for ln in lines[max(0, at - 3):at + 4] if (m := PR_RE.search(ln))), None)
    return f"#{m.group(1)}" if m else None


def release_key(c):
    """The card's column and a hash of its description: a released run card is held while this stays the same."""
    d = str(c.get("description") or "").encode()
    return f"{col_name(c['list_id'])}:{hashlib.sha256(d).hexdigest()[:16]}"


def run_blocked(c, now=None):
    """'blocked: <why> · retry HH:MM' while a released run card waits out its backoff (the card unchanged since),
    else None."""
    m = c.get("metadata") or {}
    t = parse_iso(str(m.get("run_retry_at") or ""))
    if not t or (now or time.time()) >= t or m.get("run_released_on") != release_key(c):
        return None
    return f"blocked: {m.get('run_released_why') or 'waiting'} \u00b7 retry {datetime.fromtimestamp(t):%H:%M}"


MAX_ATTEMPTS = 3   # a stage whose agent died this many times waits for a person (pl retry)
ERROR_LINE_RE = re.compile(r"\b[Ee]rror:")


def death_line(pane):
    """The last "Error: ..." line (masked, at most 200 characters) on a dead agent's pane, for example the harness
    refusing its command line; None when the pane is gone or shows none. Read from the screen only."""
    if not pane:
        return None
    r = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"])
    if getattr(r, "returncode", 1):
        return None
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()][-15:]
    hit = next((ln for ln in reversed(lines) if ERROR_LINE_RE.search(ln)), None)
    return mask(hit)[:200] if hit else None


def died_label(c, hint):
    """"<stage> agent died N× · <its last error line, short> · <hint>" for a card whose agent died MAX_ATTEMPTS
    times. The error line is screen text: data, shown as plain text only."""
    m = c.get("metadata") or {}
    w = m.get("worker") or {}
    err = m.get("agent_error") or {}
    line = str(err.get("line") or "") if err.get("stage") == w.get("stage") else ""
    short = line if len(line) <= 60 else line[:59] + "\u2026"
    return f"{w.get('stage')} agent died {int(w.get('attempts') or 0)}\u00d7" + (f" \u00b7 {short}" if short else "") + f" \u00b7 {hint}"


API_ERROR_IDLE = 600   # an agent idle this long at the prompt right after an "API Error:" line is stopped, not working
API_ERROR_RE = re.compile(r"API Error:")


def api_error_wait(w, reg):
    """The "API Error: ..." line (masked, short) when the agent's last screen output is an API error and its pane
    has not changed for API_ERROR_IDLE seconds (for example "API Error: Your computer went to sleep mid-response."):
    the harness stopped its turn and sits at the prompt. None otherwise, or while its session is busy."""
    rec = reg.get(w.get("session_id")) or next((r for r in reg.values() if _on_pane(r, w)), {})
    pane = w.get("pane")
    if rec.get("status") == "busy" or not pane:
        return None
    r = harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"])
    if getattr(r, "returncode", 1):
        return None
    lines = [BOX_RE.sub(" ", ln).strip() for ln in (r.stdout or "").splitlines()]
    hit = next((ln for ln in reversed([ln for ln in lines if ln][-8:]) if API_ERROR_RE.search(ln)), None)
    if hit is None:
        return None
    act = (harnesses._run(["tmux", "display-message", "-p", "-t", pane, "#{window_activity}"]).stdout or "").strip()
    if not act.isdigit() or time.time() - int(act) < API_ERROR_IDLE:
        return None
    return mask(hit[hit.index("API Error:"):])[:120]


PERMISSION_IDLE = 120   # an agent at a permission prompt this long, with no change on screen, is stuck there
BOX_RE = re.compile(r"[│┃|╭╮╰╯─━]+")
OPTION_RE = re.compile(r"^\s*(?:[❯›>]\s*)?([1-9])[.)]\s+(.*?)\s*$")   # a numbered option; groups: digit, label


def permission_wait(h, pane):
    """The tool line of a permission prompt on the pane's screen, masked and short, once the pane has been idle
    PERMISSION_IDLE seconds; else None. pl only reads the screen: it never answers the prompt."""
    pats = harnesses.permission_patterns(h)
    if not pats or not pane:
        return None
    lines = [BOX_RE.sub(" ", ln).strip() for ln in
             (harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"]).stdout or "").splitlines()]
    lines = [ln for ln in lines if ln][-20:]   # the prompt sits at the bottom of the screen
    at = next((i for i, ln in enumerate(lines) if any(re.search(p, ln, re.I) for p in pats)), None)
    if at is None:
        return None
    act = (harnesses._run(["tmux", "display-message", "-p", "-t", pane, "#{window_activity}"]).stdout or "").strip()
    if not act.isdigit() or time.time() - int(act) < PERMISSION_IDLE:
        return None
    near = [ln for ln in lines[max(0, at - 3):at] + lines[at + 1:at + 3]
            if not OPTION_RE.match(ln) and not any(re.search(p, ln, re.I) for p in pats)]
    return mask(" · ".join(near) or lines[at])[:120]


def trust_wait(h, pane):
    """The folder a harness asks the person to trust on this pane's screen ("this folder" when the screen does not
    name it), else None. A first run in a new folder stops here, so the session never registers. pl only reads the
    screen: it never answers the prompt."""
    pats = harnesses.trust_patterns(h) if h else []
    if not pats or not pane or not pane_exists(pane):
        return None
    lines = [BOX_RE.sub(" ", ln).strip() for ln in
             (harnesses._run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"]).stdout or "").splitlines()]
    if not any(re.search(p, ln, re.I) for ln in lines for p in pats):
        return None
    for i, ln in enumerate(lines):
        if re.search(r"Accessing workspace", ln, re.I):
            path = next((x for x in [ln.split(":", 1)[1].strip() if ":" in ln else "", *lines[i + 1:i + 4]]
                         if x.startswith(("/", "~"))), "")
            return mask(path)[:120] or "this folder"
    return "this folder"


def worker_view(c, reg):
    """One short phrase describing the card's agent, for pl list."""
    m = c.get("metadata") or {}
    w = m.get("worker") or {}
    if hold_reason(c):
        return hold_reason(c)
    if m.get("pipeline_mode") != "auto":
        return "manual"
    if not w and (blocked := run_blocked(c)):
        return blocked
    if col_name(c["list_id"]) == "Plan for review":
        return "ready for your review"
    if col_name(c["list_id"]) == "Manual":
        return "manual: yours to do"
    if not w:
        return "waiting"
    if int(w.get("attempts") or 0) >= MAX_ATTEMPTS and worker_status(w, reg)[0] == "dead":
        return died_label(c, "pl retry")
    if (hit := w.get("limit_hit")) and isinstance(hit, dict):   # set by the dispatcher while the agent sits on a limit screen
        try:
            t = datetime.fromisoformat(str(hit.get("until"))).astimezone()
            when = f"{t:%b} {t.day} {t:%H:%M}"
        except ValueError:
            when = "?"
        return f"limit hit — {hit.get('account')}, resets {when}"
    if w.get("trust_wait"):   # set by the dispatcher while the agent sits at the folder trust prompt
        return f"{w['stage']} agent waiting: trust the folder ({w.get('profile')})"
    if w.get("permission_wait"):   # set by the dispatcher while the agent sits at a permission prompt
        return f"{w['stage']} agent waiting for permission ({w.get('profile')})"
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
            if _on_pane(rec, w):
                return "alive", s
        since = max(parse_iso(w.get("started_at") or ""), parse_iso(w.get("moved_at") or ""))   # a move relaunches it
        if time.time() - since < 180:
            return "starting", sid
        if harnesses.pane_command(pane) not in ("", *harnesses.SHELLS) and trust_wait(h, pane):
            return "alive", sid   # held at the folder trust prompt: waiting for a person, not dead; no second window
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
