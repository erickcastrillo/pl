"""Antigravity (agy) agents: its quota screen parks the account until "Resets in 4h9m46s", and a finished agy prep
agent left at its idle prompt frees its slot and its window closes. agy has no session registry and redraws its
screen every second, so tmux's window_activity never ages: idle is read from an unchanged screen."""
import subprocess
import time
import types
from datetime import datetime, timedelta

import pytest

from pl import accounts, dispatch, harnesses, move_agent
from pl import config as C

AGY_QUOTA = """─────────────────────────── Conversation compacted ────────────────────────────

⚠ Individual quota reached. Please upgrade your subscription to increase your
limits. Resets in 4h9m46s.
Error ID: ca9c6a10-4d84-48ee-9553-91b2c006dea0-732

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
? for shortcuts                                          Gemini 3.8 Flash · high
"""
AGY_IDLE = """  • Key Decisions Recorded:
      • Reversals restricted to root, admin, accountant.

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
? for shortcuts                                          Gemini 3.8 Flash · high
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
    C.PROFILES = {"agy": tmp_path / "agy"}
    C.ACCOUNTS = {"agy": {"harness": "antigravity"}}
    C.STAGES = {}
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}", "plan": "/pl-plan {id}"}
    monkeypatch.setattr(accounts, "lists", lambda: {})
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


# ---------- A: the quota screen and its reset time ----------

def test_the_agy_quota_screen_is_its_limit_and_not_claudes(monkeypatch):
    monkeypatch.setattr(accounts, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=AGY_QUOTA))
    assert accounts.screen_hit_limit("%1", harnesses.get("antigravity")) == AGY_QUOTA
    assert accounts.screen_hit_limit("%1", harnesses.get("claude")) is None
    assert harnesses.limit_hit(harnesses.get("antigravity"), "⚠ Individual\nquota reached. Please upgrade")   # wrapped
    assert harnesses.limit_hit(harnesses.get("antigravity"), AGY_IDLE) is None


@pytest.mark.parametrize("text, delta", [
    (AGY_QUOTA, timedelta(hours=4, minutes=9, seconds=46)),
    ("Individual quota reached. Resets in 9m.", timedelta(minutes=9)),
    ("Individual quota reached. Resets in 2h", timedelta(hours=2)),
    ("quota reached, resets in 1d 3h", timedelta(days=1, hours=3)),
], ids=["agy-screen", "9m", "2h", "1d-3h"])
def test_a_relative_reset_parks_until_now_plus_that_time(text, delta):
    now = datetime(2026, 10, 8, 10, 0)
    assert accounts.reset_at(text, now) == now + delta


def test_mark_exhausted_parks_an_agy_account_for_the_printed_duration_else_one_hour():
    until = datetime.fromisoformat(accounts.mark_exhausted("agy", AGY_QUOTA)).timestamp()
    assert abs(until - (time.time() + 4 * 3600 + 9 * 60 + 46 + 60)) < 5
    until = datetime.fromisoformat(accounts.mark_exhausted("agy", "⚠ Individual quota reached.")).timestamp()
    assert abs(until - (time.time() + C.LIMIT_COOLDOWN)) < 5


# ---------- A: the dispatcher parks once, frees the slot, restarts after the reset ----------

def _card(n, col="In progress", **meta):
    return {"id": f"o/r#{n}", "title": f"Card {n}", "list_id": col, "updated_at": f"2026-10-0{n}",
            "metadata": {"pipeline_mode": "auto", "profile": "agy", **meta}}


def _run_worker(**extra):
    return {"stage": "run", "profile": "agy", "harness": "antigravity", "pane": "%1", "window": "@1",
            "session_id": "s", "started_at": "2026-10-07T22:41:40+00:00", **extra}


def _pass(monkeypatch, cs, parked, marks, kills, max_runs=1, st_screen=AGY_QUOTA, waiting=None):
    started = []

    def update(cid, **f):
        c = next(x for x in cs if x["id"] == cid)
        c["metadata"] = {k: v for k, v in {**c["metadata"], **f.get("metadata", {})}.items() if v is not None}
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", update)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append(c["id"]))
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: st_screen if h and h.name == "antigravity" else None)
    monkeypatch.setattr(dispatch, "mark_exhausted", lambda prof, screen: marks.append(prof) or parked.add(prof)
                        or "2026-10-08T14:10:00+00:00")
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: kills.append(a) or "")
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "run_waiting", waiting or (lambda w, reg, st=None: None))
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: None)
    for name in ("trust_wait", "permission_wait"):
        monkeypatch.setattr(dispatch, name, lambda *a: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "locked", lambda cid: False)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set(parked))
    monkeypatch.setattr(accounts, "check_parked", lambda: None)
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(max_runs, False, pull=False)
    return started


def test_an_agy_run_agent_on_its_quota_parks_once_frees_its_slot_and_restarts_after_the_reset(monkeypatch, capsys):
    c = _card(1, worker=_run_worker())
    parked, marks, kills = set(), [], []
    _pass(monkeypatch, [c], parked, marks, kills)
    assert marks == ["agy"]
    assert c["metadata"]["worker"]["limit_hit"] == {"account": "agy", "until": "2026-10-08T14:10:00+00:00"}
    assert "run agent waiting (usage limit); its slot is free" in capsys.readouterr().out
    assert not [k for k in kills if k[0] == "kill-window"]
    _pass(monkeypatch, [c], parked, marks, kills)              # same screen next pass: "Resets in" is not read again
    assert marks == ["agy"] and c["metadata"]["worker"]["limit_hit"]["until"] == "2026-10-08T14:10:00+00:00"
    parked.clear()                                             # the reset came: agy does not resume by itself
    _pass(monkeypatch, [c], parked, marks, kills)
    assert ("kill-window", "-t", "@1") in kills and not c["metadata"].get("worker")
    assert marks == ["agy"] and c["metadata"]["profile_switches"][-1]["reason"] == "limit reset"


def test_a_run_agent_on_a_limit_screen_does_not_hold_a_run_slot(monkeypatch):
    C.PROFILES = {**C.PROFILES, "claude": C.PROFILES["agy"].parent / ".claude"}
    C.ACCOUNTS = {**C.ACCOUNTS, "claude": {"harness": "claude"}}
    monkeypatch.setattr(move_agent, "move", lambda *a, **k: (False, "different harness", False))
    limited = _card(1, worker=_run_worker(limit_hit={"account": "agy", "until": "2026-10-08T14:10:00+00:00"}))
    fresh = _card(2, col="Approved", profile="claude")
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, *a, **k: want)   # no other account for card 1
    started = _pass(monkeypatch, [limited, fresh], {"agy"}, [], [])
    assert started == ["o/r#2"]   # max_runs 1: the limited agent's slot is free


# ---------- B: a finished agy prep agent frees its slot and its window closes ----------

def _screens(monkeypatch, panes, now):
    """tmux faked through subprocess.run: list-panes lists the windows, capture-pane shows each pane's text."""
    calls = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        if "list-panes" in argv:
            out = "".join(f"@{i} %{i} {now} {name} {cmd}\n" for i, (name, cmd, _) in panes.items())
        elif "capture-pane" in argv:
            out = panes[int(argv[argv.index("-t") + 1].lstrip("%"))][2]
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(["tmux", *a]))
    return calls


def test_an_untracked_agy_plan_window_idle_at_its_prompt_frees_its_slot_and_closes(monkeypatch):
    """pl approve clears the card's worker; agy keeps its window busy-looking (window_activity is always now)."""
    now = int(time.time())
    panes = {36: ("plan-w3-payments-parity", "agy", AGY_IDLE), 37: ("plan-w3-assessment", "agy", AGY_IDLE),
             33: ("run-w4-auth", "agy", AGY_QUOTA)}
    calls = _screens(monkeypatch, panes, now)
    st = {}
    assert dispatch.sweep_untracked([], {}, False, st) == 2          # first sight: it may still be working
    for rec in st["screens"].values():
        rec["since"] -= 601                                          # the same screen for over 10 minutes
    panes[37] = ("plan-w3-assessment", "agy", AGY_IDLE + "working…")  # this one changed: still working
    assert dispatch.sweep_untracked([], {}, False, st) == 1
    kills = [c for c in calls if c[:2] == ["tmux", "kill-window"]]
    assert kills == [["tmux", "kill-window", "-t", "@36"]]           # never the run window


def test_a_finished_agy_plan_worker_is_closed_once_its_screen_stays_the_same(monkeypatch):
    w = {"stage": "plan", "profile": "agy", "harness": "antigravity", "pane": "%36", "window": "@36",
         "session_id": "s", "started_at": "2026-10-07T22:00:00+00:00"}
    c = _card(11, col="Plan for review", worker=w)
    kills = []
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=AGY_IDLE))
    _pass(monkeypatch, [c], set(), [], kills, st_screen=None)
    assert c["metadata"].get("worker") and not [k for k in kills if k[0] == "kill-window"]
    from pl.util import load_state, save_state
    st = load_state()
    st["screens"]["%36"]["since"] -= 301
    save_state(st)
    _pass(monkeypatch, [c], set(), [], kills, st_screen=None)
    assert ("kill-window", "-t", "@36") in kills and "worker" not in c["metadata"]


# ---------- C: an idle agy run agent frees its run slot ----------

def _run_screen(monkeypatch, text):
    """The run agent's pane: its screen text, and a window_activity of now (agy redraws every second)."""
    from pl import agents
    monkeypatch.setattr(agents, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout=str(int(time.time())) if "display-message" in argv else text))


def test_an_agy_run_agent_is_waiting_once_its_screen_stays_the_same_for_ten_minutes(monkeypatch):
    from pl import agents
    _run_screen(monkeypatch, AGY_IDLE)
    st, w = {}, _run_worker()
    assert agents.run_waiting(w, {}, st) is None                      # first sight: it may be working
    st["screens"]["%1"]["since"] -= agents.WAIT_IDLE - 30
    assert agents.run_waiting(w, {}, st) is None                      # 9.5 minutes: not yet
    st["screens"]["%1"]["since"] -= 60
    assert agents.run_waiting(w, {}, st) == "waiting (idle 10 min)"
    _run_screen(monkeypatch, AGY_IDLE + "working…")
    assert agents.run_waiting(w, {}, st) is None                      # the screen changed: working again
    assert agents.run_waiting(w, {}) is None                          # no dispatcher state: window_activity only


def test_a_claude_run_agent_still_reads_its_window_activity(monkeypatch):
    from pl import agents
    _run_screen(monkeypatch, "working on WP2")
    w = {**_run_worker(), "harness": "claude", "profile": "claude"}
    st = {"screens": {"%1": {"hash": "x", "since": 0}}}
    assert agents.run_waiting(w, {"s": {"status": "idle"}}, st) is None   # its window is active now
    assert st == {"screens": {"%1": {"hash": "x", "since": 0}}}          # and its screen is not hashed


def test_an_idle_agy_run_agent_frees_its_run_slot_for_the_next_card(monkeypatch, capsys):
    from pl import agents
    from pl.util import load_state, save_state
    C.DISPATCH = {**C.DISPATCH, "release_waiting_after": 0}           # waits but is never stopped
    _run_screen(monkeypatch, AGY_IDLE)
    idle, new = _card(1, worker=_run_worker()), _card(2, col="Approved")
    started = _pass(monkeypatch, [idle, new], set(), [], [], st_screen=None, waiting=agents.run_waiting)
    assert started == []                                              # first sight: it holds the only slot
    st = load_state()
    st["screens"]["%1"]["since"] -= agents.WAIT_IDLE + 1
    save_state(st)
    kills = []
    started = _pass(monkeypatch, [idle, new], set(), [], kills, st_screen=None, waiting=agents.run_waiting)
    assert started == ["o/r#2"] and "run agent waiting (idle 10 min); its slot is free" in capsys.readouterr().out
    assert not [k for k in kills if k[0] == "kill-window"] and idle["metadata"]["worker"]   # its window stays
