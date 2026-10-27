"""Background jobs of a stage agent that can never report back: children of the harness whose output goes to a file,
read from `ps` and /proc only. pl marks them and says so; it never signals them."""
import os
import subprocess
import sys

from pl import config as C
from pl import harnesses, memory
from pl.util import mask


def _run(argv):
    """The one subprocess seam of this module (tests fake it): stdout, or None when the command fails."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _link(pid, fd):
    try:
        return os.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return None


def _procs():
    """pid -> (parent pid, seconds running, command) for every process."""
    procs = {}
    for line in (_run(["ps", "-axo", "pid=,ppid=,etimes=,args="]) or "").splitlines():
        p = line.split(None, 3)
        if len(p) >= 3 and all(x.isdigit() for x in p[:3]):
            procs[int(p[0])] = (int(p[1]), int(p[2]), p[3] if len(p) > 3 else "")
    return procs


def _outputs(tree):
    links = (_link(x, fd) for x in tree for fd in (1, 2))
    return [x for x in links if x and x.startswith("/") and not x.startswith("/dev/")]


def of(harness_pid):
    """The background jobs of a harness: {"pid", "command", "minutes", "state"} for each direct child whose tree writes
    to a file. state: "orphaned" (an output file was deleted), "overrun" (past [watchdog] job_minutes) or "ok"."""
    if not sys.platform.startswith("linux") or not isinstance(harness_pid, int) or isinstance(harness_pid, bool):
        return []
    procs, limit, out = _procs(), C.WATCHDOG.get("job_minutes", 45), []
    for kid in sorted(pid for pid, p in procs.items() if p[0] == harness_pid):
        tree = [kid, *memory._below(procs, [kid])]
        files = _outputs(tree)
        if not files:
            continue
        minutes = procs[kid][1] // 60
        state = "orphaned" if any(f.endswith(" (deleted)") for f in files) else "overrun" if limit and minutes >= limit else "ok"
        out.append({"pid": kid, "command": memory._short(procs[tree[-1]][2]), "minutes": minutes, "state": state})
    return out


def stuck(js):
    return js if js and all(j["state"] != "ok" for j in js) else []


def describe(j):
    why = "output deleted" if j["state"] == "orphaned" else f"over {C.WATCHDOG.get('job_minutes', 45)} min"
    # the text reaches a live terminal, the alerts file, events and notifications: no control characters, no secrets
    return harnesses._typeable(mask(" ".join(f"{j['command']} (pid {j['pid']}, {j['minutes']} min, {why})".split())))


def nudge(js):
    return (f"pl: you are waiting on background jobs that cannot report back: {'; '.join(map(describe, js))}. "
            "Stop them, then re-run what you need in the foreground with a time limit (for example: timeout 1200 <command>).")
