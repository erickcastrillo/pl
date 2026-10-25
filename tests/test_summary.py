"""state/summary.json: what each dispatcher pass publishes for any screen to read."""
import json

import pytest

from pl import alerts, dispatch, summary
from pl import config as C


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(dispatch, "col_name", lambda list_id: list_id)
    monkeypatch.setattr(dispatch, "busy_agents", lambda reg: 2)
    monkeypatch.setattr(dispatch, "paused", lambda: None)
    monkeypatch.setattr(alerts, "listing", lambda all=False: [])
    yield d
    for k, v in saved.items():
        setattr(C, k, v)


def _card(n, col, **extra):
    return {"id": f"o/r#{n}", "title": f"Card {n}", "list_id": col, "updated_at": f"2026-10-0{n}T10:00:00+00:00",
            "metadata": {}, **extra}


def test_a_pass_writes_every_field(profile, monkeypatch):
    monkeypatch.setattr(alerts, "listing", lambda all=False: [{"key": "stage_failed:o/r#4:run"}, {"key": "loop_dead:x"}])
    cards = [_card(1, "Inbox"), _card(2, "Plan for review"), _card(3, "Manual"), _card(4, "In progress"), _card(5, "Done")]
    summary.write(summary.build(cards, {}, 1000.0))
    d = json.loads((profile / "state" / "summary.json").read_text())
    assert (d["version"], d["name"], d["running"], d["paused"], d["last_good_refresh"]) == (1, "t", True, False, 1000.0)
    assert d["pipeline"] == "engineering"
    assert [c["title"] for c in d["columns"]] == C.COLUMNS
    assert {c["title"]: c["cards"] for c in d["columns"]}["Done"] == 1
    assert d["needs_you"] == 3
    assert d["live_agents"] == 2
    assert d["oldest_wait"] == {"card": "o/r#2", "since": "2026-10-02T10:00:00+00:00"}
    assert d["alerts"] == 2
    assert oct((profile / "state" / "summary.json").stat().st_mode & 0o777) == "0o600"


def test_the_spec_gate_counts_only_unapproved_specs(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    cards = [_card(1, "Spec ready"), _card(2, "Spec ready", metadata={"spec_approved_at": "x"})]
    assert summary.build(cards, {}, 1.0)["needs_you"] == 1
    monkeypatch.setattr(C, "GATES", {})
    assert summary.build(cards, {}, 1.0)["needs_you"] == 0


def test_a_failed_pass_keeps_the_last_good_refresh(profile):
    summary.write(summary.build([], {}, 1000.0))
    summary.mark_failed("boom " * 100)
    d = json.loads((profile / "state" / "summary.json").read_text())
    assert d["last_good_refresh"] == 1000.0
    assert len(d["error"]) == 200


def test_read_of_a_missing_or_bad_file_is_none(profile):
    assert summary.read(profile) is None
    (profile / "state").mkdir(exist_ok=True)
    (profile / "state" / "summary.json").write_text("{not json")
    assert summary.read(profile) is None
    (profile / "state" / "summary.json").write_text("[1]")
    assert summary.read(profile) is None


def test_read_treats_an_old_summary_as_stopped(profile):
    summary.write(summary.build([], {}, 1.0))
    d = summary.read(profile)
    assert d["running"] is True
    d = summary.read(profile, interval=1, now=d["at"] + 4)
    assert d["running"] is False


def test_summary_holds_no_card_text(profile):
    c = _card(1, "Manual", description="SECRET-BODY-TEXT")
    c["title"] = "SECRET-TITLE"
    summary.write(summary.build([c], {}, 1.0))
    assert "SECRET" not in (profile / "state" / "summary.json").read_text()
