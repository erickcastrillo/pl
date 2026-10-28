"""A stage agent idle with its stage unfinished is reported, nudged once, then stopped and started again."""
import types

import pytest

from pl import agent_alerts, agent_screen, agents, alerts, dispatch, events, harnesses, move_agent
from pl import config as C
from pl.util import load_state, save_state

REAL_IDLE_SECONDS = agents.idle_seconds   # conftest fakes the attribute for every test
W = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "attempts": 1}
CID = "aaaa0001"
KEY = f"stalled:{CID}"


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


def _card(col="In progress", **meta):
    return {"id": CID, "title": "card", "list_id": col, "tags": [], "updated_at": "2026-10-26",
            "description": "# PIPELINE: PLAN\nWP1", "metadata": {"pipeline_mode": "auto", "profile": "acme", "worker": dict(W), **meta}}


@pytest.fixture
def fake(monkeypatch):
    ns = types.SimpleNamespace(started=[], stops=[], events=[], keys=[], notified=[], idle=21 * 60, word="", now=1_000_000.0)
    monkeypatch.setattr(agent_alerts, "time", types.SimpleNamespace(time=lambda: ns.now))
    monkeypatch.setattr(agents, "idle_seconds", lambda w, reg, st=None: ns.idle)
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("some screen", ns.word, None))
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: None)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: None)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: ns.keys.append(a) or "")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: ns.started.append((c["id"], attempts)))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: ns.notified.append(a))
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(move_agent, "stop", lambda pane, sid: ns.stops.append((pane, sid)) or True)
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))

    def run(cs):
        monkeypatch.setattr(dispatch, "cards", lambda: cs)
        dispatch.dispatch_once(1, False, pull=False)
    ns.run = run
    ns.kinds = lambda kind: [e for e in ns.events if e[0] == kind]
    ns.nudges = lambda: [k for k in ns.keys if k[:1] == ("send-keys",) and agent_alerts.NUDGE in k]
    return ns


def _open(key=KEY):
    a = alerts.get(key)
    return bool(a) and not a.get("resolved_at")


def test_idle_seconds_none_while_busy(monkeypatch):
    monkeypatch.setattr(agents, "pane_exists", lambda pane: True)
    monkeypatch.setattr(agents, "_harness", lambda w: None)
    monkeypatch.setattr(agents.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(harnesses, "_run", lambda cmd: types.SimpleNamespace(stdout="9300\n"))
    assert REAL_IDLE_SECONDS(W, {"s1": {"status": "busy"}}) is None
    assert REAL_IDLE_SECONDS(W, {"s1": {"status": "idle"}}) == 700


def test_idle_agent_is_logged_once(fake):
    fake.run([_card()])
    fake.run([_card()])
    [(_, cid, d)] = fake.kinds("stalled")
    assert cid == CID and d["stage"] == "run" and d["minutes"] == 21


def test_stalled_agent_nudged_once(fake):
    for _ in range(3):
        fake.run([_card()])
        fake.now += 60
    assert len(fake.nudges()) == 1
    assert len(fake.kinds("stall_nudged")) == 1


def test_still_idle_after_nudge_restarts_in_same_pass(fake):
    fake.run([_card()])
    fake.now += C.DISPATCH["stall_minutes"] * 60
    fake.run([_card()])
    assert fake.stops == [("%1", "s1")]
    assert fake.started == [(CID, 1)]
    assert [e[2].get("reason") for e in fake.kinds("agent_restarted")] == ["stalled"]


@pytest.mark.parametrize("how", ["question", "permission_wait", "gate", "word"])
def test_agent_waiting_on_a_person_is_not_a_stall(fake, monkeypatch, how):
    card = _card()
    if how == "question":
        card["metadata"]["question"] = True
    elif how == "permission_wait":
        monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: "Do you want to proceed?")
    elif how == "gate":
        monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("GATE: before-push", "", None))
    else:
        fake.word = "waiting for you"
    fake.run([card])
    fake.now += 3600
    fake.run([card])
    assert not _open() and not fake.nudges() and not fake.stops and not fake.kinds("stalled")


def test_third_stall_in_a_day_alerts_only(fake):
    save_state({"notified": {}, "stall_retries": {CID: [fake.now - 3600, fake.now - 60]}})
    fake.run([_card()])
    fake.now += C.DISPATCH["stall_minutes"] * 60
    fake.run([_card()])
    assert not fake.stops and not fake.started


def test_unmoved_alert_skipped_while_stalled(fake):
    alerts.open(KEY, "warn", "stalled", "fix", cards=[CID])
    card = _card()
    card["metadata"]["worker"]["finished_turn"] = True
    fake.run([card])
    assert not _open(f"agent_unmoved:{CID}")


@pytest.mark.parametrize("key", ["limit_hit", "trust_wait", "permission_wait"])
def test_a_reset_or_prompt_wait_is_excused(fake, key):
    assert agent_alerts._excused(_card(), {**W, key: "x"}, "")
    assert not agent_alerts._excused(_card(), W, "")


def test_an_agent_that_resumes_gets_a_fresh_nudge_before_a_restart(fake):
    fake.run([_card()])
    fake.idle = None   # busy again
    fake.now += 3 * 3600
    fake.run([_card()])
    fake.idle = 21 * 60
    fake.run([_card()])
    assert not fake.stops and not fake.started
    assert len(fake.nudges()) == 2 and len(fake.kinds("stalled")) == 2


def test_a_finished_agent_is_not_nudged(fake):
    fake.word = "finished"
    fake.run([_card()])
    assert not fake.nudges() and not fake.kinds("stalled")


def test_the_nudges_own_echo_does_not_cancel_the_restart(fake):
    limit = C.DISPATCH["stall_minutes"] * 60
    fake.run([_card()])
    fake.idle = 5   # the typed nudge reset the pane's activity clock
    fake.now += 5
    fake.run([_card()])
    fake.now += limit - 5
    fake.idle = limit   # quiet again since the echo
    fake.run([_card()])
    assert fake.stops == [("%1", "s1")] and len(fake.nudges()) == 1


def test_real_activity_after_the_nudge_clears_it(fake):
    limit = C.DISPATCH["stall_minutes"] * 60
    fake.run([_card()])
    fake.now += limit // 2
    fake.idle = 10   # the agent did something well after the nudge
    fake.run([_card()])
    fake.now += limit
    fake.idle = limit
    fake.run([_card()])
    assert not fake.stops and len(fake.nudges()) == 2


def _stage_card(stage="plan", col="Spec ready", desc="# PIPELINE: SPEC\nthe spec", **meta):
    c = _card(col=col, spec_approved_at="2026-10-26", **meta)
    c["description"] = desc
    c["metadata"]["worker"] = {**W, "stage": stage}
    return c


def test_a_finished_plan_agent_without_its_section_is_reported_then_restarted(fake):
    fake.word = "finished"
    fake.run([_stage_card()])
    [(_, cid, d)] = fake.kinds("stalled")
    assert cid == CID and d["stage"] == "plan" and d["section"] == "PLAN"
    assert not fake.nudges() and not fake.stops
    assert _open() and len(fake.notified) == 1
    fake.now += 60
    fake.run([_stage_card()])
    assert fake.stops == [("%1", "s1")] and fake.started == [(CID, 1)]
    assert [e[2].get("reason") for e in fake.kinds("agent_restarted")] == ["stalled"]
    assert len(load_state()["stall_retries"][CID]) == 1
    assert len(fake.kinds("stalled")) == 1


def test_finished_turn_without_its_section_counts_too(fake):
    card = _stage_card()
    card["metadata"]["worker"]["finished_turn"] = True
    fake.run([card])
    fake.now += 60
    fake.run([card])
    assert fake.stops == [("%1", "s1")] and len(fake.kinds("stalled")) == 1


def test_a_blank_section_counts_as_missing(fake):
    fake.word = "finished"
    card = _stage_card(desc="# PIPELINE: PLAN\n   \n")
    fake.run([card])
    fake.now += 60
    fake.run([card])
    assert fake.stops == [("%1", "s1")]


def test_a_finished_agent_with_its_section_is_not_stalled(fake):
    fake.word = "finished"
    card = _stage_card(desc="# PIPELINE: PLAN\nWP1")
    fake.run([card])
    fake.now += 60
    fake.run([card])
    assert not fake.kinds("stalled") and not fake.stops and not _open()


def test_a_finished_agent_under_the_limit_is_left_alone(fake):
    fake.word = "finished"
    fake.idle = 10 * 60
    fake.run([_stage_card()])
    fake.now += 60
    fake.run([_stage_card()])
    assert not fake.kinds("stalled") and not fake.stops and not _open()


@pytest.mark.parametrize("how", ["question", "permission_wait", "gate"])
def test_a_finished_agent_waiting_on_a_person_is_not_restarted(fake, monkeypatch, how):
    fake.word = "finished"
    card = _stage_card()
    if how == "question":
        card["metadata"]["question"] = True
    elif how == "permission_wait":
        monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: "Proceed?")
    else:
        monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("GATE: before-push", "finished", None))
    fake.run([card])
    fake.now += 60
    fake.run([card])
    assert not fake.kinds("stalled") and not fake.stops and not _open()


def test_restarts_of_a_missing_section_stop_at_stall_retries(fake):
    fake.word = "finished"
    save_state({"notified": {}, "stall_retries": {CID: [fake.now - 3600, fake.now - 60]}})
    for _ in range(3):
        fake.run([_stage_card()])
        fake.now += 60
    assert not fake.stops and not fake.started
    assert _open()


def test_a_pl_stage_agent_without_its_section_is_restarted(fake, monkeypatch):
    C.PIPELINE = {"stages": {"draft": {"column": "Drafting", "done": "Drafted", "section": "DRAFT"}}}
    fake.word = "finished"
    card = _stage_card(stage="draft", col="Drafting")
    monkeypatch.setattr(dispatch, "stage_for", lambda c, col: "draft")
    fake.run([card])
    fake.now += 60
    fake.run([card])
    assert fake.stops == [("%1", "s1")]


def test_a_finished_run_agent_is_not_restarted(fake):
    fake.word = "finished"
    fake.run([_card()])
    fake.now += 60
    fake.run([_card()])
    assert not fake.stops and not fake.kinds("stalled")
