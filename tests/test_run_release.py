"""Waiting run agents are never released; a leftover release hold is cleared."""
import subprocess
import time
import types
from datetime import datetime, timezone

import pytest

from pl import agents, dispatch, events, harnesses, move_agent
from pl import config as C
from pl.util import load_state, save_state

RUN_W = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "attempts": 1}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"acme": tmp_path / ".claude"}
    C.ACCOUNTS = {"acme": {"harness": "claude"}}
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)   # list ids are column names here: no board
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _auto(cid, col, desc="# PIPELINE: PLAN\nWP1", **meta):
    return {"id": cid, "title": f"card {cid}", "list_id": col, "tags": [], "updated_at": "2026-09-30", "description": desc,
            "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def fake(monkeypatch):
    """The dispatcher with board, tmux and the stop path faked. Returns a namespace of what happened."""
    ns = types.SimpleNamespace(started=[], writes=[], stops=[], events=[], stop_ok=True, refusal=None, waiting=None,
                               locked=[], ask=None, trust=None, api_error=None)
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: ns.ask)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: ns.trust)
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: ns.waiting)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: ns.api_error)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)   # a stopped agent's window is gone
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: ns.started.append((c["id"], attempts)))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: ns.writes.append((cid, f)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: ns.locked.append(cid) or True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: ns.locked.append(("unlock", cid)))
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: ns.refusal)
    monkeypatch.setattr(move_agent, "stop", lambda pane, sid: ns.stops.append((pane, sid)) or ns.stop_ok)
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))

    def run(cs, max_runs=1):
        monkeypatch.setattr(dispatch, "cards", lambda: cs)
        dispatch.dispatch_once(max_runs, False, pull=False)
    ns.run = run
    return ns


def _seed_wait(cid, ago, sid="s1"):
    save_state({"notified": {}, "run_waits": {cid: {"since": time.time() - ago, "session": sid}}})


def _release_writes(ns):
    """Writes other than the worker's stored GATE line (those are covered by their own tests)."""
    return [w for w in ns.writes if set((w[1].get("metadata") or {}).keys()) != {"worker"}]


def _meta_write(ns, cid):
    return next(f["metadata"] for c, f in ns.writes if c == cid and "metadata" in f)


# ---------- no release ----------

def test_a_run_agent_waiting_under_the_limit_keeps_its_window(fake):
    fake.waiting = "waiting (nothing-runnable)"
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == [] and not _release_writes(fake)
    assert "aaaa0001" in load_state()["run_waits"]   # the clock started on first sight


@pytest.mark.parametrize("waiting", ["waiting (idle 40 min)", "waiting (nothing-runnable)"])
def test_a_run_agent_waiting_past_30_minutes_is_not_stopped(fake, waiting):
    fake.waiting = waiting
    _seed_wait("aaaa0001", 31 * 60)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == [] and not _release_writes(fake)
    assert not any(k == "run_released" for k, _, _ in fake.events)
    assert "aaaa0001" in load_state()["run_waits"]   # its window is kept


def test_a_waiting_run_agent_still_starts_the_stall_clock(fake):
    fake.waiting = "waiting (idle 5 min)"
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert isinstance(load_state()["run_waits"]["aaaa0001"]["since"], float)


def test_an_agent_at_claudes_read_outside_prompt_alerts(fake, monkeypatch):
    """The real screen check on Claude Code's "Allow this read outside the working directories?" prompt."""
    from pl import alerts
    from test_harnesses import CLAUDE_READ_OUTSIDE
    monkeypatch.setattr(dispatch, "permission_wait", agents.permission_wait)

    def run(argv, **kw):   # the agent pane: the prompt, unchanged for 40 minutes
        out = str(int(time.time() - 40 * 60)) if "display-message" in argv else CLAUDE_READ_OUTSIDE
        return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(harnesses, "_run", run)
    fake.waiting = "waiting (idle 40 min)"
    _seed_wait("aaaa0001", 3 * 3600)
    fake.run([_auto("aaaa0001", "In progress", spec_slug="w4-import", worker=dict(RUN_W))])
    assert fake.stops == []
    a = alerts.get("permission_wait:aaaa0001")
    assert a and "Read(/Users/me/code/your-repo-pl-w4-bulk-import/" in a["title"]
    assert _meta_write(fake, "aaaa0001")["worker"]["permission_wait"].startswith("Read(")


# ---------- a leftover hold ----------

def _leftover(cid="aaaa0001", col="Approved"):
    c = _auto(cid, col, run_released_at=_iso(time.time() - 60), run_released_why="nothing-runnable (#1757)",
              run_retry_at=_iso(time.time() + 20 * 60), run_releases=1, run_released_on=f"{col}:0123456789abcdef")
    return c


def test_a_leftover_release_hold_does_not_hold_the_card_back(fake):
    fake.run([_leftover()])
    assert fake.started == [("aaaa0001", 1)]
    assert _meta_write(fake, "aaaa0001") == dict.fromkeys(agents.RELEASE_KEYS)


def test_a_leftover_release_hold_is_cleared_in_any_column(fake):
    fake.run([_leftover(col="PR open")])
    assert _meta_write(fake, "aaaa0001") == dict.fromkeys(agents.RELEASE_KEYS)


# ---------- what the person sees ----------

def test_worker_view_ignores_a_leftover_release_hold(monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    assert not agents.worker_view(_leftover(), {}).startswith("blocked")


def test_watch_does_not_mark_a_leftover_hold_as_needing_you(monkeypatch):
    from pl import watch
    c = _leftover()
    monkeypatch.setattr(watch, "cards", lambda: [c])
    monkeypatch.setattr(watch, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    snap = watch.watch_snapshot()
    r = next(r for r in snap["rows"] if r.get("card") and r["card"]["id"] == "aaaa0001")
    assert not r["approved"] and snap["needs"]["attention"] == 0


# ---------- config ----------

@pytest.mark.parametrize("value, ok", [(0, True), (45, True), (-1, False), ("30", False), (True, False)])
def test_release_waiting_after_is_validated(value, ok):
    import tomlkit
    doc = tomlkit.parse("")
    doc["dispatch"] = {"release_waiting_after": value}
    errs = C.validate(doc)
    assert (not any("release_waiting_after" in e for e in errs)) == ok


# ---------- an agent idle after an API error restarts fresh ----------

def test_api_error_wait_reads_the_error_line_once_the_pane_is_idle(monkeypatch):
    screen = "⏺ Writing the plan\n  ⎿  API Error: Your computer went to sleep mid-response.\n\n> \n  ? for shortcuts\n"
    for idle, status, want in ((700, "idle", "API Error: Your computer went to sleep mid-response."), (60, "idle", None),
                               (700, "busy", None)):
        def run(argv, **kw):
            out = str(int(time.time() - idle)) if "display-message" in argv else screen
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        monkeypatch.setattr(harnesses, "_run", run)
        assert agents.api_error_wait(RUN_W, {"s1": {"status": status}}) == want
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout=str(int(time.time() - 700)) if "display-message" in argv else "API Error: x\n" + "work\n" * 20, stderr=""))
    assert agents.api_error_wait(RUN_W, {}) is None   # an old error, scrolled up: the agent went on


PLAN_W = {"stage": "plan", "harness": "claude", "pane": "%2", "window": "@2", "session_id": "s1", "attempts": 2}


def test_a_stage_agent_idle_after_an_api_error_is_stopped_and_started_fresh(fake):
    fake.api_error = "API Error: Your computer went to sleep mid-response."
    c = _auto("aaaa0001", "Spec ready", spec_approved_at="2026-10-01T00:00:00+00:00", worker=dict(PLAN_W))
    fake.run([c])
    assert fake.stops == [("%2", "s1")]
    m = _meta_write(fake, "aaaa0001")
    assert m["worker"] is None and m["finished_workers"] == [PLAN_W]
    assert fake.started == [("aaaa0001", 1)]   # attempt 1: not a death
    assert [(k, c, d) for k, c, d in fake.events if k == "agent_restarted"] == \
        [("agent_restarted", "aaaa0001", {"stage": "plan", "reason": "api_error"})]


def test_api_error_restarts_are_capped_per_day_then_raise_an_alert(fake, monkeypatch):
    fake.api_error = "API Error: overloaded"
    opened = []
    monkeypatch.setattr(dispatch.alerts, "open", lambda key, *a, **k: opened.append(key) or None)
    save_state({"notified": {}, "api_restarts": {"aaaa0001": [time.time() - 3600] * 3 + [time.time() - 90000]}})
    c = _auto("aaaa0001", "Spec ready", spec_approved_at="2026-10-01T00:00:00+00:00", worker=dict(PLAN_W))
    fake.run([c])
    assert fake.stops == [] and fake.started == []
    assert "api_error:aaaa0001" in opened
    save_state({"notified": {}, "api_restarts": {"aaaa0001": [time.time() - 3600] * 2 + [time.time() - 90000]}})
    fake.run([c])
    assert fake.stops == [("%2", "s1")]
    assert len(load_state()["api_restarts"]["aaaa0001"]) == 3   # the day-old one dropped, this one added


def test_an_api_error_on_an_earlier_stage_worker_is_left_to_the_finished_cleanup(fake):
    fake.api_error = "API Error: overloaded"
    c = _auto("aaaa0001", "Approved", worker=dict(PLAN_W))   # the plan is done: the card is on run now
    fake.run([c])
    assert fake.stops == []


# ---------- an agent that exits with an error line ----------

def test_a_dead_agent_keeps_its_error_line_on_the_card_once(fake, monkeypatch):
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("dead", None))
    monkeypatch.setattr(dispatch, "death_line", lambda pane: 'Error: unexpected argument "/pl-run x"')
    c = _auto("aaaa0001", "Approved", worker=dict(RUN_W))
    fake.run([c])
    m = _meta_write(fake, "aaaa0001")
    assert m["agent_error"]["line"] == 'Error: unexpected argument "/pl-run x"' and m["agent_error"]["stage"] == "run"
    assert fake.started == [("aaaa0001", 2)]   # still a death: attempt 2
    assert [(k, d) for k, _, d in fake.events if k == "agent_died"] == [("agent_died", {"stage": "run", "attempts": 1})]
    fake.writes.clear()
    fake.run([c])   # the same line again: nothing new written
    assert not [w for w in fake.writes if "agent_error" in (w[1].get("metadata") or {})]


# ---------- the GATE wait stored for the console ----------

def test_a_run_agent_at_a_gate_stores_the_gate_line_on_its_worker(fake):
    fake.waiting = "waiting (before-push)"
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert _meta_write(fake, "aaaa0001")["worker"]["run_waiting"] == "GATE: before-push"


def test_the_stored_gate_line_is_cleared_when_the_agent_moves_on(fake):
    fake.waiting = None
    fake.run([_auto("aaaa0001", "In progress", worker={**RUN_W, "run_waiting": "GATE: before-push"})])
    assert "run_waiting" not in _meta_write(fake, "aaaa0001")["worker"]


def test_an_idle_wait_without_a_gate_stores_nothing(fake):
    fake.waiting = "waiting (idle 5 min)"
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert not _release_writes(fake)


def test_an_unchanged_gate_line_is_not_written_again(fake):
    fake.waiting = "waiting (before-push)"
    fake.run([_auto("aaaa0001", "In progress", worker={**RUN_W, "run_waiting": "GATE: before-push"})])
    assert not _release_writes(fake)
