"""A stuck card no rule settles goes to the steward_classify local job; its answer is checked against fixed lists."""
from datetime import datetime, timezone

import pytest

from pl import agent_screen, commands, dispatch, events, local_jobs, steward
from pl import config as C

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(events, "_read", lambda: [])
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("", "", ""))
    monkeypatch.setattr(steward.notifications, "send", lambda *a: True)
    monkeypatch.setattr(steward.alerts, "_offer", lambda *a, **k: None)
    yield
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, card=None, **d: out.append((kind, card, d)))
    return out


def _card():
    return {"id": "o/r#1", "title": "Secret title", "list_id": "In progress", "description": "secret body",
            "updated_at": datetime.fromtimestamp(NOW - 9 * 3600, timezone.utc).isoformat(), "metadata": {"pipeline_mode": "auto"}}


def _run(monkeypatch, answer):
    seen = []
    monkeypatch.setattr(local_jobs, "run", lambda job, text: seen.append((job, text)) or (answer, {}))
    return steward.run_pass([_card()], {}, reg={}, now=NOW), seen


def test_model_choice_applied_and_logged_by_model(monkeypatch, emitted):
    calls = []
    monkeypatch.setattr(commands, "retry", lambda ref, stage=None: calls.append(ref) or [])
    rows, _ = _run(monkeypatch, "dead/retry")
    assert rows[0]["by"] == "model" and rows[0]["action"] == "retry" and calls == ["o/r#1"]
    assert emitted[0][2]["by"] == "model"


def test_facts_only_in_the_prompt(monkeypatch):
    _, seen = _run(monkeypatch, "slow/none")
    assert seen[0][0] == "steward_classify" and "Secret" not in seen[0][1] and "secret" not in seen[0][1] and "In progress" in seen[0][1]


@pytest.mark.parametrize("answer", [None, "garbage", "waiting_owner/merge", "wat/none", "dead"])
def test_bad_or_missing_answer_becomes_unclear(monkeypatch, answer):
    rows, _ = _run(monkeypatch, answer)
    assert (rows[0]["reason"], rows[0]["line"], rows[0]["action"]) == ("waiting_owner", "unclear", "needs you")


def test_no_local_model_skips_to_unclear(monkeypatch):
    rows = steward.run_pass([_card()], {}, reg={}, now=NOW)
    assert rows[0]["line"] == "unclear" and rows[0]["by"] == "model"


def test_model_call_recorded_in_stats(monkeypatch):
    C.MODELS = {"local": {"steward_classify": "m:1"}}
    monkeypatch.setattr(local_jobs, "_ask_local", lambda model, s, system, text: {"choice": "slow/none"})
    rows = steward.run_pass([_card()], {}, reg={}, now=NOW)
    assert rows[0]["reason"] == "slow"
    log = (C.STATE_DIR / "local-jobs.jsonl").read_text()
    assert '"job": "steward_classify"' in log and '"ok": true' in log
