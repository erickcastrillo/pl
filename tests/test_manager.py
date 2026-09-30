"""pl manager: one per machine, owns every managed profile's dispatcher, writes status.json.
tmux is a fake subprocess.run that logs argv; locks are real flock files under a temp HOME."""
import argparse
import fcntl
import json
import subprocess

import pytest

from pl import config as C
from pl import dispatch, manager


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
    notes = []
    monkeypatch.setattr(manager, "_notify", lambda title, msg, cmds=(): notes.append((title, msg)))
    yield {"home": tmp_path, "log": log, "notes": notes}
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


def test_a_tick_starts_one_managed_dispatcher_per_free_profile(machine):
    held = hold(machine["home"] / ".pl-home" / "state" / "pl-dispatch.lock")   # home's dispatcher already runs
    try:
        manager.tick({})
        status = json.loads((manager.machine_dir() / "status.json").read_text())
    finally:
        held.close()
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
    try:
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
    assert at == [0, 15, 50, 175, 480, 785]   # backoff 10 s, 30 s, 2 min, 5 min after each exit
    status = json.loads((manager.machine_dir() / "status.json").read_text())
    assert status["profiles"] == [{"name": "work", "running": False, "dispatcher_pid": None, "restarts": 5,
                                   "gave_up": True}]
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
