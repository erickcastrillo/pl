"""Memory safety net: hold new agent starts while memory is low, and stop an agent whose process tree runs away."""
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter

from pl import config as C
from pl import alerts, events
from pl.util import notify

PLATFORM = sys.platform
CACHE_SECONDS = 10
_CACHE = {}
_clock = time.monotonic
GB = 1024 ** 3
UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": GB, "gb": GB, "t": 1024 ** 4, "tb": 1024 ** 4}
AGENT_PREFIXES = ("spec-", "design-", "plan-", "run-")
KILL_GRACE = 5   # seconds between SIGTERM and SIGKILL for a runaway tree
_sleep = time.sleep


def _run(argv):
    """The one subprocess seam of this module (tests fake it): stdout, or None when the command fails."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _meminfo():
    try:
        with open("/proc/meminfo") as f:
            return f.read()
    except OSError:
        return None


def _kill(pid, sig):
    os.kill(pid, sig)


def _read():
    if PLATFORM == "darwin":
        vm, total = _run(["vm_stat"]), _run(["sysctl", "-n", "hw.memsize"])
        if not vm or not total or not total.strip().isdigit():
            return None
        page = re.search(r"page size of (\d+) bytes", vm)
        pages = re.findall(r"^Pages (?:free|inactive|speculative):\s+(\d+)\.", vm, re.M)
        if not page or len(pages) < 3:
            return None
        return sum(map(int, pages)) * int(page.group(1)), int(total.strip())
    info = dict(re.findall(r"^(MemAvailable|MemTotal):\s+(\d+) kB", _meminfo() or "", re.M))
    if len(info) < 2:
        return None
    return int(info["MemAvailable"]) * 1024, int(info["MemTotal"]) * 1024


def reading():
    """(available bytes, total bytes), or None when unknown. Cached for CACHE_SECONDS."""
    now = _clock()
    if "at" not in _CACHE or now - _CACHE["at"] >= CACHE_SECONDS:
        _CACHE.update(at=now, val=_read())
    return _CACHE["val"]


def parse_size(v, total):
    """Bytes from a whole number of bytes, "15%" of total, or "4GB" / "512 MB" / "2g". ValueError otherwise."""
    if isinstance(v, bool):
        raise ValueError(v)
    if isinstance(v, int) and v >= 0:
        return v
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(%|[a-zA-Z]+)\s*", str(v))
    if not m or (m.group(2) != "%" and m.group(2).lower() not in UNITS):
        raise ValueError(v)
    n = float(m.group(1))
    return int(total * n / 100) if m.group(2) == "%" else int(n * UNITS[m.group(2).lower()])


def gb(n):
    return f"{n / GB:.1f} GB"


def _limit(key, total):
    """A [dispatch] size in bytes; a value that does not parse falls back to the default."""
    try:
        return parse_size(C.DISPATCH.get(key), total)
    except ValueError:
        return parse_size(C.MEMORY_DEFAULTS[key], total)


def status():
    """{"free", "total", "low"} for the console, or None when memory cannot be read."""
    r = reading()
    if r is None:
        return None
    free, total = r
    return {"free": free, "total": total, "low": free < _limit("min_free_memory", total)}


def check_starts(st):
    """None when agents may start; else the line to print. One event and one notification per low-memory episode."""
    s = status()
    if not s or not s["low"]:
        st.pop("memory_low", None)
        alerts.resolve("memory_low")
        return None
    line = f"starts paused: only {gb(s['free'])} free (minimum {gb(_limit('min_free_memory', s['total']))})"
    if not st.get("memory_low"):
        st["memory_low"] = True
        events.emit("memory_low", message=line)
    if why := alerts.open("memory_low", "high", "Low memory: pl holds new agents",
                          "close apps, or lower [dispatch] min_free_memory"):
        notify(alerts.headline(why, "Low memory: pl holds new agents"), line)
    return line


def _short(cmd):
    """ "/usr/bin/ruby /gems/vite_ruby-3.9/bin/vite build" -> "ruby … bin/vite build"."""
    t = cmd.split()
    if not t:
        return "?"
    tail = t[1:][-2:]
    rest = ["/".join(x.split("/")[-2:]) for x in tail]
    cut = len(t) > 3 or rest != tail
    return " ".join([os.path.basename(t[0]) or t[0], *(["…"] if cut else []), *rest])


def _procs():
    """pid -> (parent pid, rss bytes, command) for every process."""
    procs = {}
    for line in (_run(["ps", "-axo", "pid=,ppid=,rss=,command="]) or "").splitlines():
        p = line.split(None, 3)
        if len(p) >= 3 and all(x.isdigit() for x in p[:3]):
            procs[int(p[0])] = (int(p[1]), int(p[2]) * 1024, p[3] if len(p) > 3 else "")
    return procs


def _below(procs, roots):
    """Every descendant of the roots, the roots themselves excluded."""
    kids = {}
    for pid, (ppid, _, _) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    out, seen, todo = [], set(roots), list(roots)
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in seen:
                seen.add(k)
                out.append(k)
                todo.append(k)
    return out


def _signal(pid, sig):
    try:
        _kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop(harness, victims):
    """SIGTERM the victims ({pid: command}); after KILL_GRACE s, SIGKILL whatever of them is left (same pid, same
    command: an orphan re-parented away from the harness still counts) and whatever the harness started since."""
    for x in victims:
        _signal(x, signal.SIGTERM)
    waited = 0
    while True:
        procs = _procs()
        left = sorted({x for x, cmd in victims.items() if x in procs and procs[x][2] == cmd}
                      | {x for x in _below(procs, harness) if x != os.getpid()})
        if not left or waited >= KILL_GRACE:
            break
        _sleep(1)
        waited += 1
    for x in left:
        _signal(x, signal.SIGKILL)


def guard_runaways(st, all_cards, dry=False):
    """Stop an agent or loop window whose process tree passes [dispatch] max_agent_processes or max_agent_memory:
    SIGTERM what the harness started, SIGKILL after KILL_GRACE s; notify once per window per episode.
    The pane shell, the harness and the dispatcher are never signalled. kill_runaway = false only notifies."""
    r = reading()
    panes = _run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id} #{pane_pid} #{window_name}"])
    if not r or not panes:
        return
    procs = _procs()
    owner = {((c.get("metadata") or {}).get("worker") or {}).get("window"): c for c in all_cards}
    max_n, max_mem = C.DISPATCH.get("max_agent_processes", 150), _limit("max_agent_memory", r[1])
    before, now = set(st.get("runaway") or []), set()
    for line in panes.splitlines():
        p = line.split(" ", 2)
        if len(p) < 3 or not p[1].isdigit():
            continue
        win, pane_pid, name = p[0], int(p[1]), p[2]
        if not name.startswith(AGENT_PREFIXES) and name not in C.SERVICES:
            continue
        tree = _below(procs, [pane_pid])
        mem = sum(procs[x][1] for x in tree)
        if len(tree) <= max_n and mem <= max_mem:
            continue
        now.add(win)
        if win in before:
            continue
        harness = [x for x in tree if procs[x][0] == pane_pid]   # the launch script execs the harness in the pane
        victims = [x for x in _below(procs, harness) if x != os.getpid()]
        top, n = Counter(procs[x][2] for x in (victims or tree)).most_common(1)[0]
        c = owner.get(win) or {}
        kill = C.DISPATCH.get("kill_runaway") is not False and not dry and bool(victims)
        if kill:
            _stop(harness, {x: procs[x][2] for x in victims})
        msg = (f"{(c.get('id') or 'loop')[:8]} window {name}: {len(tree)} processes; {_short(top)} ×{n}, {gb(mem)}"
               + (f"; stopped {len(victims)} child processes" if kill else ""))
        # the command is the program name only: argv can hold secrets
        events.emit("runaway", c.get("id"), window=name, processes=len(tree), memory=gb(mem),
                    command=os.path.basename(top.split()[0]) if top.split() else "?", stopped=len(victims) if kill else 0)
        who = f"card {c['id'][:8]}" if c.get("id") else f"loop {name}" if name in C.SERVICES else f"window {win}"
        if why := alerts.open(f"runaway:{win}", "high", f"Runaway agent in {who}",
                              "pl stopped its child processes" if kill else "stop it by hand (kill_runaway is off)"):
            notify(alerts.headline(why, f"Runaway agent: {(c.get('title') or name)[:40]}"), msg)
        print(f"runaway: {msg}")
    st["runaway"] = sorted(now)
    alerts.sweep("runaway:", {f"runaway:{w}" for w in now}, notify)
