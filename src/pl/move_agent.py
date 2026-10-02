"""Move a live Claude agent to another account in place: Ctrl-C it in its own tmux pane until the pane shows the
shell, copy its transcript into the other account's projects folder, and relaunch `claude --resume <session>` there.

Safety: only Ctrl-C typed into the agent's own pane stops it, after the pane is checked to be that agent's window
in this profile's session; no process is ever signalled. There is no relaunch until the pane shows the shell and no
live registry entry holds the session, so two copies of one thread never run. One lock file per card keeps two moves
(and the dispatcher) off the same card. Only the session's own transcript files are copied (0600, written to a new
file and renamed into place); credential files and links are never opened."""
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from pl import config as C
from pl import alerts, events, harnesses
from pl.accounts import exhausted_profiles, healthy_profile
from pl.agents import alive, registry, worker_status
from pl.board import card, cards, find_card, fresh_next, update
from pl.usage import SID_RE
from pl.util import notify, now_iso, parse_iso, short_id, slug_of

STOP_TRIES = 4     # Ctrl-C presses at most (Claude exits on the second one)
STOP_WAIT = 10     # seconds to wait for the shell and for the session to leave the registry
LOCK_STALE = 600   # a move lock older than this, or held by a dead pid, is removed
LOCK_YOUNG = 5     # an unreadable lock younger than this is still being written: it counts as held
TAIL = 1 << 20     # bytes of the transcript's end read for a limit after a resume
CONTINUE_PROMPT = "Continue where you left off; your previous account hit its usage limit."
PANE_RE = re.compile(r"%\d+")

_sleep = time.sleep


def _run(argv):
    """The one subprocess seam of this module; tests fake it."""
    return subprocess.run(argv, capture_output=True, text=True)


def _lock_path(cid):
    return C.STATE_DIR / "moving" / (re.sub(r"[^\w-]", "_", str(cid))[:120] + ".lock")


def _state(p):
    """held | stale | missing, for one lock file."""
    try:
        age = time.time() - os.lstat(p).st_mtime
    except FileNotFoundError:
        return "missing"
    try:
        pid, at = p.read_text().split()[:2]
        pid, at = int(pid), float(at)
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "held" if age < LOCK_YOUNG else "stale"
    return "held" if alive(pid) and time.time() - at <= LOCK_STALE else "stale"


def _unique(p, tag):
    return p.with_name(f".{p.name}.{os.getpid()}.{time.monotonic_ns()}.{tag}")


def _break(p):
    """Remove a stale lock: rename it to a unique name first, so of two breakers only one gets it. A fresh lock that
    replaced it in between is put back."""
    grab = _unique(p, "broken")
    try:
        os.rename(p, grab)
    except FileNotFoundError:
        return
    if _state(grab) == "held":
        try:
            os.link(grab, p)
        except FileExistsError:
            pass
    grab.unlink(missing_ok=True)


def locked(cid):
    """True while a live pl process holds this card's move lock."""
    p = _lock_path(cid)
    st = _state(p)
    if st == "stale":
        _break(p)
    return st == "held"


def lock(cid):
    """Take this card's move lock; False when a live holder has it. The pid and time go into a private file first,
    which is then linked to the lock path: the link fails when the lock exists, and a lock is never seen half written."""
    p = _lock_path(cid)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.parent.chmod(0o700)
    tmp = _unique(p, "tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {time.time()}\n")
        for _ in range(2):
            try:
                os.link(tmp, p)
                return True
            except FileExistsError:
                if _state(p) == "held":
                    return False
                _break(p)   # stale or just gone: try once more
        return False
    finally:
        tmp.unlink(missing_ok=True)


def unlock(cid):
    try:
        _lock_path(cid).unlink()
    except FileNotFoundError:
        pass


def _root(name):
    return Path(C.PROFILES[name]).expanduser()


def _same(a, b):
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return False


def find_transcript(account, sid):
    """The session's transcript <account>/projects/<project>/<sid>.jsonl (a regular file, no credential), or None."""
    if account not in C.PROFILES or not isinstance(sid, str) or not SID_RE.fullmatch(sid):
        return None
    root = _root(account)
    try:
        projects = (root / "projects").resolve()
        for p in projects.glob(f"*/{sid}.jsonl"):
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode) and p.resolve().parent.parent == projects and not harnesses.is_secret_file(root, st):
                return p
    except (OSError, RuntimeError):
        pass
    return None


def refusal(w, target):
    """Why this worker cannot move to target in place, or None."""
    src, sid = w.get("profile"), w.get("session_id")
    if (w.get("harness") or "claude") != "claude" or (target in C.PROFILES and harnesses.account_harness(target).name != "claude"):
        return "only a Claude agent moves in place (Codex is not supported yet)"
    if target not in C.PROFILES:
        return f"{target!r} is not an account"
    if src not in C.PROFILES:
        return f"the agent's account {src!r} is not an account"
    if not harnesses.get("claude").resume:
        return "the claude harness has no resume command"
    if _same(_root(src), _root(target)):
        return "the target is the same account folder"
    if not w.get("pane") or not sid:
        return "the agent has no pane or session id"
    if find_transcript(src, sid) is None:
        return f"its transcript cannot be found under {src}"
    tp = _root(target) / "projects"
    if tp.is_symlink() and not _same(tp, _root(src) / "projects"):
        return f"{target}'s projects folder is a link"
    return None


def _window_name(c, w):
    return f"{w.get('stage')}-{slug_of(c)[:28]}"   # the name pl gave the agent's window (dispatch.start_worker)


def pane_refusal(c, w):
    """Why the worker's pane is not safe to type into, or None: it must be a pane id inside the worker's own window
    of this profile's tmux session, and that window must still carry the name pl gave it (a tmux restart reuses ids)."""
    pane = w.get("pane")
    if not isinstance(pane, str) or not PANE_RE.fullmatch(pane):
        return f"its pane {pane!r} is not a tmux pane id"
    r = _run(["tmux", "display-message", "-p", "-t", pane, "#{session_name} #{window_id} #{window_name}"])
    if r.returncode or r.stdout.strip() != f"{C.TMUX_SESSION} {w.get('window')} {_window_name(c, w)}":
        return f"its pane {pane} is not in window {w.get('window')} ({_window_name(c, w)}) of tmux session {C.TMUX_SESSION}"
    return None


def _pane_cmd(pane):
    r = _run(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"])
    return r.stdout.strip() if r.returncode == 0 else ""


def stop(pane, sid):
    """Ctrl-C the harness in its own pane until the pane shows the shell; True once the shell is back and no live
    registry entry holds the session. Nothing but that pane ever gets a key, and no process is signalled."""
    for _ in range(STOP_TRIES):
        cmd = _pane_cmd(pane)
        if cmd in harnesses.SHELLS:
            break
        if not cmd:
            return False   # the pane cannot be read: nothing is known to be stopped
        _run(["tmux", "send-keys", "-t", pane, "C-c"])
        _sleep(0.5)
    for _ in range(STOP_WAIT):
        if _pane_cmd(pane) in harnesses.SHELLS and sid not in registry():
            return True
        _sleep(1)
    return False


def _copy_file(src, dst, src_root):
    """Copy one regular, non-credential file to dst, created 0600 before any byte is written. False when skipped."""
    if harnesses.SECRET_RE.fullmatch(src.name):
        return False
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or harnesses.is_secret_file(src_root, st):
            return False
        try:
            old = os.lstat(dst)
        except FileNotFoundError:
            old = None
        if old and stat.S_ISREG(old.st_mode) and (old.st_mtime_ns > st.st_mtime_ns or old.st_size > st.st_size):
            raise OSError(f"{dst.name} under the target is newer or larger than the agent's own")
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")   # a new file renamed over dst: an existing dst (a
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)   # link too) is never written
        try:
            os.fchmod(out, 0o600)
            with os.fdopen(out, "wb") as f:
                while chunk := os.read(fd, 1 << 20):
                    f.write(chunk)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))   # the copy is never newer than its source
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return True
    finally:
        os.close(fd)


def _mkdir(d):
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.is_symlink():
        raise OSError(f"{d} is a link")


def copy_transcript(src_file, src, target, sid):
    """Copy the transcript and its <sid>/ sub-agent folder into target's projects/<project>/. Nothing to do when both
    accounts share one projects folder."""
    src_root, dst_projects = _root(src), _root(target) / "projects"
    if _same(src_file.parent.parent, dst_projects):
        return
    dst_dir = dst_projects / src_file.parent.name
    _mkdir(dst_dir)
    if not _copy_file(src_file, dst_dir / src_file.name, src_root):
        raise OSError("the transcript is not a regular file")
    sub = src_file.parent / sid
    if not sub.is_dir() or sub.is_symlink():
        return
    for dirpath, _dirs, files in os.walk(sub):   # links to folders are listed, never walked into
        rel = Path(dirpath).relative_to(sub)
        for name in files:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            try:
                _mkdir(dst_dir / sid / rel)
                _copy_file(p, dst_dir / sid / rel / name, src_root)
            except OSError:
                continue   # a sub-agent file that cannot be copied is not worth losing the move


def _copy(src, target, sid):
    src_file = find_transcript(src, sid)
    if src_file is None:
        raise OSError("the transcript went away")
    copy_transcript(src_file, src, target, sid)


def _relaunch(c, w, account, prompt):
    from pl import dispatch   # dispatch imports this module
    label = f"{w.get('stage')}:{slug_of(c)[:28]}"
    return dispatch._launch(w["pane"], _window_name(c, w), harnesses.launch_script(harnesses.unattended(harnesses.get("claude")), account, prompt,
                                                                    w["session_id"], label, resume=True))


FRESH_KEYS = ("pane", "session_id", "profile", "started_at")   # a change in any: the card data is stale


def _refuse(c, target, why):
    events.emit("agent_move_refused", c["id"], to=target, reason=why[:120])
    return False, why, False


def move(c, target, reason, dry=False, held=False):
    """(moved, message, worker_changed): worker_changed means c["metadata"] holds a new worker to use.
    held: the caller already holds this card's move lock (the dispatcher)."""
    m = c.get("metadata") or {}
    w = m.get("worker") or {}
    why = refusal(w, target) or pane_refusal(c, w)
    if why:
        return (False, why, False) if dry else _refuse(c, target, why)
    src, sid = w["profile"], w["session_id"]
    if dry:
        return True, f"would move the {w.get('stage')} agent from {src} to {target}", False
    if not held and not lock(c["id"]):
        return False, "another move of this card is in progress", False
    try:
        fresh_next()
        fw = ((card(c["id"]) or {}).get("metadata") or {}).get("worker") or {}
        if any(fw.get(k) != w.get(k) for k in FRESH_KEYS):
            return _refuse(c, target, "the card's agent changed since it was read; not moved")
        return _move(c, m, w, src, sid, target, reason)
    finally:
        if not held:
            unlock(c["id"])


def _move(c, m, w, src, sid, target, reason):
    try:
        _copy(src, target, sid)   # before any key: a copy that cannot be made leaves the agent running
    except OSError as e:
        return _refuse(c, target, f"its transcript could not be copied: {e}")
    if not stop(w["pane"], sid):
        notify(f"Agent move stopped: {short_id(c['id'])}", f"its {w.get('stage')} agent still runs under {src} after Ctrl-C; see its window")
        return _refuse(c, target, "its old process is still running after Ctrl-C; not relaunched")
    partial = None
    try:
        _copy(src, target, sid)   # again: the lines written while it stopped
    except OSError as e:
        partial = str(e)[:120]    # the first copy is intact: it resumes from there
    try:
        script = _relaunch(c, w, target, CONTINUE_PROMPT)
    except (SystemExit, OSError) as e:   # stopped, but it could not be started again: the next pass starts it fresh
        update(c["id"], metadata={"worker": None})
        c["metadata"] = {**m, "worker": None}
        why = f"agent stopped but relaunch failed for {short_id(c['id'])}"
        events.emit("agent_move_failed", c["id"], to=target, reason=f"{why}: {e}"[:120])
        if alerts.open(f"agent_move_failed:{c['id']}", "warn", f"Agent stopped but relaunch failed: {short_id(c['id'])}",
                       "the dispatcher starts it again fresh on its next pass"):
            notify(f"Agent move failed: {short_id(c['id'])}", why)
        return False, why, True
    nw = {**w, "profile": target, "moved_at": now_iso(), "launch": str(script), "resume": {"at": time.time()}}
    sw = {"at": now_iso(), "from": src, "to": target, "stage": w.get("stage"), "reason": reason, "resumed": True}
    if partial:
        sw["partial"] = partial
    meta = {"worker": nw, "profile": target, "profile_switches": (m.get("profile_switches") or []) + [sw]}
    update(c["id"], metadata=meta)
    c["metadata"] = {**m, **meta}
    events.emit("agent_moved", c["id"], stage=w.get("stage"), to=target, session=sid, reason=reason, **{"from": src})
    notify(f"Agent moved: {short_id(c['id'])}", f"moved {short_id(c['id'])} to {target} and resumed it")
    tail = f" (its last lines may be missing: {partial})" if partial else ""
    return True, f"moved the {w.get('stage')} agent from {src} to {target}; it resumes session {sid[:8]}{tail}", True


def _text(e):
    """The text of one transcript entry's message (a string or a list of text blocks)."""
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else e.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def limit_screen(c, w, h):
    """For an agent pl relaunched with --resume: the text of a usage-limit entry its transcript got after the
    relaunch, else None. The screen is never read here: --resume replays the old limit banner on it."""
    r = w.get("resume")
    at = r.get("at") if isinstance(r, dict) else None
    f = find_transcript(w.get("profile"), w.get("session_id"))
    if not isinstance(at, (int, float)) or f is None:
        return None
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - TAIL))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("assistant", "system"):
            continue
        if parse_iso(e.get("timestamp") or "") <= at:
            break   # the entries are in time order: the rest is from before the relaunch
        text = _text(e)
        if harnesses.limit_hit(h, text):
            return text
    return None


def move_card(ref, target=None, confirm=None):
    """pl move-agent: move one card's live agent to target (default: another healthy Claude account).
    confirm(question) -> bool is asked first when given."""
    c = find_card(ref)
    w = (c.get("metadata") or {}).get("worker") or {}
    if not w or worker_status(w, registry())[0] not in ("alive", "starting") or locked(c["id"]):
        raise SystemExit(f"pl move-agent: {short_id(c['id'])} has no live agent to move")
    if target is None:
        target = healthy_profile(None, cards(), harness="claude", skip=w.get("profile"))
        if target is None:
            raise SystemExit("pl move-agent: no other Claude account with credits to move it to")
    if target in exhausted_profiles():
        raise SystemExit(f"pl move-agent: {target} is parked for its usage limit (pl accounts)")
    why = refusal(w, target)
    if why:
        raise SystemExit(f"pl move-agent: not moved: {why}")
    if confirm and not confirm(f"Move the {w.get('stage')} agent of {short_id(c['id'])} from {w.get('profile')} to {target}? "
                               "pl stops it with Ctrl-C in its window and resumes the same session there."):
        return "not moved"
    ok, msg, _ = move(c, target, "by hand")
    if not ok:
        raise SystemExit(f"pl move-agent: {msg}")
    return msg


def cmd_move_agent(a):
    ask = None if a.yes else (lambda q: input(q + " [y/N] ").strip().lower() in ("y", "yes"))
    print(move_card(a.card, a.account, ask))
