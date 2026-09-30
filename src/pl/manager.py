"""pl manager: one process per machine. It keeps one dispatcher running for each managed profile and writes
status.json, which consoles and dispatchers read. Opt-in: nothing changes until `pl manager start` runs once."""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TICK = 5                      # seconds between ticks, as the dispatcher's fast guard
STALE = 3 * TICK              # a status older than this is ignored: a dead manager never freezes the pipeline
BACKOFF = (10, 30, 120, 300)  # seconds before restarting a dispatcher that exited: 1st, 2nd, 3rd, 4th+ time
MAX_RESTARTS = 5              # restarts in an hour; the next exit gives the profile up
WAIT = 5.0                    # seconds start/stop wait for the manager lock
_clock = time.time
_sleep = time.sleep
MACHINE_TOML = """# pl manager settings for this machine. Delete this file and consoles stop starting the manager.
[limits]
max_live_agents = 8          # agent and loop windows across every profile; more hold new starts
max_agents_memory = "60%"    # past it the largest agent tree is stopped; over 80% of it holds new starts
min_free_memory = "15%"      # less free memory holds new starts
kill_runaway = true          # false: only notify
"""


def machine_dir() -> Path:
    return Path(os.environ.get("PL_MACHINE_DIR") or Path.home() / ".local" / "state" / "pl-machine")


def _path(name):
    return machine_dir() / name


def _run(argv):
    """The one subprocess seam of this module (tests fake it)."""
    return subprocess.run(argv, capture_output=True, text=True)


def _notify(title, msg, cmds=()):
    for cmd in cmds:
        _run([cmd, "notify", title, msg])


def _pid(lock):
    try:
        m = re.match(r"pid (\d+)", lock.read_text())
    except OSError:
        return None
    return int(m.group(1)) if m else None


def holder_pid():
    return _pid(_path("manager.lock"))


def running() -> bool:
    """The machine lock is held: a manager runs."""
    from pl.profiles import _is_locked
    return _is_locked(_path("manager.lock"))


def _write(name, data):
    """Atomic: temp file in the same folder, then rename. Readers never see half a file."""
    d = machine_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = d / f".{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, d / name)


def read_status(max_age=STALE):
    """status.json, or None when there is none or it is older than max_age seconds."""
    try:
        st = json.loads(_path("status.json").read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and _clock() - float(st.get("at") or 0) <= max_age else None


def managed():
    """Every profile whose [dispatch] autostart is not false, with what the manager needs to start it."""
    from pl import config as C
    from pl.profiles import _is_locked, list_profiles
    out = []
    for row in list_profiles():
        d = Path(row["dir"])
        try:
            t = tomllib.loads((d / "config.toml").read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if t.get("dispatch", {}).get("autostart", True) is False:
            continue
        gh = t.get("code_host", {}).get("gh_config_dir")
        att = t.get("paths", {}).get("attention_cmd")
        lock = d / "state" / "pl-dispatch.lock"
        g = {**C._defaults(), "CONFIG_DIR": d}
        try:
            C._apply(g, t)
        except Exception:  # noqa: BLE001 - a profile whose loops do not load has none
            g["SERVICES"] = {}
        out.append({"loops": set(g["SERVICES"]), "name": row["name"], "dir": d, "session": row["tmux_session"], "lock": lock,
                    "running": _is_locked(lock), "gh": d / Path(gh).expanduser() if isinstance(gh, str) and gh else None,
                    "attention": str(Path(att).expanduser()) if isinstance(att, str) and att else None})
    return out


def _alerts(profs):
    return sorted({p["attention"] for p in profs if p["attention"] and os.access(p["attention"], os.X_OK)})


def _limits(state):
    """machine.toml [limits] over the defaults; a file that does not load or validate keeps the defaults."""
    from pl import config as C
    try:
        t = tomllib.loads(_path("machine.toml").read_text())
        errs = C.validate_machine(t)
    except (OSError, tomllib.TOMLDecodeError) as e:
        t, errs = {}, [] if isinstance(e, FileNotFoundError) else [str(e)]
    if errs != state.get("limit_errs", []):
        print("warning: machine.toml not used, keeping the default limits: " + "; ".join(errs), flush=True)
    state["limit_errs"] = errs
    return {**C.MACHINE_DEFAULTS, **({} if errs else t.get("limits", {}))}


def _caps(state, profs):
    """Count agent windows and their memory across every managed profile's tmux session; stop the largest tree
    past max_agents_memory (one per tick). Returns (live agents, agent memory %, the hold reason or None)."""
    from pl import memory
    lim, r = _limits(state), memory.reading()
    panes = memory._run(["tmux", "list-panes", "-a", "-F", "#{session_name} #{window_id} #{pane_pid} #{window_name}"])
    procs, by_session, trees = memory._procs(), {p["session"]: p for p in profs}, []
    for line in (panes or "").splitlines():
        s = line.split(" ", 3)
        p = by_session.get(s[0])
        if len(s) < 4 or not s[2].isdigit() or not p or not (s[3].startswith(memory.AGENT_PREFIXES) or s[3] in p["loops"]):
            continue
        tree = memory._below(procs, [int(s[2])])
        trees.append((sum(procs[x][1] for x in tree), p["name"], s[3], int(s[2]), tree))
    live, used, why = len(trees), sum(t[0] for t in trees), []
    if live >= lim["max_live_agents"]:
        why.append(f"{live} live agents across profiles (max {lim['max_live_agents']})")
    pct, over = None, set()
    if r:
        free, total = r
        cap, pct = memory.parse_size(lim["max_agents_memory"], total), round(100 * used / total)
        if free < memory.parse_size(lim["min_free_memory"], total):
            why.append(f"only {memory.gb(free)} free (minimum {memory.gb(memory.parse_size(lim['min_free_memory'], total))})")
        if used > 0.8 * cap:
            why.append(f"agents use {memory.gb(used)}, over 80% of the {memory.gb(cap)} cap")
        if used > cap and trees:
            mem, name, win, pane_pid, tree = max(trees, key=lambda t: t[0])
            over.add(f"{name}:{win}")
            harness = [x for x in tree if procs[x][0] == pane_pid]
            skip = {os.getpid(), *(_pid(p["lock"]) for p in profs)}   # the pane shell, harness, dispatchers: never
            victims = {x: procs[x][2] for x in memory._below(procs, harness) if x not in skip}
            kill = lim["kill_runaway"] is not False and bool(victims)
            if kill:
                memory._stop(harness, victims)
            if kill or f"{name}:{win}" not in state.get("runaway", set()):
                _notify(f"Runaway agent: {name} {win}", f"profile {name} window {win}: {memory.gb(mem)}; all agents "
                        f"{memory.gb(used)}, machine cap {memory.gb(cap)}" + (f"; stopped {len(victims)} child processes"
                                                                              if kill else ""), _alerts(profs))
    state["runaway"] = over
    hold = "; ".join(why) or None
    if hold and not state.get("hold"):
        print(f"hold: {hold}", flush=True)
        _notify("pl manager holds new agents", hold, _alerts(profs))
    state["hold"] = hold
    return live, lim["max_live_agents"], pct, hold


def tick(state):
    """One pass: start each managed dispatcher that does not run (with backoff after an exit), write status.json."""
    from pl.dispatch import start_for
    now, rows, profs = _clock(), [], managed()
    live, cap, pct, hold = _caps(state, profs)
    for p in profs:
        r = state.setdefault(p["name"], {"up": False, "restarts": [], "next": 0, "gave_up": False})
        r["restarts"] = [t for t in r["restarts"] if now - t < 3600]
        if p["running"]:
            r["up"] = True
        elif r["up"]:   # it ran (or was started) and is gone: an exit
            r["up"] = False
            if len(r["restarts"]) >= MAX_RESTARTS:
                r["gave_up"] = True
                print(f"{p['name']}: dispatcher exited {len(r['restarts']) + 1} times in an hour; not restarting", flush=True)
                _notify("pl manager gave up", f"the dispatcher of profile {p['name']} keeps exiting; pl manager stopped "
                        "restarting it (see its tmux window dispatch)", _alerts([p]))
            else:
                r["next"] = now + BACKOFF[min(len(r["restarts"]), len(BACKOFF) - 1)]
        if not p["running"] and not r["gave_up"] and now >= r["next"]:
            err = start_for(p["dir"], p["session"], p["gh"], managed=True)
            print(f"{p['name']}: " + (f"dispatcher failed to start — {err}" if err else "dispatcher started"), flush=True)
            if r["next"]:
                r["restarts"].append(now)
            r["up"], r["next"] = True, 0
        rows.append({"name": p["name"], "running": p["running"], "dispatcher_pid": _pid(p["lock"]) if p["running"] else None,
                     "restarts": len(r["restarts"]), "gave_up": r["gave_up"]})
    status = {"at": now, "pid": os.getpid(), "profiles": rows, "live_agents": live, "max_live_agents": cap,
              "agent_memory_pct": pct, "hold": hold}
    _write("status.json", status)
    return status


def machine_lock():
    """Hold the machine lock for this process's lifetime, or None when another manager holds it."""
    d = machine_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = open(d / "manager.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    f.flush()
    return f


def _run_loop():
    lock = machine_lock()   # noqa: F841 - kept open to hold the lock
    if lock is None:
        print(f"a manager is already running on this machine (pid {holder_pid() or '?'})")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # dispatchers live in tmux and keep running
    print(f"pl manager started (pid {os.getpid()})", flush=True)
    state = {}
    try:
        while True:
            tick(state)
            _sleep(TICK)
    finally:
        _path("status.json").unlink(missing_ok=True)


def _wait(held):
    end = time.monotonic() + WAIT
    while running() != held:
        if time.monotonic() > end:
            return False
        time.sleep(0.1)
    return True


def start():
    """Start the manager detached (its own session, so tmux restore cannot bring it back). A no-op when it runs."""
    if running():
        return f"manager: running (pid {holder_pid() or '?'})"
    machine_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _path("machine.toml").exists():
        _path("machine.toml").write_text(MACHINE_TOML)
    with open(_path("manager.log"), "a") as log:
        subprocess.Popen([sys.executable, "-m", "pl", "manager", "run"], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True)
    if not _wait(True):
        return f"manager: failed to start — no lock after {WAIT:g} s; see {_path('manager.log')}"
    return f"manager: started (pid {holder_pid() or '?'})"


def stop(all_=False):
    """SIGTERM the manager and wait for its lock to free. all_: also stop each managed dispatcher."""
    note, pid = "manager: not running", holder_pid()
    if running() and pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        note = "manager: stopped" if _wait(False) else f"manager: still running (pid {pid})"
    if all_:
        from pl import config as C
        from pl import dispatch
        for p in managed():
            C.load(config_dir=str(p["dir"]))
            note += f"\n{p['name']}: {dispatch.stop_dispatcher()}"
    return note


def show():
    st = read_status(max_age=float("inf"))
    if not running() or not st:
        return "manager: not running" + ("" if _path("machine.toml").exists() else " (never started on this machine)")
    lines = [f"manager: running (pid {st['pid']}), status {int(_clock() - st['at'])} s old",
             f"{'profile':<16}{'dispatcher':<12}{'pid':<8}restarts"]
    for p in st["profiles"]:
        state = "gave up" if p["gave_up"] else "running" if p["running"] else "stopped"
        lines.append(f"{p['name']:<16}{state:<12}{str(p['dispatcher_pid'] or '-'):<8}{p['restarts']}")
    if st.get("hold"):
        lines.append(f"hold: {st['hold']}")
    return "\n".join(lines)


def cmd_manager(argv):
    ap = argparse.ArgumentParser(prog="pl manager", description="one manager per machine: it runs the dispatcher of "
                                 "every profile whose [dispatch] autostart is not false")
    ap.add_argument("action", choices=["start", "stop", "status", "run"])
    ap.add_argument("--all", action="store_true", help="stop: also stop every managed dispatcher")
    a = ap.parse_args(argv)
    if a.action == "run":
        return _run_loop()
    print({"start": start, "status": show}.get(a.action, lambda: stop(a.all))())
    return 0
