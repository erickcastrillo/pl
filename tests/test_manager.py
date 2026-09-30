"""pl manager: one per machine, owns every managed profile's dispatcher, writes status.json.
tmux is a fake subprocess.run that logs argv; locks are real flock files under a temp HOME."""
import argparse
import fcntl
import json
import subprocess

import pytest

from pl import config as C
from pl import dispatch, manager

REAL_NOTIFY = manager._notify   # the fixture replaces it; the timeout test needs the real one


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    for var in ("PL_CONFIG_DIR", "PL_MANAGED", "GH_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    for name, body in (("work", ""), ("home", ""), ("off", "[dispatch]\nautostart = false\n")):
        d = tmp_path / f".pl-{name}"
        (d / "state").mkdir(parents=True)
        (d / "config.toml").write_text(body)
    log = []

    def run(argv, **k):
        log.append(list(argv))
        return subprocess.CompletedProcess(argv, 1 if argv[:2] == ["tmux", "has-session"] else 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    notes, alive = [], set()
    monkeypatch.setattr(manager, "_notify", lambda title, msg, cmds=(): notes.append((title, msg)))
    monkeypatch.setattr(manager, "_alive", lambda pid, what, since=None: pid in alive)   # no real kill(pid, 0) or ps
    yield {"home": tmp_path, "log": log, "notes": notes, "alive": alive}
    for k, v in saved.items():
        setattr(C, k, v)


def hold(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def starts(log):
    return [c for c in log if c[:2] in (["tmux", "new-session"], ["tmux", "new-window"])]


def test_a_second_run_exits_0_and_starts_nothing(machine, capsys):
    lock = manager.machine_dir() / "manager.lock"
    f = hold(lock)
    f.write("pid 777 on h since now")
    f.flush()
    try:
        assert manager.cmd_manager(["run"]) == 0
    finally:
        f.close()
    assert "a manager is already running on this machine (pid 777)" in capsys.readouterr().out
    assert starts(machine["log"]) == []


def running_dispatcher(machine, name, pid):
    (machine["home"] / f".pl-{name}" / "state" / "pl-dispatch.lock").write_text(f"pid {pid} on h since now")
    machine["alive"].add(pid)


def test_a_tick_starts_one_managed_dispatcher_per_free_profile(machine):
    running_dispatcher(machine, "home", 4321)   # home's dispatcher already runs
    manager.tick({})
    status = json.loads((manager.machine_dir() / "status.json").read_text())
    got = starts(machine["log"])
    assert got == [["tmux", "new-session", "-d", "-s", "pl-work", "-n", "dispatch",
                    "-e", f"PL_CONFIG_DIR={machine['home'] / '.pl-work'}", "-e", "PL_MANAGED=1",
                    "--", *got[0][-4:-3], "-m", "pl", "dispatch"]]
    assert {p["name"]: p["running"] for p in status["profiles"]} == {"home": True, "work": False}   # off: not managed
    assert status["hold"] is None and status["pid"] > 0


def test_pl_dispatch_on_a_managed_machine_exits_before_taking_the_profile_lock(machine, monkeypatch, capsys):
    """The tmux-restore regression: a restored `pl dispatch` window closes; only the manager's own one runs."""
    C.load(config_dir=str(machine["home"] / ".pl-work"))
    passes = []
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **k: passes.append(1))
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: None)
    args = argparse.Namespace(once=True, dry_run=False, no_pull=True, max_runs=None, max_prep=None, interval=None)
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 777 on h since now")
    f.flush()
    machine["alive"].add(777)
    try:
        manager.tick({})   # the manager's status lists work as managed
        assert dispatch.cmd_dispatch(args) is None
        assert "this machine is run by pl manager (pid 777)" in capsys.readouterr().out
        assert passes == [] and not C.LOCK_FILE.exists()
        monkeypatch.setenv("PL_MANAGED", "1")   # the manager's own start goes ahead
        dispatch.cmd_dispatch(args)
        assert passes == [1] and C.LOCK_FILE.exists()
    finally:
        f.close()


def test_a_dispatcher_that_dies_six_times_in_an_hour_is_given_up(machine, monkeypatch):
    for name in ("home",):
        (machine["home"] / f".pl-{name}" / "config.toml").write_text("[dispatch]\nautostart = false\n")
    now = [0.0]
    monkeypatch.setattr(manager, "_clock", lambda: now[0])
    state, at = {}, []
    while now[0] <= 1000:
        before = len(starts(machine["log"]))
        manager.tick(state)
        if len(starts(machine["log"])) > before:
            at.append(now[0])
        now[0] += manager.TICK
    # it never takes its lock: each start counts as an exit after the 30 s grace, then backoff 10 s, 30 s, 2 min, 5 min
    assert at == [0, 40, 100, 250, 580, 910]
    status = json.loads((manager.machine_dir() / "status.json").read_text())
    assert status["profiles"] == [{"name": "work", "running": False, "dispatcher_pid": None, "restarts": 5,
                                   "gave_up": True, "stopped": False}]
    assert len(machine["notes"]) == 1 and "work" in machine["notes"][0][1]


# ---------- WP2: machine-wide caps ----------

GB = 1024 ** 3
VM_STAT = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: {p}.\nPages inactive: 0.\nPages speculative: 0.\n"


def fake_machine(monkeypatch, panes, ps, free_gb=20, total_gb=32):
    """One vm_stat, one ps, one tmux list-panes -a; every other command answers nothing."""
    from pl import memory
    out = {"vm_stat": VM_STAT.format(p=int(free_gb * GB / 16384)), "sysctl": f"{total_gb * GB}\n", "ps": ps, "tmux": panes}
    monkeypatch.setattr(memory, "PLATFORM", "darwin")
    monkeypatch.setattr(memory, "_run", lambda argv: out.get(argv[0]))
    monkeypatch.setattr(memory, "_sleep", lambda s: None)
    memory._CACHE.clear()
    kills = []
    monkeypatch.setattr(memory, "_kill", lambda pid, sig: kills.append((pid, sig)))
    return kills


def limits(machine, text):
    manager.machine_dir().mkdir(parents=True, exist_ok=True)
    (manager.machine_dir() / "machine.toml").write_text(text)


def test_ten_agents_across_two_profiles_past_a_cap_of_eight_set_the_hold_and_notify_once(machine, monkeypatch):
    limits(machine, "[limits]\nmax_live_agents = 8\n")
    panes = "".join(f"pl-{p} @{p}{i} {1000 * (p == 'home') + 2000 + 10 * i} run-{p}-{i}\n" for p in ("work", "home") for i in range(5))
    fake_machine(monkeypatch, panes + "pl-work @d 900 dispatch\nother @x 950 run-not-ours\n", "1 0 5000 /sbin/launchd\n")
    state = {}
    manager.tick(state)
    manager.tick(state)
    status = json.loads((manager.machine_dir() / "status.json").read_text())
    assert status["hold"] and "10 live agents" in status["hold"] and "max 8" in status["hold"]
    assert status["live_agents"] == 10
    assert [t for t, _ in machine["notes"]] == ["pl manager holds new agents"]
    assert any("max_live_agents" in e for e in C.validate_machine({"limits": {"max_live_agents": 0}}))
    assert any("max_agents_memory" in e for e in C.validate_machine({"limits": {"max_agents_memory": "lots"}}))


PANES2 = "pl-work @1 100 run-a\npl-home @2 200 run-b\npl-work @3 300 dispatch\npl-home @4 400 dispatch\n"
PS2 = "\n".join(["1 0 5000 /sbin/launchd",
                 "100 1 3000 -zsh", "101 100 400000 claude --session-id a", "102 101 7000000 ruby bin/rspec",
                 "200 1 3000 -zsh", "201 200 400000 claude --session-id b", "202 201 7500000 node vite build",
                 "300 1 3000 -zsh", "301 300 90000 python -m pl dispatch",
                 "400 1 3000 -zsh", "401 400 90000 python -m pl dispatch"]) + "\n"
SAFE2 = {1, 100, 101, 200, 201, 300, 301, 400, 401}


def test_two_windows_at_24_percent_in_two_profiles_stop_the_larger_tree_only(machine, monkeypatch):
    """The 2026-09-30 regression: each window passes its own profile's 25 % check, together they take half the machine."""
    import signal
    limits(machine, '[limits]\nmax_agents_memory = "40%"\n')
    kills = fake_machine(monkeypatch, PANES2, PS2)
    manager.tick({})
    assert {pid for pid, sig in kills if sig == signal.SIGTERM} == {202}
    assert not {pid for pid, _ in kills} & SAFE2
    assert len(machine["notes"]) >= 1 and any("home" in m and "run-b" in m for _, m in machine["notes"])


def test_kill_runaway_false_notifies_and_sends_no_signal(machine, monkeypatch):
    limits(machine, '[limits]\nmax_agents_memory = "40%"\nkill_runaway = false\n')
    kills = fake_machine(monkeypatch, PANES2, PS2)
    state = {}
    manager.tick(state)
    manager.tick(state)
    assert kills == []
    assert len([m for _, m in machine["notes"] if "run-b" in m]) == 1   # once per episode


# ---------- fix round 1 ----------

def runs(n, profile="work", start=2000):
    return "".join(f"pl-{profile} @{profile}{i} {start + 10 * i} run-{profile}-{i}\n" for i in range(n))


def test_loop_windows_do_not_count_as_live_agents(machine, monkeypatch):
    (machine["home"] / ".pl-work" / "config.toml").write_text('[loops.merge]\nprompt = "/loop 5m /m"\n'
                                                              '[loops.review]\nprompt = "/loop 5m /r"\n')
    limits(machine, "[limits]\nmax_live_agents = 8\n")
    fake_machine(monkeypatch, runs(6) + "pl-work @m 800 merge\npl-work @r 810 review\npl-home @h 820 run-home-0\n",
                 "1 0 5000 /sbin/launchd\n")
    st = manager.tick({})
    assert st["live_agents"] == 7 and st["hold"] is None and st["room"] == 1


def test_the_live_cap_holds_at_exactly_max(machine, monkeypatch):
    limits(machine, "[limits]\nmax_live_agents = 8\n")
    fake_machine(monkeypatch, runs(7), "1 0 5000 /sbin/launchd\n")
    assert manager.tick({})["hold"] is None
    fake_machine(monkeypatch, runs(8), "1 0 5000 /sbin/launchd\n")
    st = manager.tick({})
    assert st["hold"] and "8 live agents" in st["hold"] and st["room"] == 0


def _tree(kb):
    return f"1 0 5000 /sbin/launchd\n100 1 3000 -zsh\n101 100 1000 claude\n102 101 {kb} ruby bin/rspec\n"


def test_agent_memory_over_80_percent_of_the_cap_holds_without_a_kill(machine, monkeypatch):
    limits(machine, '[limits]\nmax_agents_memory = "40%"\nmin_free_memory = "1%"\n')   # cap 12.8 GB, 80 % = 10.24 GB
    kills = fake_machine(monkeypatch, "pl-work @1 100 run-a\n", _tree(10_000_000))       # ~9.5 GB
    assert manager.tick({})["hold"] is None
    kills = fake_machine(monkeypatch, "pl-work @1 100 run-a\n", _tree(11_000_000))       # ~10.5 GB
    st = manager.tick({})
    assert st["hold"] and "over 80%" in st["hold"] and kills == []


def test_a_dispatcher_inside_an_agent_tree_is_never_signalled(machine, monkeypatch):
    limits(machine, '[limits]\nmax_agents_memory = "40%"\n')
    running_dispatcher(machine, "work", 102)   # the dispatcher pid sits under the runaway harness
    kills = fake_machine(monkeypatch, "pl-work @1 100 run-a\n", _tree(14_000_000) + "103 101 500000 node x\n")
    manager.tick({})
    assert kills == []   # memory._stop re-walks the tree for its SIGKILL pass: the dispatcher would be in it


def test_a_runaway_notifies_once_and_a_bare_harness_over_the_cap_is_left_alone(machine, monkeypatch):
    limits(machine, '[limits]\nmax_agents_memory = "40%"\n')
    state = {}
    kills = fake_machine(monkeypatch, PANES2, PS2)
    manager.tick(state)
    manager.tick(state)   # the same episode: no second notification
    assert len([m for t, m in machine["notes"] if t.startswith("Runaway")]) == 1
    big_harness = "1 0 5000 /sbin/launchd\n200 1 3000 -zsh\n201 200 14000000 claude --session-id b\n"
    kills = fake_machine(monkeypatch, "pl-home @2 200 run-b\n", big_harness)
    manager.tick(state)
    assert kills == [] and len([m for t, m in machine["notes"] if t.startswith("Runaway")]) == 1


def test_notify_has_a_timeout_and_never_raises(monkeypatch):
    seen = []

    def run(argv, timeout=None):
        seen.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(manager, "_run", run)
    REAL_NOTIFY("t", "m", ["notify-me"])
    assert seen == [10]


def test_an_exception_in_a_tick_is_logged_and_the_loop_goes_on(machine, monkeypatch, capsys):
    n = []

    def tick(state):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("boom")

    class Done(Exception):
        pass
    monkeypatch.setattr(manager, "tick", tick)
    monkeypatch.setattr(manager, "_sleep", lambda s: len(n) >= 2 and (_ for _ in ()).throw(Done()))
    monkeypatch.setattr(manager.signal, "signal", lambda *a: None)
    with pytest.raises(Done):
        manager._run_loop()
    assert len(n) == 2 and "tick failed: RuntimeError: boom" in capsys.readouterr().out


@pytest.mark.parametrize("at", [1e12, "soon", None, [1]])
def test_a_status_from_the_future_or_with_a_bad_time_is_absent(machine, at):
    manager.machine_dir().mkdir(parents=True)
    (manager.machine_dir() / "status.json").write_text(json.dumps({"at": at, "pid": 1, "profiles": []}))
    assert manager.read_status() is None


def test_stop_signals_only_a_pid_whose_command_is_pl_manager(machine, monkeypatch):
    import os
    import signal
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 4242 since now")
    f.flush()
    sent, cmd = [], ["01:00 python -m pl dispatch"]
    monkeypatch.setattr(os, "kill", lambda pid, sig: sig and sent.append((pid, sig)))
    monkeypatch.setattr(manager, "_run", lambda argv, timeout=None: subprocess.CompletedProcess(argv, 0, cmd[0], ""))
    monkeypatch.setattr(manager, "_alive", REAL_ALIVE)
    monkeypatch.setattr(manager, "WAIT", 0.2)
    try:
        assert manager.stop() == "manager: not running" and sent == []   # a reused pid is not the manager
        cmd[0] = "01:00 /usr/bin/python3 -m pl manager run\n"
        manager.stop()
        assert sent == [(4242, signal.SIGTERM)]
    finally:
        f.close()


def test_a_tick_never_takes_a_profile_lock(machine, monkeypatch):
    """A probe that briefly takes the lock makes a dispatcher starting at that moment exit "already running"."""
    import fcntl as real
    taken = []
    running_dispatcher(machine, "home", 4321)   # the lock file exists
    monkeypatch.setattr(real, "flock", lambda f, op: taken.append(getattr(f, "name", f)))
    manager.tick({})
    assert not [t for t in taken if str(t).endswith("pl-dispatch.lock")]


def test_a_started_dispatcher_gets_30_s_to_take_its_lock(machine, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(manager, "_clock", lambda: now[0])
    (machine["home"] / ".pl-home" / "config.toml").write_text("[dispatch]\nautostart = false\n")
    state = {}
    while now[0] <= 60:
        if now[0] == 25:
            running_dispatcher(machine, "work", 5555)   # slow to start under memory pressure: lock at 25 s
        manager.tick(state)
        now[0] += manager.TICK
    assert len(starts(machine["log"])) == 1 and state["work"]["restarts"] == []


def test_pl_dispatch_runs_for_a_profile_the_manager_does_not_manage(machine, monkeypatch):
    C.load(config_dir=str(machine["home"] / ".pl-off"))
    passes = []
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **k: passes.append(1))
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: None)
    args = argparse.Namespace(once=True, dry_run=False, no_pull=True, max_runs=None, max_prep=None, interval=None)
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 777 since now")
    f.flush()
    machine["alive"].add(777)
    try:
        manager.tick({})   # status.json lists work and home, not off
        dispatch.cmd_dispatch(args)
    finally:
        f.close()
    assert passes == [1]


def test_restart_clears_a_give_up(machine, monkeypatch):
    (machine["home"] / ".pl-home" / "config.toml").write_text("[dispatch]\nautostart = false\n")
    state = {"work": {"up": False, "seen": False, "since": 0, "restarts": [0] * 5, "next": 0, "gave_up": True}}
    manager.tick(state)
    assert starts(machine["log"]) == []
    assert manager.cmd_manager(["restart", "work"]) == 0
    st = manager.tick(state)
    assert len(starts(machine["log"])) == 1 and st["profiles"][0]["gave_up"] is False
    assert manager.cmd_manager(["restart", "../x"]) == 2   # a name is a profile name, never a path


def test_start_detaches_the_manager_in_its_own_session(machine, monkeypatch):
    got = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: got.append((argv, k)))
    monkeypatch.setattr(manager, "_wait", lambda held: True)
    note = manager.start()
    argv, k = got[0]
    assert argv[1:] == ["-m", "pl", "manager", "run"] and k["start_new_session"] is True
    assert k["stdin"] is subprocess.DEVNULL and note.startswith("manager: started")
    assert (manager.machine_dir() / "machine.toml").read_text() == manager.MACHINE_TOML


def test_start_is_a_no_op_while_a_manager_runs(machine, monkeypatch):
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 31 since now")
    f.flush()
    machine["alive"].add(31)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started a second manager"))
    try:
        assert manager.start() == "manager: running (pid 31)"
    finally:
        f.close()


def test_show_prints_each_profile_and_the_hold(machine):
    assert manager.show() == "manager: not running (never started on this machine)"
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 32 since now")
    f.flush()
    machine["alive"].add(32)
    try:
        manager.tick({"work": {"up": False, "seen": False, "since": 0, "restarts": [0] * 5, "next": 0, "gave_up": True}})
        out = manager.show()
    finally:
        f.close()
    assert "manager: running" in out and "work" in out and "gave up" in out and "home" in out


# ---------- fix round 2 ----------

REAL_ALIVE = manager._alive


def test_a_big_harness_over_the_cap_never_stops_an_innocent_tree(machine, monkeypatch):
    """run-x's harness holds 9 GB, run-y's pytest 1 GB, cap 9.6 GB: run-x is the cause and has nothing to stop."""
    limits(machine, '[limits]\nmax_agents_memory = "60%"\n')
    ps = ("1 0 5000 /sbin/launchd\n100 1 3000 -zsh\n101 100 9437184 claude --session-id x\n"
          "200 1 3000 -zsh\n201 200 50000 claude --session-id y\n202 201 1048576 python -m pytest\n")
    kills = fake_machine(monkeypatch, "pl-work @1 100 run-x\npl-home @2 200 run-y\n", ps, free_gb=5, total_gb=16)
    state = {}
    manager.tick(state)
    manager.tick(state)
    assert kills == []
    runaway = [m for t, m in machine["notes"] if t.startswith("Runaway")]
    assert len(runaway) == 1 and "run-x" in runaway[0]


def test_a_bare_harness_over_the_cap_is_never_stopped_and_notifies_once(machine, monkeypatch):
    from pl import memory
    limits(machine, '[limits]\nmax_agents_memory = "40%"\n')
    fake_machine(monkeypatch, "pl-home @2 200 run-b\n", "1 0 5000 /sbin/launchd\n200 1 3000 -zsh\n"
                 "201 200 14000000 claude --session-id b\n")
    stops = []
    monkeypatch.setattr(memory, "_stop", lambda harness, victims: stops.append(harness))
    state = {}
    manager.tick(state)
    manager.tick(state)
    assert stops == [] and len([m for t, m in machine["notes"] if t.startswith("Runaway")]) == 1


@pytest.mark.parametrize("cmd", ["python -m pl dispatch --once", "python -m pl manager run"])
def test_a_tree_running_pl_dispatch_or_pl_manager_is_never_signalled(machine, monkeypatch, cmd):
    limits(machine, '[limits]\nmax_agents_memory = "40%"\n')
    kills = fake_machine(monkeypatch, "pl-work @1 100 run-a\n", _tree(14_000_000) + f"103 101 500000 {cmd}\n")
    manager.tick({})
    assert kills == []


def test_a_run_agent_waiting_at_a_gate_does_not_count_as_live(machine, monkeypatch):
    from pl import harnesses
    limits(machine, "[limits]\nmax_live_agents = 8\n")
    fake_machine(monkeypatch, runs(8), "1 0 5000 /sbin/launchd\n")
    monkeypatch.setattr(harnesses, "_run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, "GATE: before-push\n" if "@work3" in argv and "capture-pane" in argv else "", ""))
    st = manager.tick({})
    assert st["live_agents"] == 7 and st["hold"] is None


def test_checking_whether_the_manager_runs_never_takes_its_lock(machine, monkeypatch):
    lock = manager.machine_dir() / "manager.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pid 55 since now")
    taken = []
    monkeypatch.setattr(fcntl, "flock", lambda f, op: taken.append(f))
    machine["alive"].add(55)
    assert manager.running() is True
    machine["alive"].clear()
    assert manager.running() is False and taken == []


def test_alive_needs_the_command_and_a_start_before_the_lock_was_written(monkeypatch):
    import os
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    out = ["  01:40 python -m pl dispatch\n"]
    monkeypatch.setattr(manager, "_run", lambda argv, timeout=None: subprocess.CompletedProcess(argv, 0, out[0], ""))
    monkeypatch.setattr(manager, "_clock", lambda: 10_000.0)
    since = 10_000.0 - 60   # the lock was written 60 s ago
    assert REAL_ALIVE(4, "pl dispatch", since) is True    # started 100 s ago
    assert REAL_ALIVE(4, "pl manager", since) is False    # another program has the pid
    out[0] = "  00:30 python -m pl dispatch\n"              # started after the lock was written: a reused pid
    assert REAL_ALIVE(4, "pl dispatch", since) is False
    assert REAL_ALIVE(4, "pl dispatch") is True            # no time in the lock: the command alone decides
    out[0] = "1-02:00:00 python -m pl dispatch\n"           # days-hours form
    assert REAL_ALIVE(4, "pl dispatch", since) is True


def test_a_tick_checks_the_dispatcher_pid_against_the_lock_time(machine, monkeypatch):
    from pl.util import parse_iso
    (machine["home"] / ".pl-work" / "state" / "pl-dispatch.lock").write_text("pid 9 on h since 2026-09-30T10:00:00+00:00")
    seen = []
    monkeypatch.setattr(manager, "_alive", lambda pid, what, since=None: seen.append((pid, what, since)) or True)
    manager.tick({})
    assert (9, "pl dispatch", parse_iso("2026-09-30T10:00:00+00:00")) in seen


def test_manages_skips_a_profile_the_manager_gave_up_on(machine):
    f = hold(manager.machine_dir() / "manager.lock")
    f.write("pid 777 since now")
    f.flush()
    machine["alive"].add(777)
    try:
        manager.tick({"work": {"up": False, "seen": False, "since": 0, "restarts": [0] * 5, "next": 0, "gave_up": True}})
        assert manager.manages("home") and not manager.manages("work")   # a hand `pl dispatch` runs for work
    finally:
        f.close()


def test_stop_name_stops_that_dispatcher_until_it_is_restarted(machine, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(manager, "_clock", lambda: now[0])
    (machine["home"] / ".pl-home" / "config.toml").write_text("[dispatch]\nautostart = false\n")
    running_dispatcher(machine, "work", 5555)
    state = {}
    manager.tick(state)
    assert manager.cmd_manager(["stop", "work"]) == 0
    manager.tick(state)
    assert ["tmux", "send-keys", "-t", "=pl-work:dispatch", "C-c"] in machine["log"]
    machine["alive"].discard(5555)   # it exited
    while now[0] < 1000:
        now[0] += manager.TICK
        st = manager.tick(state)
    assert starts(machine["log"]) == [] and st["profiles"][0]["stopped"] is True
    monkeypatch.setattr(manager, "running", lambda: True)
    assert "stopped by you" in manager.show()
    assert manager.cmd_manager(["stop", "../x"]) == 2
    assert manager.cmd_manager(["restart", "work"]) == 0
    st = manager.tick(state)
    assert len(starts(machine["log"])) == 1 and st["profiles"][0]["stopped"] is False


def test_a_profile_whose_config_does_not_load_is_marked_and_skipped(machine):
    (machine["home"] / ".pl-work" / "config.toml").write_text("dispatch = 1\n")   # loads, wrong shape
    (machine["home"] / ".pl-home" / "config.toml").write_text("[not toml\n")
    (machine["home"] / ".pl-good" / "state").mkdir(parents=True)
    (machine["home"] / ".pl-good" / "config.toml").write_text("")
    st = manager.tick({})
    rows = {p["name"]: p for p in st["profiles"]}
    assert rows["work"]["error"] == rows["home"]["error"] == "config error"
    assert [c[4] for c in starts(machine["log"])] == ["pl-good"]
