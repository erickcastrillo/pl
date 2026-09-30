"""pl manager: one process per machine. It keeps one dispatcher running for each managed profile and writes
status.json, which consoles and dispatchers read. On by default: every console starts it unless machine.toml says
[manager] enabled = false. A dispatcher already running for a profile is adopted (its lock is seen), never doubled."""
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
LOCK_GRACE = 30               # seconds a dispatcher the manager started has to take its lock (slow under memory pressure)
_clock = time.time
_sleep = time.sleep
MACHINE_TOML = """# pl manager settings for this machine. Deleting this file keeps the defaults below.
[manager]
enabled = true               # false: each console starts its own profile's dispatcher instead of the manager

[limits]
# max_live_agents = 12       # spec/design/plan/run agent windows across every profile; more hold new starts.
                             # Unset: every managed profile's max_runs + max_prep added up, at least 8
max_agents_memory = "60%"    # past it the largest agent tree is stopped; over 80% of it holds new starts
min_free_memory = "15%"      # less free memory holds new starts
kill_runaway = true          # false: only notify
"""


def machine_dir() -> Path:
    return Path(os.environ.get("PL_MACHINE_DIR") or Path.home() / ".local" / "state" / "pl-machine")


def _path(name):
    return machine_dir() / name


def enabled() -> bool:
    """The manager is on unless machine.toml sets [manager] enabled = false; no file, or one that does not load, is on."""
    m = _machine_toml().get("manager", {})
    return not isinstance(m, dict) or m.get("enabled", True) is not False


def _machine_toml():
    """machine.toml as a dict; {} when there is none or it does not load."""
    try:
        return tomllib.loads(_path("machine.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def write_machine_toml() -> bool:
    """Write machine.toml with the defaults and a comment per key; True when written, False when one is already there."""
    machine_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with open(_path("machine.toml"), "x") as f:
            f.write(MACHINE_TOML)
    except FileExistsError:
        return False
    return True


def _run(argv, timeout=None):
    """The one subprocess seam of this module (tests fake it)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _notify(title, msg, cmds=()):
    """A notifier that hangs or fails never stops the manager."""
    for cmd in cmds:
        try:
            _run([cmd, "notify", title, msg], timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"notify failed ({cmd}): {type(e).__name__}", flush=True)


def _holder(lock):
    """(pid, lock time in epoch seconds or None) from a lock file's "pid N ... since T" line; (None, None) without one."""
    from pl.util import parse_iso
    try:
        m = re.match(r"pid (\d+)(?:.* since (\S+))?", lock.read_text())
    except OSError:
        return None, None
    return (int(m.group(1)), parse_iso(m.group(2) or "") or None) if m else (None, None)


def _seconds(etime):
    """ps etime "[[dd-]hh:]mm:ss" in seconds, or None."""
    days, _, hms = etime.rpartition("-")
    parts = hms.split(":")
    if not (days or "0").isdigit() or not all(x.isdigit() for x in parts) or not 2 <= len(parts) <= 3:
        return None
    return int(days or 0) * 86400 + sum(int(x) * 60 ** i for i, x in enumerate(reversed(parts)))


def _alive(pid, what, since=None):
    """pid runs, its command line holds `what` and, given the lock's time, it started before that lock was written:
    a reused pid is someone else. Never takes a lock."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    etime, _, cmd = ((getattr(_run(["ps", "-o", "etime=,command=", "-p", str(pid)], timeout=10), "stdout", "") or "")
                     .strip().partition(" "))
    up = _seconds(etime)
    return what in cmd and not (since and up is not None and _clock() - up > since + 5)


def holder_pid():
    return _holder(_path("manager.lock"))[0]


def running() -> bool:
    """The pid in the machine lock file is a live pl manager. Never takes the lock."""
    pid, since = _holder(_path("manager.lock"))
    return bool(pid) and _alive(pid, "pl manager", since)


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
    at = st.get("at") if isinstance(st, dict) else None
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    return st if 0 <= _clock() - at <= max_age else None


def manages(name):
    """A running manager lists this profile in its fresh status: it, not a hand-typed `pl dispatch`, runs it."""
    st = read_status() if running() else None
    return any(p.get("name") == name and not p.get("gave_up") and not p.get("stopped")
               for p in (st or {}).get("profiles") or [])


def managed():
    """Every profile whose [dispatch] autostart is not false, with what the manager needs to start it.
    A profile whose config does not load, has no [tracker] type or fails validation is {"name", "error"}: marked
    in the status as "config error: <reason>", never started, never notified about."""
    import tomlkit
    from pl import config as C
    from pl.profiles import list_profiles
    out = []
    for row in list_profiles(check_lock=False):   # a lock probe would make a dispatcher starting then exit
        name, d = row["name"], Path(row["dir"])
        try:
            t = tomllib.loads((d / "config.toml").read_text())
            if t.get("dispatch", {}).get("autostart", True) is False:
                continue
            tr = t.get("tracker")
            errs = (["no [tracker] type"] if not (isinstance(tr, dict) and tr.get("type"))
                    else C.validate(tomlkit.parse((d / "config.toml").read_text())))
            if errs:
                out.append({"name": name, "error": f"config error: {errs[0]}"})
                continue
            gh = t.get("code_host", {}).get("gh_config_dir")
            att = t.get("paths", {}).get("attention_cmd")
            g = {**C._defaults(), "CONFIG_DIR": d}
            try:
                C._apply(g, t)
            except Exception:  # noqa: BLE001 - a profile whose loops do not load has none
                g["SERVICES"] = {}
            pid, since = _holder(d / "state" / "pl-dispatch.lock")
            slots = sum(v for k in ("max_runs", "max_prep")
                        if isinstance(v := g["DISPATCH"].get(k), int) and not isinstance(v, bool))
            out.append({"loops": set(g["SERVICES"]), "slots": slots, "name": name, "dir": d, "session": row["tmux_session"], "pid": pid,
                        "running": bool(pid) and _alive(pid, "pl dispatch", since),
                        "gh": d / Path(gh).expanduser() if isinstance(gh, str) and gh else None,
                        "attention": str(Path(att).expanduser()) if isinstance(att, str) and att else None})
        except Exception as e:  # noqa: BLE001 - one bad profile never stops the others
            out.append({"name": name, "error": f"config error: does not load ({type(e).__name__})"})
    return out


def _alerts(profs):
    return sorted({p["attention"] for p in profs if p["attention"] and os.access(p["attention"], os.X_OK)})


def _limits(profs=()):
    """machine.toml [limits] over the defaults; [limits] that do not validate keep the defaults (a bad [manager]
    value never drops them). max_live_agents unset: every managed profile's max_runs + max_prep, at least 8."""
    from pl import config as C
    lim = _machine_toml().get("limits", {})
    lim = lim if isinstance(lim, dict) and not C.validate_machine({"limits": lim}) else {}
    auto = max(C.MACHINE_DEFAULTS["max_live_agents"], sum(p.get("slots", 0) for p in profs))
    return {**C.MACHINE_DEFAULTS, "max_live_agents": auto, **lim}


HOLD_NOTIFY_EVERY = 3600   # seconds: a hold that ends and starts again notifies at most once in this time


def _machine_check(state):
    """machine.toml's problems as one "machine.toml: ..." line, or None. A new problem is logged and written to the
    machine events.jsonl once; the manager then runs with the defaults for what is wrong."""
    from pl import config as C
    errs = C.validate_machine(_machine_toml())
    note = f"machine.toml: {'; '.join(errs)}" if errs else None
    if note and note != state.get("machine_error"):
        print(note, flush=True)
        try:
            with open(_path("events.jsonl"), "a") as f:
                f.write(json.dumps({"at": _clock(), "kind": "machine_toml_invalid", "detail": note}) + "\n")
        except OSError:
            pass
    state["machine_error"] = note
    return note


def _caps(state, profs):
    """Count agent windows and their memory across every managed profile's tmux session; stop the largest runaway
    tree past max_agents_memory (one per tick). Loops count toward memory, never toward max_live_agents.
    Returns (live agents, max live agents, agent memory %, the hold reason or None)."""
    from pl import memory
    from pl.agents import run_waiting
    lim, r = _limits(profs), memory.reading()
    panes = memory._run(["tmux", "list-panes", "-a", "-F", "#{session_name} #{window_id} #{pane_pid} #{window_name}"])
    procs, by_session, trees, live = memory._procs(), {p["session"]: p for p in profs}, [], 0
    for line in (panes or "").splitlines():
        s = line.split(" ", 3)
        p = by_session.get(s[0])
        if len(s) < 4 or not s[2].isdigit() or not p or not (s[3].startswith(memory.AGENT_PREFIXES) or s[3] in p["loops"]):
            continue
        tree = memory._below(procs, [int(s[2])])
        trees.append((sum(procs[x][1] for x in tree), p["name"], s[3], int(s[2]), tree))
        # a run agent waiting at a GATE or idle holds no start, as in the dispatcher; it still counts toward memory
        if s[3].startswith(memory.AGENT_PREFIXES) and not (s[3].startswith("run-") and run_waiting({"pane": s[1]}, {})):
            live += 1
    used, why = sum(t[0] for t in trees), []
    if live >= lim["max_live_agents"]:
        why.append(f"{live} live agents across profiles (max {lim['max_live_agents']})")
    pct, before = None, set(state.get("runaway") or ())
    over = set()
    if r:
        free, total = r
        cap, pct = memory.parse_size(lim["max_agents_memory"], total), round(100 * used / total)
        if free < memory.parse_size(lim["min_free_memory"], total):
            why.append(f"only {memory.gb(free)} free (minimum {memory.gb(memory.parse_size(lim['min_free_memory'], total))})")
        if used > 0.8 * cap:
            why.append(f"agents use {memory.gb(used)}, over 80% of the {memory.gb(cap)} cap")
        if used > cap:
            over = before   # the episode lasts while agents stay over the cap
            dispatchers = {os.getpid(), *(p["pid"] for p in profs if p["pid"])}
            mem, name, win, pane_pid, tree = max(trees, key=lambda t: t[0])   # the cause; never another tree
            harness = [x for x in tree if procs[x][0] == pane_pid]
            victims = {x: procs[x][2] for x in memory._below(procs, harness)}
            below = sum(procs[x][1] for x in victims)
            # a tree holding a dispatcher or a manager is never signalled; the harness alone has nothing to stop
            safe = dispatchers & set(tree) or any("pl dispatch" in procs[x][2] or "pl manager" in procs[x][2] for x in tree)
            kill = lim["kill_runaway"] is not False and below > 0 and not safe
            if kill:
                memory._stop(harness, victims)
            if f"{name}:{win}" not in before:   # one notification per window per episode
                _notify(f"Runaway agent: {name} {win}", f"profile {name} window {win}: {memory.gb(mem)}, "
                        f"{memory.gb(below)} of it below the harness; all agents {memory.gb(used)}, machine cap "
                        f"{memory.gb(cap)}" + (f"; stopped {len(victims)} child processes" if kill else
                                               "; nothing stopped"), _alerts(profs))
            over = before | {f"{name}:{win}"}
    state["runaway"] = over
    hold = "; ".join(why) or None
    if hold and not state.get("hold"):
        print(f"hold: {hold}", flush=True)
        if _clock() - state.get("hold_notified", float("-inf")) >= HOLD_NOTIFY_EVERY:
            state["hold_notified"] = _clock()
            _notify("pl manager holds new agents", hold, _alerts(profs))
    state["hold"] = hold
    return live, lim["max_live_agents"], pct, hold


def _asked(kind, name):
    """`pl manager restart|stop NAME` (or the console's D key) left a request file: take it."""
    f = _path(kind) / name
    if not f.exists():
        return False
    f.unlink(missing_ok=True)
    return True


def tick(state):
    """One pass: start each managed dispatcher that does not run (with backoff after an exit), write status.json."""
    from pl.dispatch import start_for
    now, rows, profs = _clock(), [], managed()
    bad, profs = [p for p in profs if "error" in p], [p for p in profs if "error" not in p]
    machine_error = _machine_check(state)
    live, cap, pct, hold = _caps(state, profs)
    for p in profs:
        fresh = {"up": False, "seen": False, "since": 0, "restarts": [], "next": 0, "gave_up": False, "stopped": False}
        r = state.setdefault(p["name"], fresh)
        if _asked("restart", p["name"]):
            r.update(fresh)
        if _asked("stop", p["name"]):
            r["stopped"] = r["stopping"] = True
        r["restarts"] = [t for t in r["restarts"] if now - t < 3600]
        if r.get("stopped"):   # stopped by you: Ctrl-C until it exits, then leave the profile alone
            r["stopping"] = r.get("stopping") and p["running"]
            if r["stopping"]:
                _run(["tmux", "send-keys", "-t", f"={p['session']}:dispatch", "C-c"], timeout=10)
            r["up"] = False
        elif p["running"]:
            r["up"] = r["seen"] = True
        elif r["up"] and not r["seen"] and now - r["since"] < LOCK_GRACE:
            pass   # started, has not taken its lock yet: not an exit
        elif r["up"]:   # it ran (or was started and never took its lock) and is gone: an exit
            r["up"] = False
            if len(r["restarts"]) >= MAX_RESTARTS:
                r["gave_up"] = True
                print(f"{p['name']}: dispatcher exited {len(r['restarts']) + 1} times in an hour; not restarting", flush=True)
                _notify("pl manager gave up", f"the dispatcher of profile {p['name']} keeps exiting; pl manager stopped "
                        f"restarting it (see its tmux window dispatch; pl manager restart {p['name']})", _alerts([p]))
            else:
                r["next"] = now + BACKOFF[min(len(r["restarts"]), len(BACKOFF) - 1)]
        if not p["running"] and not r["up"] and not r["gave_up"] and not r.get("stopped") and now >= r["next"]:
            err = start_for(p["dir"], p["session"], p["gh"], managed=True)
            print(f"{p['name']}: " + (f"dispatcher failed to start — {err}" if err else "dispatcher started"), flush=True)
            if r["next"]:
                r["restarts"].append(now)
            r["up"], r["seen"], r["since"], r["next"] = True, False, now, 0
        rows.append({"name": p["name"], "running": p["running"], "dispatcher_pid": p["pid"] if p["running"] else None,
                     "restarts": len(r["restarts"]), "gave_up": r["gave_up"], "stopped": bool(r.get("stopped"))})
    rows += [{"name": p["name"], "running": False, "dispatcher_pid": None, "restarts": 0, "gave_up": False,
              "error": p["error"]} for p in bad]
    status = {"at": now, "pid": os.getpid(), "profiles": rows, "live_agents": live, "max_live_agents": cap,
              "room": max(0, cap - live), "agent_memory_pct": pct, "hold": hold, "machine_error": machine_error}
    _write("status.json", status)
    return status


def restart(name, kind="restart"):
    """Ask the manager to start a profile's dispatcher again on the next tick (clearing a give-up or a stop), or,
    kind "stop", to stop it and not restart it until `pl manager restart NAME`."""
    from pl import config as C
    if not C.NAME_RE.match(name or ""):
        return None
    d = _path(kind)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (_path("stop" if kind == "restart" else "restart") / name).unlink(missing_ok=True)   # the last request wins
    (d / name).touch()
    return (f"manager: {'restarting' if kind == 'restart' else 'stopping'} the dispatcher of {name} on the next tick"
            + ("" if running() else " (manager not running)"))


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
    from pl.util import now_iso
    f.write(f"pid {os.getpid()} since {now_iso()}")
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
            try:
                tick(state)
            except Exception as e:  # noqa: BLE001 - one bad tick never stops the manager
                print(f"tick failed: {type(e).__name__}: {e}", flush=True)
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
    write_machine_toml()
    with open(_path("manager.log"), "a") as log:
        subprocess.Popen([sys.executable, "-m", "pl", "manager", "run"], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True)
    if not _wait(True):
        return f"manager: failed to start — no lock after {WAIT:g} s; see {_path('manager.log')}"
    return f"manager: started (pid {holder_pid() or '?'})"


def stop(all_=False):
    """SIGTERM the manager and wait for its lock to free. all_: also stop each managed dispatcher."""
    note, pid = "manager: not running", holder_pid()
    if running():   # the lock's pid is a live pl manager: a reused pid is never signalled
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        note = "manager: stopped" if _wait(False) else f"manager: still running (pid {pid})"
    if all_:
        from pl import config as C
        from pl import dispatch
        for p in (p for p in managed() if "error" not in p):
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
        state = (p.get("error") or "gave up" if p.get("error") or p["gave_up"] else "stopped by you" if p.get("stopped")
                 else "running" if p["running"] else "stopped")
        lines.append(f"{p['name']:<16}{state:<12}{str(p['dispatcher_pid'] or '-'):<8}{p['restarts']}")
    if st.get("hold"):
        lines.append(f"hold: {st['hold']}")
    if st.get("machine_error"):
        lines.append(st["machine_error"])
    return "\n".join(lines)


def cmd_manager(argv):
    ap = argparse.ArgumentParser(prog="pl manager", description="one manager per machine: it runs the dispatcher of "
                                 "every profile whose [dispatch] autostart is not false")
    ap.add_argument("action", choices=["start", "stop", "status", "run", "restart"])
    ap.add_argument("profile", nargs="?", help="restart: start this profile's dispatcher again; stop: stop only "
                    "this profile's dispatcher, not restarted until restart")
    ap.add_argument("--all", action="store_true", help="stop: also stop every managed dispatcher")
    a = ap.parse_args(argv)
    if a.action == "run":
        return _run_loop()
    if a.action == "restart" or a.action == "stop" and a.profile:
        note = restart(a.profile, a.action)
        if note is None:
            print(f"pl manager {a.action}: give a profile name", file=sys.stderr)
            return 2
        print(note)
        return 0
    print({"start": start, "status": show}.get(a.action, lambda: stop(a.all))())
    return 0
