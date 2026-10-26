"""Slow-work warnings: thresholds, one warning per episode, and the two agent-window conditions."""
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from pl import agents, cli, dispatch, slowwork
from pl import config as C
from test_dispatch_slots import _card, _pass

DEFAULTS = {"prompt_minutes": 10, "slow_factor": 2, "slow_minutes": 90, "wait_minutes": 60, "finished_minutes": 5}


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_CONFIG_DIR", str(tmp_path / ".pl-t"))
    (tmp_path / ".pl-t").mkdir()
    (tmp_path / ".pl-t" / "config.toml").write_text("")
    C.load("t")
    C.ACCOUNTS = {"claude": {"harness": "claude"}}
    C.PROFILES = {"claude": tmp_path / ".claude"}
    C.STAGES = {"spec": {"account": "claude"}}
    slowwork._since.clear()
    slowwork._open.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(slowwork, "_clock", lambda: now[0])
    return lambda minutes=0: now.__setitem__(0, now[0] + minutes * 60)


def _events():
    path = C.STATE_DIR / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# ---------- WP1: thresholds ----------

def test_thresholds_default_without_an_activity_table():
    assert slowwork.thresholds() == (DEFAULTS, [])


def test_thresholds_use_values_set_in_activity():
    C.ACTIVITY = {"prompt_minutes": 3, "slow_factor": 1.5}
    values, errors = slowwork.thresholds()
    assert values == {**DEFAULTS, "prompt_minutes": 3, "slow_factor": 1.5} and errors == []


@pytest.mark.parametrize("bad", [0, -1, "ten", True])
def test_a_value_that_is_not_a_positive_number_names_the_key_and_keeps_the_default(bad):
    C.ACTIVITY = {"wait_minutes": bad}
    values, errors = slowwork.thresholds()
    assert values["wait_minutes"] == 60
    assert errors == ["[activity] wait_minutes must be a positive number"]


def _run_cli(monkeypatch, *argv):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "argv", ["pl", *argv])
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def test_activity_thresholds_json_prints_the_five_keys(monkeypatch):
    code, out, _ = _run_cli(monkeypatch, "activity", "thresholds", "--json")
    assert code == 0 and json.loads(out) == DEFAULTS


def test_activity_thresholds_with_a_bad_value_prints_the_error_and_exits_2(profile, monkeypatch):
    (profile / ".pl-t" / "config.toml").write_text("[activity]\nprompt_minutes = 0\n")
    code, out, err = _run_cli(monkeypatch, "activity", "thresholds", "--json")
    assert code == 2 and "[activity] prompt_minutes must be a positive number" in err
    assert json.loads(out)["prompt_minutes"] == 10


# ---------- WP2: episodes ----------

SEEN = {"prompt:c1": ("agent_waiting_prompt", "c1", "prompt_minutes", {"window": "w1"})}


def test_a_condition_past_its_threshold_logs_one_warning_with_level_warning(clock):
    slowwork.observe(SEEN)
    clock(11)
    slowwork.observe(SEEN)
    [e] = _events()
    assert (e["kind"], e["card"], e["level"], e["minutes"], e["window"]) == ("agent_waiting_prompt", "c1", "warning", 11, "w1")


def test_the_same_condition_on_later_passes_logs_no_second_warning(clock):
    slowwork.observe(SEEN)
    for _ in range(3):
        clock(11)
        slowwork.observe(SEEN)
    assert len(_events()) == 1


def test_the_condition_ending_logs_one_cleared_info_entry_and_a_new_episode_can_warn_again(clock):
    slowwork.observe(SEEN)
    clock(11)
    slowwork.observe(SEEN)
    slowwork.observe({})
    slowwork.observe({})
    assert [(e["kind"], e["level"]) for e in _events()] == [("agent_waiting_prompt", "warning"), ("agent_waiting_prompt_cleared", "info")]
    slowwork.observe(SEEN)
    clock(11)
    slowwork.observe(SEEN)
    assert len(_events()) == 3 and _events()[-1]["level"] == "warning"


def test_a_condition_that_ends_before_its_threshold_logs_nothing(clock):
    slowwork.observe(SEEN)
    clock(5)
    slowwork.observe(SEEN)
    slowwork.observe({})
    assert _events() == []


# ---------- WP3: the dispatcher pass ----------

@pytest.fixture
def agent(monkeypatch, clock):
    """One alive run agent; `screen` is the word agent_screen reports."""
    state = {"word": "working", "ask": None, }
    c = _card(1, worker={"stage": "run", "window": "run-1", "pane": "%1", "profile": "claude", "harness": "claude"})
    c["list_id"] = "In progress"
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", "s1"))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "permission_wait", lambda *a, **k: state["ask"])
    monkeypatch.setattr(agents, "permission_wait", lambda *a, **k: state["ask"])
    monkeypatch.setattr(dispatch.agent_screen, "read", lambda pane, lines=20: ("", state["word"], ""))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(agents, "pane_exists", lambda pane: True)
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st: None)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: True)
    state["card"] = c
    return state


def _pass_once(monkeypatch, agent, dry=False):
    _pass(monkeypatch, [agent["card"]], dry=dry)


def test_an_agent_at_a_prompt_past_prompt_minutes_logs_agent_waiting_prompt_with_card_window_and_minutes(monkeypatch, agent, clock):
    agent["word"] = "waiting for you"
    _pass_once(monkeypatch, agent)
    clock(11)
    _pass_once(monkeypatch, agent)
    [e] = [e for e in _events() if e["kind"] == "agent_waiting_prompt"]
    assert (e["card"], e["window"], e["minutes"], e["level"]) == ("o/r#1", "run-1", 11, "warning")


def test_the_prompt_answered_logs_agent_waiting_prompt_cleared(monkeypatch, agent, clock):
    agent["ask"] = "Bash(rm x)"
    _pass_once(monkeypatch, agent)
    clock(11)
    _pass_once(monkeypatch, agent)
    agent["ask"] = None
    _pass_once(monkeypatch, agent)
    assert [e["kind"] for e in _events() if e["kind"].startswith("agent_waiting")] == ["agent_waiting_prompt", "agent_waiting_prompt_cleared"]


def test_a_finished_agent_still_alive_past_finished_minutes_logs_finished_window_open(monkeypatch, agent, clock):
    agent["word"] = "finished"
    _pass_once(monkeypatch, agent)
    clock(6)
    _pass_once(monkeypatch, agent)
    [e] = [e for e in _events() if e["kind"] == "finished_window_open"]
    assert (e["card"], e["window"], e["level"]) == ("o/r#1", "run-1", "warning")


def test_the_finished_window_closing_logs_finished_window_open_cleared(monkeypatch, agent, clock):
    agent["word"] = "finished"
    _pass_once(monkeypatch, agent)
    clock(6)
    _pass_once(monkeypatch, agent)
    agent["card"]["metadata"]["worker"], agent["card"]["list_id"] = None, "Done"
    _pass_once(monkeypatch, agent)
    assert [e["kind"] for e in _events() if e["kind"].startswith("finished_window")] == ["finished_window_open", "finished_window_open_cleared"]


def test_a_dry_run_pass_logs_no_slow_work_entries(monkeypatch, agent, clock):
    agent["word"] = "waiting for you"
    _pass_once(monkeypatch, agent, dry=True)
    clock(30)
    _pass_once(monkeypatch, agent, dry=True)
    assert _events() == []
