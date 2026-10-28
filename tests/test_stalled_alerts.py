"""Stalled work raises an alert and one notification, and clears itself when the work moves again (Plan 54 start)."""
import pytest

from pl import agents, alerts, dispatch
from pl import config as C
from pl.trackers import github


def _card(cid="o/r#1", col="In progress", **meta):
    return {"id": cid, "title": "Some card", "list_id": col, "description": "d",
            "metadata": {"pipeline_mode": "auto", **meta}}


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
    monkeypatch.setattr(github, "_gh", lambda args: (_ for _ in ()).throw(AssertionError(f"GitHub call {args}")))
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    monkeypatch.setattr(dispatch.time, "time", lambda: t["now"])
    monkeypatch.setattr(agents.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(dispatch, "notify", lambda title, fix="": out.append((title, fix)))
    return out


def _idle(clock, minutes, cid="o/r#1"):
    return {"run_waits": {cid: {"since": clock["now"] - minutes * 60, "session": "s1"}}}


def test_an_agent_idle_past_the_limit_while_its_card_has_not_moved_opens_an_alert_and_notifies(clock, sent):
    dispatch.check_alerts([_card()], set(), _idle(clock, 25))
    a = alerts.get("stalled:o/r#1")
    assert alerts._live(a) and "idle 25 min" in a["title"] and a["cards"] == ["o/r#1"]
    assert len(sent) == 1 and "Some card" in sent[0][0]


def test_an_agent_idle_less_than_the_limit_is_left_alone(clock, sent):
    dispatch.check_alerts([_card()], set(), _idle(clock, 10))
    assert not alerts._live(alerts.get("stalled:o/r#1")) and sent == []


def test_the_limit_is_a_setting(clock, sent):
    C.DISPATCH["stall_minutes"] = 5
    dispatch.check_alerts([_card()], set(), _idle(clock, 6))
    assert alerts._live(alerts.get("stalled:o/r#1"))


def test_the_alert_notifies_once_and_clears_when_the_agent_works_again(clock, sent):
    dispatch.check_alerts([_card()], set(), _idle(clock, 25))
    clock["now"] += 300
    dispatch.check_alerts([_card()], set(), _idle(clock, 30))
    assert len(sent) == 1
    dispatch.check_alerts([_card()], set(), {"run_waits": {}})
    assert not alerts._live(alerts.get("stalled:o/r#1"))


def test_a_leftover_release_hold_opens_no_stalled_alert(clock, sent):
    from datetime import datetime, timezone
    c = _card()
    c["metadata"]["run_retry_at"] = datetime.fromtimestamp(clock["now"] + 1200, timezone.utc).isoformat()
    c["metadata"]["run_released_why"] = "idle 31 min"
    c["metadata"]["run_releases"] = 2
    dispatch.check_alerts([c], set(), {"run_waits": {}})
    assert not alerts._live(alerts.get("stalled:o/r#1")) and sent == []


def test_a_card_that_reached_its_done_column_is_not_stalled(clock, sent):
    dispatch.check_alerts([_card(col="PR open")], set(), _idle(clock, 60))
    assert not alerts._live(alerts.get("stalled:o/r#1"))


def test_stall_minutes_must_be_a_whole_number(fake_home):
    import tomlkit
    assert any("dispatch.stall_minutes" in e for e in C.validate(tomlkit.parse("[dispatch]\nstall_minutes = -1\n")))
    assert not any("stall_minutes" in e for e in C.validate(tomlkit.parse("[dispatch]\nstall_minutes = 0\n")))


def test_a_stalled_alert_records_what_pl_tried(clock, sent):
    st = {"stalls": {"o/r#1": {"nudged_at": clock["now"] - 60}}, "stall_retries": {"o/r#1": [clock["now"] - 30]}}
    dispatch.check_stalled([_card()], {**st, **_idle(clock, 25)})
    assert alerts.get("stalled:o/r#1")["tried"] == "nudged, retried 1 time today"
    st["stall_retries"]["o/r#1"].append(clock["now"] - 10)
    dispatch.check_stalled([_card()], {**st, **_idle(clock, 25)})
    assert alerts.get("stalled:o/r#1")["tried"] == "nudged, retried 2 times today"


def test_a_stalled_alert_before_any_nudge_has_no_tried(clock, sent):
    dispatch.check_stalled([_card()], _idle(clock, 25))
    assert "tried" not in alerts.get("stalled:o/r#1")


def _stage_card(desc="# PIPELINE: SPEC\nthe spec"):
    c = _card(col="Spec ready", spec_approved_at="2026-10-26", worker={"stage": "plan"})
    c["description"] = desc
    return c


def _missing(cid="o/r#1"):
    return {"stalls": {cid: {"missing": "PLAN"}}}


def test_a_finished_stage_agent_without_its_section_opens_one_alert(clock, sent):
    for _ in range(2):
        dispatch.check_alerts([_stage_card()], set(), _missing())
    a = alerts.get("stalled:o/r#1")
    assert alerts._live(a) and "plan" in a["title"] and "PLAN" in a["title"]
    assert len(sent) == 1


def test_the_missing_section_alert_resolves_when_the_section_appears(clock, sent):
    dispatch.check_alerts([_stage_card()], set(), _missing())
    dispatch.check_alerts([_stage_card("# PIPELINE: PLAN\nx")], set(), _missing())
    assert not alerts._live(alerts.get("stalled:o/r#1"))


def test_the_missing_section_alert_resolves_when_the_card_moves(clock, sent):
    dispatch.check_alerts([_stage_card()], set(), _missing())
    dispatch.check_alerts([_stage_card()], set(), {"stalls": {}})
    assert not alerts._live(alerts.get("stalled:o/r#1"))
