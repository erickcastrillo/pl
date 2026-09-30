"""WP44: the memory safety net. Low memory holds new agent starts; a runaway agent process tree is reported."""
import json
import signal

import pytest
import tomlkit

from pl import config as C
from pl import dispatch, memory
from pl.util import load_state

GB = 1024 ** 3
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               {free}.
Pages active:                             900000.
Pages inactive:                           {inactive}.
Pages speculative:                        {spec}.
Pages wired down:                         100000.
"""


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def mac(monkeypatch, free_gb, total_gb=32, extra=None):
    """Fake a Mac: vm_stat reports free_gb split across free/inactive/speculative pages; extra answers other argv."""
    page = 16384
    pages = int(free_gb * GB / page)
    out = {("vm_stat",): VM_STAT.format(free=pages // 2, inactive=pages - pages // 2 - pages // 4, spec=pages // 4),
           ("sysctl", "-n", "hw.memsize"): f"{total_gb * GB}\n"}
    calls = []

    def run(argv):
        calls.append(tuple(argv))
        if tuple(argv) in out:
            return out[tuple(argv)]
        return (extra or {}).get(argv[0])
    monkeypatch.setattr(memory, "PLATFORM", "darwin")
    monkeypatch.setattr(memory, "_run", run)
    memory._CACHE.clear()
    return calls


def test_mac_reading_adds_free_inactive_speculative_pages_and_is_cached(monkeypatch):
    calls = mac(monkeypatch, 6)
    free, total = memory.reading()
    assert abs(free - 6 * GB) < 3 * 16384 and total == 32 * GB
    memory.reading()
    assert calls.count(("vm_stat",)) == 1          # cached for a few seconds
    monkeypatch.setattr(memory, "_clock", lambda: 1e12)
    memory.reading()
    assert calls.count(("vm_stat",)) == 2


def test_linux_reading_uses_mem_available(monkeypatch):
    monkeypatch.setattr(memory, "PLATFORM", "linux")
    monkeypatch.setattr(memory, "_meminfo", lambda: "MemTotal:       16000000 kB\nMemFree: 1 kB\nMemAvailable:    4000000 kB\n")
    assert memory.reading() == (4000000 * 1024, 16000000 * 1024)


def test_unreadable_memory_never_blocks(monkeypatch):
    monkeypatch.setattr(memory, "PLATFORM", "darwin")
    assert memory.reading() is None
    assert memory.status() is None
    assert memory.check_starts({"notified": {}}) is None


def test_sizes_parse_from_percent_units_and_numbers():
    assert memory.parse_size("15%", 100 * GB) == 15 * GB
    assert memory.parse_size("4GB", 0) == 4 * GB
    assert memory.parse_size("512 MB", 0) == 512 * 1024 ** 2
    assert memory.parse_size("2g", 0) == 2 * GB
    assert memory.parse_size(1000, 0) == 1000
    for bad in ("lots", "-1GB", True, "15"):
        with pytest.raises(ValueError):
            memory.parse_size(bad, GB)


def test_defaults_and_config_values(fake_home):
    assert C.DISPATCH["min_free_memory"] == "15%" and C.DISPATCH["max_agent_memory"] == "25%"
    assert C.DISPATCH["max_agent_processes"] == 150 and C.DISPATCH["kill_runaway"] is True   # WP45: kill by default
    (fake_home / ".pl-t" / "config.toml").write_text(
        '[dispatch]\nmin_free_memory = "4GB"\nmax_agent_processes = 90\nkill_runaway = false\n')
    C.load("t")
    assert C.DISPATCH["min_free_memory"] == "4GB" and C.DISPATCH["max_agent_processes"] == 90
    assert C.DISPATCH["kill_runaway"] is False and C.DISPATCH["max_agent_memory"] == "25%"   # the opt-out
    errs = C.validate(tomlkit.parse('[dispatch]\nmin_free_memory = "lots"\nmax_agent_processes = 0\nkill_runaway = "yes"\n'))
    assert any("min_free_memory" in e for e in errs)
    assert any("max_agent_processes" in e for e in errs)
    assert any("kill_runaway" in e for e in errs)
    assert C.validate(tomlkit.parse('[dispatch]\nmin_free_memory = "4GB"\nmax_agent_memory = "30%"\n')) == []


def _funnel(monkeypatch):
    """One auto Inbox card and one loop; records agent starts, loop windows, notifications and events."""
    c = {"id": "card0001aaaa", "title": "A card", "list_id": "L", "updated_at": "2026-09-30",
         "metadata": {"pipeline_mode": "auto", "profile": "acme"}}
    got = {"starts": [], "notes": [], "windows": []}
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [c])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Inbox")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: got["starts"].append(c["id"]))
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cs: "acme")
    for name in ("sweep_untracked", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(memory, "notify", lambda title, msg: got["notes"].append((title, msg)))
    monkeypatch.setattr(dispatch, "notify", lambda title, msg: got["notes"].append((title, msg)))
    monkeypatch.setattr(dispatch.subprocess, "run", lambda argv, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: got["windows"].append(a) or {"new-window": "@7", "list-panes": "%9"}.get(a[0], ""))
    C.SERVICES = {"merge-check": {"prompt": "/loop 30m /merge-check", "profile": "acme"}}
    return got


def _events():
    p = C.STATE_DIR / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_low_memory_holds_every_start_and_notifies_once_per_episode(monkeypatch, capsys):
    got = _funnel(monkeypatch)
    mac(monkeypatch, 2)                              # 2 GB free of 32 GB: under 15 %
    dispatch.dispatch_once(1, False, pull=False)
    dispatch.dispatch_once(1, False, pull=False)
    assert got["starts"] == [] and not any(a[0] == "new-window" for a in got["windows"])
    assert "starts paused: only 2.0 GB free" in capsys.readouterr().out
    assert len(got["notes"]) == 1 and "2.0 GB" in got["notes"][0][1]
    assert [e["kind"] for e in _events()] == ["memory_low"]
    mac(monkeypatch, 12)                             # recovered: starts go ahead
    dispatch.dispatch_once(1, False, pull=False)
    assert got["starts"] == ["card0001aaaa"] and not load_state().get("memory_low")
    mac(monkeypatch, 1)                              # a new episode notifies again
    dispatch.dispatch_once(1, False, pull=False)
    assert len(got["notes"]) == 2 and [e["kind"] for e in _events()] == ["memory_low", "memory_low"]


def test_an_absolute_minimum_from_config(monkeypatch):
    got = _funnel(monkeypatch)
    C.DISPATCH = {**C.DISPATCH, "min_free_memory": "8GB"}
    mac(monkeypatch, 6)                              # 6 GB is over 15 % of 32 GB but under 8 GB
    dispatch.dispatch_once(1, False, pull=False)
    assert got["starts"] == []


# ---------- runaway guard ----------

PANES = "@1 100 run-add-a-thing\n@2 200 spec-other\n@3 300 dispatch\n"


def _ps(n_vite, rss_kb=1000):
    """Pane 100: zsh → claude (harness) → rspec → n_vite ruby copies. Pane 200: a small tree. Pid 1: launchd."""
    rows = ["1 0 5000 /sbin/launchd", "100 1 3000 -zsh", "101 100 400000 claude --session-id s /loop 5m /run-plan x",
            "102 101 90000 ruby bin/rspec", "200 1 3000 -zsh", "201 200 400000 claude --session-id t", "300 1 3000 -zsh"]
    rows += [f"{1000 + i} {102 if i == 0 else 999 + i} {rss_kb} /usr/bin/ruby /gems/vite_ruby-3.9/bin/vite build"
             for i in range(n_vite)]
    return "\n".join(rows) + "\n"


def _guard(monkeypatch, n_vite, rss_kb=1000, after_term=None):
    """after_term: the ps output once the first SIGTERM went out (None: every process stays)."""
    got = {"notes": [], "kills": [], "sleeps": []}
    C.TMUX_SESSION = "pl-t"
    ps = _ps(n_vite, rss_kb)
    mac(monkeypatch, 20, extra={"tmux": PANES})
    run = memory._run

    def run_ps(argv):
        if argv[0] == "ps":
            return after_term if after_term is not None and got["kills"] else ps
        return run(argv)
    monkeypatch.setattr(memory, "_run", run_ps)
    monkeypatch.setattr(memory, "notify", lambda title, msg: got["notes"].append((title, msg)))
    monkeypatch.setattr(memory, "_kill", lambda pid, sig: got["kills"].append((pid, sig)))
    monkeypatch.setattr(memory, "_sleep", lambda s: got["sleeps"].append(s))
    cs = [{"id": "card0001aaaa", "title": "Add a thing", "metadata": {"worker": {"window": "@1", "stage": "run"}}}]
    return got, cs


def test_a_fat_tree_notifies_with_the_card_window_and_top_command_once(monkeypatch):
    got, cs = _guard(monkeypatch, 200)
    C.DISPATCH = {**C.DISPATCH, "kill_runaway": False}   # the opt-out: notify only
    st = {"notified": {}}
    memory.guard_runaways(st, cs)
    memory.guard_runaways(st, cs)
    assert len(got["notes"]) == 1
    title, msg = got["notes"][0]
    assert "Add a thing" in title
    assert "card0001" in msg and "run-add-a-thing" in msg and "ruby … bin/vite build ×200" in msg
    ev = [e for e in _events() if e["kind"] == "runaway"]
    assert len(ev) == 1 and ev[0]["card"] == "card0001aaaa" and ev[0]["window"] == "run-add-a-thing"
    assert got["kills"] == []                        # kill_runaway = false: notify only


def test_a_small_tree_is_left_alone_and_memory_over_the_cap_counts(monkeypatch):
    got, cs = _guard(monkeypatch, 20)
    memory.guard_runaways({"notified": {}}, cs)
    assert got["notes"] == []
    got, cs = _guard(monkeypatch, 20, rss_kb=500 * 1024)   # 20 × 500 MB = 10 GB > 25 % of 32 GB
    memory.guard_runaways({"notified": {}}, cs)
    assert len(got["notes"]) == 1 and "GB" in got["notes"][0][1]


SAFE = {1, 100, 101, 200, 201, 300}   # launchd, pane shells, harnesses, the dispatcher's pane


def test_kill_by_default_terms_what_the_harness_started_then_kills_what_is_left(monkeypatch):
    got, cs = _guard(monkeypatch, 200)
    memory.guard_runaways({"notified": {}}, cs)
    victims = {102, *range(1000, 1200)}
    term = [pid for pid, sig in got["kills"] if sig == signal.SIGTERM]
    kill = [pid for pid, sig in got["kills"] if sig == signal.SIGKILL]
    assert set(term) == victims and set(kill) == victims            # nothing left after the grace: SIGKILL
    assert got["kills"].index((1199, signal.SIGKILL)) > got["kills"].index((1199, signal.SIGTERM))
    assert not {pid for pid, _ in got["kills"]} & SAFE
    assert sum(got["sleeps"]) == memory.KILL_GRACE                  # waited 5 s before SIGKILL
    assert "stopped 201 child processes" in got["notes"][0][1]
    ev = [e for e in _events() if e["kind"] == "runaway"][0]
    assert ev["card"] == "card0001aaaa" and ev["window"] == "run-add-a-thing" and ev["command"] == "ruby"
    assert ev["stopped"] == 201


def test_what_exits_on_sigterm_gets_no_sigkill(monkeypatch):
    rest = "\n".join(line for line in _ps(0).splitlines() if not line.startswith("102 ")) + "\n"
    got, cs = _guard(monkeypatch, 200, after_term=rest)
    memory.guard_runaways({"notified": {}}, cs)
    assert got["kills"] and all(sig == signal.SIGTERM for _, sig in got["kills"])
    assert sum(got["sleeps"]) < memory.KILL_GRACE


def test_a_dry_run_never_signals(monkeypatch):
    got, cs = _guard(monkeypatch, 200)
    memory.guard_runaways({"notified": {}}, cs, dry=True)
    assert got["kills"] == [] and len(got["notes"]) == 1


def test_the_fast_check_runs_in_every_slice_of_the_wait(monkeypatch):
    seen, slept = [], []
    monkeypatch.setattr(dispatch.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(dispatch, "_marks", lambda: ("same",))
    dispatch._nap(20, lambda: seen.append(len(slept)))
    assert slept == [5, 5, 5, 5] and seen == [1, 2, 3, 4]


def test_the_dispatcher_checks_between_passes_with_the_last_cards(monkeypatch):
    import argparse
    import types
    seen, passes = [], []
    cs = [{"id": "card0001aaaa", "metadata": {}}]

    def once(*a, **k):
        passes.append(1)
        dispatch._LAST["cards"] = cs
        return None

    def sleep(_):
        if len(seen) >= 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatch, "dispatch_once", once)
    monkeypatch.setattr(dispatch, "reload_config", lambda: None)
    monkeypatch.setattr(dispatch, "time", types.SimpleNamespace(time=dispatch.time.time, monotonic=dispatch.time.monotonic, sleep=sleep))
    monkeypatch.setattr(memory, "guard_runaways", lambda st, c, dry=False: seen.append([x["id"] for x in c]))
    with pytest.raises(KeyboardInterrupt):
        dispatch.cmd_dispatch(argparse.Namespace(once=False, dry_run=True, no_pull=True, max_runs=1, max_prep=1, interval=20))
    assert passes == [1] and seen == [["card0001aaaa"]] * 3   # three 5 s slices of one 20 s wait, the fourth sleep stops it


def test_the_guard_runs_in_every_dispatcher_pass(monkeypatch):
    seen = []
    _funnel(monkeypatch)
    monkeypatch.setattr(memory, "guard_runaways", lambda st, cs, dry=False: seen.append([c["id"] for c in cs]))
    dispatch.dispatch_once(1, False, pull=False)
    assert seen == [["card0001aaaa"]]


# ---------- WP2: the machine hold written by pl manager ----------

def _machine_status(tmp_path, monkeypatch, age):
    import time
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    (tmp_path / "machine").mkdir()
    (tmp_path / "machine" / "status.json").write_text(json.dumps(
        {"at": time.time() - age, "pid": 1, "profiles": [], "hold": "10 live agents across profiles (max 8)"}))


def test_a_fresh_machine_hold_starts_nothing(fake_home, monkeypatch, capsys):
    got = _funnel(monkeypatch)
    _machine_status(fake_home, monkeypatch, age=2)
    dispatch.dispatch_once(1, False, pull=False)
    assert got["starts"] == []
    assert "10 live agents" in capsys.readouterr().out


def test_a_machine_hold_older_than_15_s_is_ignored(fake_home, monkeypatch):
    got = _funnel(monkeypatch)
    _machine_status(fake_home, monkeypatch, age=20)
    dispatch.dispatch_once(1, False, pull=False)
    assert got["starts"] == ["card0001aaaa"]


def _two_cards(monkeypatch, got):
    """A new Inbox card and one whose spec agent crashed once (a restart, attempt 2)."""
    new = {"id": "card0001aaaa", "title": "New", "list_id": "L", "updated_at": "2026-09-30",
           "metadata": {"pipeline_mode": "auto", "profile": "acme"}}
    crashed = {"id": "card0002bbbb", "title": "Crashed", "list_id": "L", "updated_at": "2026-09-29",
               "metadata": {"pipeline_mode": "auto", "profile": "acme",
                            "worker": {"stage": "spec", "attempts": 1, "window": "spec-card0002", "session_id": "s2"}}}
    monkeypatch.setattr(dispatch, "cards", lambda: [new, crashed])
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("dead", w.get("session_id")) if w else ("none", None))
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: got["starts"].append((c["id"], attempts)))


def _status(tmp_path, monkeypatch, **fields):
    import time
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    (tmp_path / "machine").mkdir(exist_ok=True)
    (tmp_path / "machine" / "status.json").write_text(json.dumps({"at": time.time() - 2, "pid": 1, "profiles": [], **fields}))


def test_a_machine_hold_blocks_new_starts_only_never_restarts_or_loops(fake_home, monkeypatch):
    got = _funnel(monkeypatch)
    _two_cards(monkeypatch, got)
    _status(fake_home, monkeypatch, hold="8 live agents across profiles (max 8)", room=0)
    dispatch.dispatch_once(2, False, pull=False)
    assert got["starts"] == [("card0002bbbb", 2)]                          # the crashed spec agent is restarted
    assert any(a[0] == "new-window" and "merge-check" in a for a in got["windows"])   # the loop is (re)started


def test_room_in_the_machine_status_caps_new_starts_per_pass(fake_home, monkeypatch):
    got = _funnel(monkeypatch)
    _two_cards(monkeypatch, got)
    two = [{"id": f"card000{i}new0", "title": "New", "list_id": "L", "updated_at": "2026-09-30",
            "metadata": {"pipeline_mode": "auto", "profile": "acme"}} for i in (1, 2)]
    monkeypatch.setattr(dispatch, "cards", lambda: two)   # both cards are new
    _status(fake_home, monkeypatch, hold=None, room=1)
    dispatch.dispatch_once(2, False, pull=False)
    assert len(got["starts"]) == 1
