"""A card sent back for rework whose run agent is still alive: the dispatcher types the review message into it
once it is idle, and restarts it fresh when it does not start."""
import time
import types
from datetime import datetime, timezone

import pytest

from pl import agents, dispatch, events, harnesses, move_agent
from pl import config as C
from pl.util import save_state

RUN_W = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "attempts": 1}
PRS = ["https://github.com/o/r/pull/7"]


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
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(worker=None, **rework):
    rw = {"active": True, "round": 1, "prs": PRS, **rework}
    return {"id": "aaaa0001", "title": "card", "list_id": "In progress", "tags": [], "updated_at": "2026-09-30",
            "description": "# PIPELINE: PLAN\nWP1",
            "metadata": {"pipeline_mode": "auto", "profile": "acme", "rework": rw, **({"worker": worker} if worker else {})}}


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def fake(monkeypatch):
    ns = types.SimpleNamespace(started=[], writes=[], stops=[], events=[], tmux=[], screen="", waiting="waiting (finished turn)",
                               reg={"s1": {"status": "idle"}}, ask=None, trust=None, locked=False)

    def tmux(*a, **k):
        ns.tmux.append(a)
        return ns.screen if a[0] == "capture-pane" else ""
    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: ns.ask)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: ns.trust)
    monkeypatch.setattr(dispatch, "registry", lambda: ns.reg)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: ns.waiting)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: None)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: ns.started.append((c["id"], attempts)))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: ns.writes.append((cid, f)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: not ns.locked)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(move_agent, "stop", lambda pane, sid: ns.stops.append((pane, sid)) or True)
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))

    def run(cs):
        monkeypatch.setattr(dispatch, "cards", lambda: cs)
        dispatch.dispatch_once(1, False, pull=False)
    ns.run = run
    ns.typed = lambda: [a for a in ns.tmux if a[0] == "send-keys" and "-l" in a]
    ns.kinds = lambda: [k for k, _, _ in ns.events]
    return ns


def _rework_writes(ns):
    return [f["metadata"]["rework"] for _, f in ns.writes if "rework" in (f.get("metadata") or {})]


def test_an_idle_live_agent_gets_the_rework_message_once(fake):
    c = _card(dict(RUN_W))
    fake.run([c])
    fake.run([c])
    assert len(fake.typed()) == 1
    text = fake.typed()[0][-1]
    assert "round 1" in text and "gh api repos/o/r/issues/7/comments --jq '.[-1].body'" in text
    assert _rework_writes(fake)[0]["sent_round"] == 1
    assert [d for k, _, d in fake.events if k == "rework_sent"] == [{"round": 1, "cleared": False}]


def test_a_new_round_gets_its_own_message(fake):
    fake.run([_card(dict(RUN_W), round=2, sent_round=1, started=True)])
    assert len(fake.typed()) == 1 and "round 2" in fake.typed()[0][-1]


def test_a_busy_agent_gets_nothing_until_it_is_idle(fake):
    c = _card(dict(RUN_W))
    fake.waiting = None
    fake.run([c])
    assert fake.typed() == []
    fake.waiting = "waiting (finished turn)"
    fake.run([c])
    assert len(fake.typed()) == 1


def test_an_agent_that_does_not_start_within_stall_minutes_is_restarted_with_no_backoff(fake):
    fake.run([_card(dict(RUN_W), sent_round=1, sent_at=_iso(time.time() - 21 * 60))])
    assert fake.stops == [("%1", "s1")]
    assert fake.started == [("aaaa0001", 1)]
    assert not any("run_retry_at" in (f.get("metadata") or {}) for _, f in fake.writes)
    assert "rework_restarted" in fake.kinds() and "run_released" not in fake.kinds()
    assert any(r.get("started") is True for r in _rework_writes(fake))


def test_an_agent_that_went_busy_is_not_restarted(fake):
    fake.reg = {"s1": {"status": "busy"}}
    fake.waiting = None
    fake.run([_card(dict(RUN_W), sent_round=1, sent_at=_iso(time.time() - 21 * 60))])
    assert fake.stops == [] and fake.started == []
    assert _rework_writes(fake)[-1]["started"] is True


def test_a_card_with_no_live_agent_starts_fresh_and_logs_no_rework_sent(fake):
    fake.run([_card()])
    assert fake.started == [("aaaa0001", 1)] and "rework_sent" not in fake.kinds()


def test_unsent_text_in_the_prompt_box_is_cleared_first(fake):
    fake.screen = "│ > half typed │"
    fake.run([_card(dict(RUN_W))])
    sends = [a for a in fake.tmux if a[0] == "send-keys"]
    assert sends[0][-1] == "C-u" and "-l" in sends[1] and sends[2][-1] == "Enter"
    assert [d["cleared"] for k, _, d in fake.events if k == "rework_sent"] == [True]


def test_an_empty_prompt_box_is_still_cleared_but_not_reported(fake):
    fake.screen = "│ > │"
    fake.run([_card(dict(RUN_W))])
    assert [a for a in fake.tmux if a[0] == "send-keys"][0][-1] == "C-u"
    assert [d["cleared"] for k, _, d in fake.events if k == "rework_sent"] == [False]


@pytest.mark.parametrize("how", ["ask", "trust", "locked"])
def test_an_agent_waiting_on_a_person_or_a_move_gets_no_message(fake, monkeypatch, how):
    if how == "locked":
        monkeypatch.setattr(move_agent, "locked", lambda cid: True)
    else:
        setattr(fake, how, "Allow this?")
        fake.reg = {}   # a registered session is past the trust prompt
    fake.run([_card(dict(RUN_W))])
    assert fake.typed() == [] and "rework_sent" not in fake.kinds()


def test_an_agent_on_a_usage_limit_screen_gets_no_message(fake):
    c = _card({**RUN_W, "limit_hit": {"account": "acme"}})
    assert dispatch.rework_live(c, c["metadata"]["worker"], "waiting (usage limit)", fake.reg, False) is None
    assert fake.typed() == [] and "rework_sent" not in fake.kinds()


def test_control_characters_never_reach_the_pane(fake):
    fake.run([_card(dict(RUN_W), prs=["https://github.com/o/r/pull/7\n", "https://github.com/o/r\x1b[2J/pull/8"])])
    assert not harnesses.CONTROL_RE.search(fake.typed()[0][-1])


def test_a_messaged_agent_is_not_released_with_a_backoff(fake):
    save_state({"notified": {}, "run_waits": {"aaaa0001": {"since": time.time() - 31 * 60, "session": "s1"}}})
    fake.run([_card(dict(RUN_W))])
    assert len(fake.typed()) == 1 and "run_released" not in fake.kinds()
