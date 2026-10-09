"""A run agent that waits too long is released: stopped, its slot and live place freed, held back with a backoff,
and shown as blocked. Not a failed attempt."""
import subprocess
import time
import types
from datetime import datetime, timezone

import pytest

from pl import agents, commands, dispatch, events, harnesses, move_agent
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
    monkeypatch.setattr(dispatch, "gate_pr", lambda pane: "#1757")
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


def _meta_write(ns, cid):
    return next(f["metadata"] for c, f in ns.writes if c == cid and "metadata" in f)


# ---------- release ----------

def test_a_run_agent_waiting_under_the_limit_keeps_its_window(fake):
    fake.waiting = "waiting (nothing-runnable)"
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == [] and fake.writes == []
    assert "aaaa0001" in load_state()["run_waits"]   # the clock started on first sight


def test_a_run_agent_waiting_past_the_limit_is_released_and_its_place_freed(fake):
    fake.waiting = "waiting (nothing-runnable)"
    cs = [_auto("aaaa0001", "In progress", worker=dict(RUN_W)), _auto("bbbb0002", "Approved")]
    _seed_wait("aaaa0001", 31 * 60)
    fake.run(cs)
    assert fake.stops == [("%1", "s1")]
    m = _meta_write(fake, "aaaa0001")
    assert m["worker"] is None and m["finished_workers"] == [RUN_W]   # the finished-window cleanup closes it
    assert m["run_released_why"] == "nothing-runnable (#1757)" and m["run_releases"] == 1
    retry = datetime.fromisoformat(m["run_retry_at"]).timestamp()
    assert 29 * 60 < retry - time.time() <= 30 * 60
    assert m["run_released_at"] and m["run_released_on"] == agents.release_key(cs[0])
    assert ("run_released", "aaaa0001") in [(k, c) for k, c, _ in fake.events]
    assert fake.started == [("bbbb0002", 1)]   # max_runs 1, cap 2: the released agent no longer counts
    assert fake.locked == ["aaaa0001", ("unlock", "aaaa0001")]
    assert "aaaa0001" not in load_state().get("run_waits", {})


def test_the_release_limit_is_a_dispatch_setting_and_zero_turns_it_off(fake):
    fake.waiting = "waiting (idle 50 min)"
    assert C.DISPATCH["release_waiting_after"] == 30
    C.DISPATCH = {**C.DISPATCH, "release_waiting_after": 0}
    _seed_wait("aaaa0001", 10 * 3600)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == []
    C.DISPATCH = {**C.DISPATCH, "release_waiting_after": 60}
    _seed_wait("aaaa0001", 45 * 60)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == []


@pytest.mark.parametrize("gate", ["before-push", "not-approved", "outside-worktree"])
def test_a_gate_that_needs_a_person_is_never_released(fake, gate):
    fake.waiting = f"waiting ({gate})"
    _seed_wait("aaaa0001", 5 * 3600)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == [] and fake.writes == []


def test_an_idle_wait_is_released_with_its_reason(fake):
    fake.waiting = "waiting (idle 40 min)"
    _seed_wait("aaaa0001", 31 * 60)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert _meta_write(fake, "aaaa0001")["run_released_why"] == "idle 40 min"   # no gate: no PR looked up


def test_a_new_session_restarts_the_wait_clock(fake):
    fake.waiting = "waiting (nothing-runnable)"
    _seed_wait("aaaa0001", 31 * 60, sid="old")
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == []


@pytest.mark.parametrize("prompt", ["ask", "trust"])
def test_an_agent_waiting_for_a_person_is_never_released(fake, prompt):
    fake.waiting = "waiting (idle 40 min)"
    setattr(fake, prompt, "Bash(rm)" if prompt == "ask" else "/x")
    sid = "s1" if prompt == "ask" else "s9"   # a registered session is past the trust prompt
    _seed_wait("aaaa0001", 3 * 3600, sid=sid)
    fake.run([_auto("aaaa0001", "In progress", worker={**RUN_W, "session_id": sid})])
    assert fake.stops == []


def test_an_agent_at_claudes_read_outside_prompt_alerts_and_is_never_released(fake, monkeypatch):
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


@pytest.mark.parametrize("refusal, ok", [("its pane %1 is not in window @1", True), (None, False)])
def test_an_agent_that_cannot_be_stopped_keeps_its_record_and_its_place(fake, refusal, ok):
    fake.waiting, fake.refusal, fake.stop_ok = "waiting (nothing-runnable)", refusal, ok
    cs = [_auto("aaaa0001", "In progress", worker=dict(RUN_W)), _auto("bbbb0002", "Approved"),
          _auto("cccc0003", "In progress", worker={**RUN_W, "session_id": "s3", "pane": "%3", "window": "@3"})]
    _seed_wait("aaaa0001", 31 * 60)
    fake.run(cs)
    assert not [w for w in fake.writes if w[0] == "aaaa0001"]
    assert fake.started == []   # two live run agents: the cap (2 x 1) is full
    assert ("unlock", "aaaa0001") in fake.locked


def test_a_card_under_a_move_lock_is_not_released(fake, monkeypatch):
    fake.waiting = "waiting (nothing-runnable)"
    monkeypatch.setattr(move_agent, "lock", lambda cid: False)
    _seed_wait("aaaa0001", 31 * 60)
    fake.run([_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    assert fake.stops == [] and fake.writes == []


def test_a_dry_run_releases_nothing(fake, monkeypatch):
    fake.waiting = "waiting (nothing-runnable)"
    monkeypatch.setattr(dispatch, "cards", lambda: [_auto("aaaa0001", "In progress", worker=dict(RUN_W))])
    _seed_wait("aaaa0001", 31 * 60)
    dispatch.dispatch_once(1, True, pull=False)
    assert fake.stops == [] and fake.writes == []


# ---------- backoff ----------

def _released(cid="aaaa0001", col="Approved", retry_in=600, n=1, desc="# PIPELINE: PLAN\nWP1"):
    c = _auto(cid, col, desc=desc, run_released_at=_iso(time.time() - 60), run_released_why="nothing-runnable (#1757)",
              run_retry_at=_iso(time.time() + retry_in), run_releases=n)
    c["metadata"]["run_released_on"] = agents.release_key(_auto(cid, col, desc=desc))
    return c


def test_a_released_card_in_backoff_gets_no_agent_and_holds_no_place(fake, capsys):
    cs = [_released(), _auto("bbbb0002", "Approved")]
    fake.run(cs)
    assert fake.started == [("bbbb0002", 1)]
    assert "aaaa0001  run blocked: nothing-runnable (#1757)" in capsys.readouterr().out
    assert fake.writes == []


def test_after_the_backoff_the_run_starts_fresh_as_attempt_one(fake):
    fake.run([_released(retry_in=-5)])
    assert fake.started == [("aaaa0001", 1)]


def test_a_second_release_backs_off_longer_up_to_the_cap(fake):
    fake.waiting = "waiting (nothing-runnable)"
    for n, minutes in ((1, 60), (2, 120), (5, 120)):
        c = _released(col="In progress", retry_in=-5, n=n)
        c["metadata"]["worker"] = dict(RUN_W)
        _seed_wait("aaaa0001", 31 * 60)
        fake.writes.clear()
        fake.run([c])
        m = _meta_write(fake, "aaaa0001")
        assert m["run_releases"] == n + 1
        assert minutes * 60 - 60 < datetime.fromisoformat(m["run_retry_at"]).timestamp() - time.time() <= minutes * 60


@pytest.mark.parametrize("change", ["description", "column"])
def test_a_changed_card_clears_the_backoff_and_starts(fake, change):
    c = _released()
    if change == "description":
        c["description"] += "\nPR #1757 merged"
    else:
        c["list_id"] = "In progress"
    fake.run([c])
    m = _meta_write(fake, "aaaa0001")
    assert all(m[k] is None for k in agents.RELEASE_KEYS)
    assert fake.started == [("aaaa0001", 1)]


def test_a_card_that_moved_on_to_pr_open_has_its_backoff_cleared(fake):
    c = _released()
    c["list_id"] = "PR open"
    fake.run([c])
    assert all(_meta_write(fake, "aaaa0001")[k] is None for k in agents.RELEASE_KEYS)


def test_pl_retry_clears_the_backoff(monkeypatch):
    c = _released()
    writes = []
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "cards", lambda: [c])
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "update", lambda cid, **f: writes.append((cid, f)))
    out = commands.retry("aaaa0001")
    assert writes == [("aaaa0001", {"metadata": dict.fromkeys(agents.RELEASE_KEYS)})]
    assert "blocked" in out[0] and "starts it now" in out[0]
    writes.clear()
    assert commands.retry("all") and writes == [("aaaa0001", {"metadata": dict.fromkeys(agents.RELEASE_KEYS)})]


# ---------- what the person sees ----------

def test_worker_view_shows_blocked_with_reason_and_retry_time(monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    c = _released()
    at = datetime.fromisoformat(c["metadata"]["run_retry_at"]).astimezone()
    want = f"blocked: nothing-runnable (#1757) · retry {at:%H:%M}"
    assert agents.run_blocked(c) == want
    assert agents.worker_view(c, {}) == want
    assert agents.run_blocked(_released(retry_in=-5)) is None
    changed = _released()
    changed["description"] = "edited"
    assert agents.run_blocked(changed) is None


def test_watch_marks_a_blocked_card_as_needing_you(monkeypatch):
    from pl import watch
    from pl.tui.cards import card_state
    from pl.tui.needs import needs_me
    c = _released()
    monkeypatch.setattr(watch, "cards", lambda: [c])
    monkeypatch.setattr(watch, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    snap = watch.watch_snapshot()
    r = next(r for r in snap["rows"] if r.get("card") and r["card"]["id"] == "aaaa0001")
    assert r["blocked"] and r["approved"].startswith("blocked: nothing-runnable (#1757) · retry ")
    assert card_state(r) == "needs" and needs_me(r)
    assert snap["needs"]["attention"] == 1


def test_gate_pr_reads_a_pr_number_near_the_gate_line(monkeypatch):
    screen = "older #12 line\n\n\n\n\n⏺ GATE: nothing-runnable\n  WP3 waits on api PR #1757 (unmerged)\n> "
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=screen))
    assert agents.gate_pr("%1") == "#1757"
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="GATE: nothing-runnable\n# Heading"))
    assert agents.gate_pr("%1") is None


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
