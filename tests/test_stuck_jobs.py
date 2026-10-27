"""An agent waiting only on background jobs that cannot report is stalled; nothing is ever signalled (Plan 54 slice 9)."""
import os
import types

import pytest
import tomlkit

from pl import agent_alerts, agent_screen, agents, alerts, dispatch, stuck_jobs
from pl import config as C

HARNESS = 100
CID = "o/r#1"
W = {"stage": "plan", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1"}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail(f"os.kill{a}"))
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def tree(monkeypatch):
    """A fake process table: add(pid, ppid, minutes, cmd, out) where out is the target of fds 1 and 2."""
    procs, links = {}, {}
    monkeypatch.setattr(stuck_jobs.sys, "platform", "linux")
    monkeypatch.setattr(stuck_jobs, "_procs", lambda: procs)
    monkeypatch.setattr(stuck_jobs, "_link", lambda pid, fd: links.get((pid, fd)))

    def add(pid, ppid, minutes, cmd, out=None):
        procs[pid] = (ppid, minutes * 60, cmd)
        if out:
            links[(pid, 1)] = links[(pid, 2)] = out
    return add


def _shell_job(tree, minutes, out):
    tree(200, HARNESS, minutes, "bash -c pytest", "pipe:[1]")
    tree(201, 200, minutes, "python -m pytest tests", out)


def test_a_job_whose_stdout_was_deleted_is_orphaned(tree):
    _shell_job(tree, 30, "/tmp/x/out (deleted)")
    [j] = stuck_jobs.of(HARNESS)
    assert j["state"] == "orphaned" and j["pid"] == 200 and "pytest" in j["command"] and j["minutes"] == 30
    assert "output deleted" in stuck_jobs.describe(j)


def test_a_job_past_job_minutes_is_overrun(tree):
    _shell_job(tree, 50, "/tmp/x/out")
    [j] = stuck_jobs.of(HARNESS)
    assert j["state"] == "overrun" and "over 45 min" in stuck_jobs.describe(j)


def test_a_healthy_job_is_ok_and_not_stuck(tree):
    _shell_job(tree, 5, "/tmp/x/out")
    js = stuck_jobs.of(HARNESS)
    assert [j["state"] for j in js] == ["ok"] and stuck_jobs.stuck(js) == []


def test_a_mix_of_healthy_and_orphaned_jobs_is_not_stuck(tree):
    _shell_job(tree, 5, "/tmp/x/out")
    tree(300, HARNESS, 30, "bash -c rspec", "/tmp/y/out (deleted)")
    js = stuck_jobs.of(HARNESS)
    assert sorted(j["state"] for j in js) == ["ok", "orphaned"] and stuck_jobs.stuck(js) == []


def test_job_minutes_zero_turns_overrun_off(tree):
    C.WATCHDOG["job_minutes"] = 0
    tree(200, HARNESS, 300, "bash -c a", "/tmp/a")
    tree(300, HARNESS, 300, "bash -c b", "/tmp/b (deleted)")
    assert sorted(j["state"] for j in stuck_jobs.of(HARNESS)) == ["ok", "orphaned"]


def test_pipes_and_dev_null_are_not_jobs(tree):
    tree(200, HARNESS, 90, "node mcp-server", "pipe:[123]")
    tree(201, HARNESS, 90, "node other-mcp", "/dev/null")
    assert stuck_jobs.of(HARNESS) == []


def test_no_proc_reports_nothing(tree, monkeypatch):
    monkeypatch.setattr(stuck_jobs.sys, "platform", "darwin")
    monkeypatch.setattr(stuck_jobs, "_procs", lambda: pytest.fail("ps"))
    monkeypatch.setattr(stuck_jobs, "_link", lambda *a: pytest.fail("/proc"))
    assert stuck_jobs.of(HARNESS) == [] and stuck_jobs.of(None) == []


def test_job_minutes_must_be_a_whole_number():
    def errors(v):
        return [e for e in C.validate(tomlkit.parse(f"[watchdog]\njob_minutes = {v}\n")) if "job_minutes" in e]
    assert errors(-1) and errors('"45"') and not errors(0)


ORPHAN = {"pid": 11, "command": "python -m pytest", "minutes": 70, "state": "orphaned"}
OVERRUN = {"pid": 12, "command": "ruby -S rspec", "minutes": 90, "state": "overrun"}
OK = {"pid": 13, "command": "npm test", "minutes": 3, "state": "ok"}


@pytest.fixture
def agent(monkeypatch):
    ns = types.SimpleNamespace(now=1_000_000.0, jobs=[ORPHAN, OVERRUN], keys=[], notified=[], stops=[], idle_minutes=25,
                               status="idle", st={}, seen={})
    clock = types.SimpleNamespace(time=lambda: ns.now)
    monkeypatch.setattr(agent_alerts, "time", clock)
    monkeypatch.setattr(dispatch.time, "time", lambda: ns.now)
    monkeypatch.setattr(alerts, "_clock", lambda: ns.now)
    monkeypatch.setattr(agents, "idle_seconds", lambda w, reg, st=None: 0)
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("screen", "", None))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: ns.keys.append(a) or "")
    monkeypatch.setattr(dispatch, "stop_worker", lambda c, w: ns.stops.append(c["id"]))
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: ns.notified.append(a))
    monkeypatch.setattr(stuck_jobs, "of", lambda pid: ns.jobs if pid == HARNESS else [])

    def card(col="In progress", stage="plan"):
        return {"id": CID, "title": "card", "list_id": col, "description": "d",
                "metadata": {"pipeline_mode": "auto", "worker": {**W, "stage": stage}}}

    def reg():
        return {"s1": {"pid": HARNESS, "status": ns.status, "statusUpdatedAt": (ns.now - ns.idle_minutes * 60) * 1000}}

    def stall(stage="plan"):
        ns.seen = {}
        stopped = agent_alerts.stall(card(stage=stage), {**W, "stage": stage}, reg(), ns.st, ns.seen)
        ns.st["stalls"] = ns.seen
        return stopped

    def alert(cards=None, stage="plan"):
        dispatch.check_alerts(cards or [card(stage=stage)], set(), ns.st)
        return alerts.get(f"stalled:{CID}")

    ns.card, ns.stall, ns.alert = card, stall, alert
    ns.sent = lambda: [k for k in ns.keys if k[:1] == ("send-keys",) and "-l" in k]
    return ns


def test_agent_at_prompt_with_only_orphaned_jobs_is_alerted_with_the_jobs(agent):
    agent.stall()
    a = agent.alert()
    assert alerts._live(a)
    for part in ("python -m pytest", "ruby -S rspec", "pid 11", "pid 12", "output deleted", "over 45 min", "plan agent"):
        assert part in a["title"]
    assert len(agent.notified) == 1


def test_stuck_jobs_are_judged_by_registry_time_not_pane_activity(agent):
    agent.idle_minutes = 25
    agent.stall()   # agents.idle_seconds is faked to 0: the pane looks busy
    assert alerts._live(agent.alert())


def test_stuck_jobs_before_stall_minutes_open_nothing(agent):
    agent.idle_minutes = 5
    agent.stall()
    assert not alerts._live(agent.alert()) and not agent.sent()


def test_healthy_jobs_open_no_stalled_alert(agent):
    agent.jobs = [OK]
    agent.st["run_waits"] = {CID: {"since": agent.now - 25 * 60, "session": "s1"}}
    agent.stall(stage="run")
    assert not alerts._live(agent.alert(stage="run")) and not agent.sent()


def test_mixed_healthy_and_orphaned_jobs_open_no_alert(agent):
    agent.jobs = [OK, ORPHAN]
    agent.stall()
    assert not alerts._live(agent.alert()) and not agent.sent()


def test_nudge_names_the_jobs_and_nothing_is_signalled(agent):
    agent.stall()
    [text] = [k[-1] for k in agent.sent()]
    assert stuck_jobs.describe(ORPHAN) in text and stuck_jobs.describe(OVERRUN) in text and "foreground" in text and "time limit" in text
    agent.now += C.DISPATCH["stall_minutes"] * 60 * 2
    agent.idle_minutes += 40
    assert agent.stall() is False
    assert agent.stops == [] and len(agent.sent()) == 1


def test_alert_clears_when_jobs_stop_or_card_moves(agent):
    agent.stall()
    assert alerts._live(agent.alert())
    agent.jobs = []
    agent.stall()
    assert not alerts._live(agent.alert())
    agent.jobs = [ORPHAN]
    agent.stall()
    assert alerts._live(agent.alert())
    agent.st["stalls"] = {}
    assert not alerts._live(agent.alert(cards=[agent.card(col="Plan ready")]))


def test_busy_agent_with_orphaned_job_is_not_stalled(agent):
    agent.status = "busy"
    agent.stall()
    assert not alerts._live(agent.alert()) and not agent.sent()
