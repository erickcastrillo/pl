"""Plan 41 slice 3: alerts for parked run accounts and for PRs that need a person."""
import json

import pytest

from pl import accounts, agents, alerts, dispatch, events, manager, merges, rework
from pl import config as C
from pl.trackers import github

URL = "https://github.com/o/r/pull/7"


def _card(cid, col="Approved", **meta):
    return {"id": cid, "title": "Some card", "list_id": col, "metadata": {"pipeline_mode": "auto", **meta}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    C.PROFILES = {"a": str(tmp_path / "a"), "b": str(tmp_path / "b")}
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    rework.SEEN.clear()
    yield tmp_path
    rework.SEEN.clear()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    monkeypatch.setattr(dispatch.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(dispatch, "notify", lambda title, fix="": out.append((title, fix)))
    return out


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    def fail(args):
        raise AssertionError(f"unexpected GitHub call {args}")
    monkeypatch.setattr(github, "_gh", fail)


def _until(clock):
    return accounts._iso(clock["now"] + 7200)


def _park(clock, name="a"):
    C.PROFILE_STATE.write_text(json.dumps({name: {"until": _until(clock), "reason": "usage limit"}}))


# ---------- WP1 ----------

def test_parked_account_opens_alert_with_reset_cards_and_fix(clock, sent):
    _park(clock)
    cards = [_card("o/r#1", profile="a"), _card("o/r#2", "In progress", worker={"profile": "a"}), _card("o/r#3", profile="b")]
    dispatch.check_alerts(cards, set())
    a = alerts.get("account_parked:a")
    assert alerts._live(a)
    assert "15:46 UTC" in a["fix"] and "r#1, r#2" in a["fix"] and "pl accounts --reset a" in a["fix"]
    assert a["cards"] == ["o/r#1", "o/r#2"]


def test_machine_wide_park_opens_alert(clock, sent):
    machine = manager.machine_dir()
    machine.mkdir(parents=True)
    (machine / "accounts.json").write_text(json.dumps({accounts._folder("a"): {"until": _until(clock), "reason": "usage limit"}}))
    dispatch.check_alerts([], set())
    assert alerts._live(alerts.get("account_parked:a"))


def test_parked_alert_not_duplicated(clock, sent):
    _park(clock)
    dispatch.check_alerts([], set())
    dispatch.check_alerts([], set())
    assert [a["key"] for a in alerts.listing() if a["key"].startswith("account_parked:")] == ["account_parked:a"]
    assert len(sent) == 1


def test_unparked_account_closes_alert_and_logs(clock, sent):
    _park(clock)
    dispatch.check_alerts([], set())
    C.PROFILE_STATE.write_text("{}")
    dispatch.check_alerts([], set())
    assert not alerts._live(alerts.get("account_parked:a"))
    kinds = [json.loads(line) for line in events._path().read_text().splitlines()]
    mine = [e["kind"] for e in kinds if (e.get("key") or e.get("detail", {}).get("key")) == "account_parked:a"]
    assert mine == ["alert_opened", "alert_resolved"]


def test_waiting_cards_capped_at_ten(clock, sent):
    _park(clock)
    dispatch.check_alerts([_card(f"o/r#{i}", profile="a") for i in range(12)], set())
    assert alerts.get("account_parked:a")["fix"].count("+2 more") == 1


# ---------- WP2 ----------

def test_merges_pr_records_labels(monkeypatch):
    reply = {"url": URL, "state": "OPEN", "labels": [{"name": "pl:review-failed"}]}
    monkeypatch.setattr(github, "_gh", lambda args: json.dumps(reply))
    merges._pr("o/r", 7)
    assert rework.SEEN[URL] == {"pl:review-failed"}
    reply["state"] = "MERGED"
    merges._pr("o/r", 7)
    assert URL not in rework.SEEN


def test_flagged_records_labels(monkeypatch):
    item = {"number": 7, "html_url": URL, "pull_request": {"url": URL}, "labels": [{"name": "pl:needs-rework"}, {"name": "x"}]}
    monkeypatch.setattr(github, "_gh", lambda args: json.dumps([item]))
    rework.flagged({"o/r"}, rework.labels())
    assert rework.SEEN[URL] == {"pl:needs-rework", "x"}


# ---------- WP3 ----------

def _pr_card(**rw):
    return _card("o/r#9", "PR open", pr_urls=[URL], rework=rw)


def _person(cards):
    dispatch.check_alerts(cards, set())
    return alerts.get("pr_person:o/r#9")


def test_last_rework_round_opens_pr_person(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    a = _person([_pr_card(round=3)])
    assert alerts._live(a)
    assert "round 3 of 3" in a["title"] and URL in a["fix"] and "remove pl:review-failed" in a["fix"]
    assert a["cards"] == ["o/r#9"]


def test_gave_up_opens_pr_person(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    assert "gave up" in _person([_pr_card(round=1, gave_up_at="2026-10-26T00:00:00Z")])["title"]


def test_failed_and_needs_person_labels_from_seen(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    rework.SEEN[URL] = frozenset({"pl:review-failed", "needs-person"})
    a = _person([_pr_card(round=3)])
    assert "label pl:review-failed is set" in a["title"] and "label needs-person is set" in a["title"]
    assert "needs-person" in a["fix"]
    assert len([x for x in alerts.listing() if x["key"].startswith("pr_person:")]) == 1


def test_pr_person_not_duplicated(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    card = _pr_card(round=3)
    dispatch.check_alerts([card], set())
    dispatch.check_alerts([card], set())
    assert len([x for x in alerts.listing() if x["key"].startswith("pr_person:")]) == 1
    assert len(sent) == 1


def test_pr_person_closes_when_card_leaves_pr_open(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    card = _pr_card(round=3)
    assert alerts._live(_person([card]))
    card["list_id"] = "Done"
    assert not alerts._live(_person([card]))


def test_pr_person_closes_when_pr_merged(clock, sent):
    C.DISPATCH["rework_rounds"] = 3
    rework.SEEN[URL] = frozenset({"needs-person"})
    card = _pr_card(round=0)
    assert alerts._live(_person([card]))
    rework.saw(URL, "MERGED", [])
    assert not alerts._live(_person([card]))


def test_rounds_zero_ignores_round_rule_keeps_labels(clock, sent):
    C.DISPATCH["rework_rounds"] = 0
    assert _person([_pr_card(round=3)]) is None
    rework.SEEN[URL] = frozenset({"pl:review-failed"})
    assert alerts._live(_person([_pr_card(round=3)]))
