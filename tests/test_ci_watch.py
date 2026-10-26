"""Plan 41 slice 4: one alert when the newest pl pull request has had no check for over 30 minutes."""
import json
import types

import pytest

from pl import alerts, board, ci_watch
from pl import config as C
from pl.trackers import github

KEY = "ci_not_running:o/r"
NOW = 1_000_000.0


def _iso(ago):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - ago, timezone.utc).isoformat()


def _view(ago_min, checks=()):
    return {"returncode": 0, "stderr": "", "stdout": json.dumps({
        "headRefOid": "abc", "commits": [{"committedDate": _iso(ago_min * 60)}],
        "statusCheckRollup": list(checks)})}


def _gh_says(monkeypatch, *outcomes):
    calls = []

    def run(args):
        calls.append(args)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        return types.SimpleNamespace(**out)

    monkeypatch.setattr(github, "_run", run)
    monkeypatch.setattr(github, "limited", lambda resource: None)
    monkeypatch.setattr(github, "_budget_wait", lambda kind: None)
    return calls


def _card(n, updated=0):
    return {"id": f"o/r#{n}", "list_id": "PR open", "updated_at": _iso(updated),
            "metadata": {"pr_urls": [f"https://github.com/o/r/pull/{n}"]}}


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    C.STATE_DIR = tmp_path
    C.STAGE_DONE_AT = {"run": "PR open"}
    C.AUTO_MERGE = {}
    monkeypatch.setattr(board, "col_name", lambda list_id: list_id)
    monkeypatch.setattr(ci_watch, "_clock", lambda: NOW)
    monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    ci_watch._CACHE.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def notes():
    return []


def _pass(cards, notes):
    ci_watch.check(cards, lambda *a: notes.append(a))


def _open():
    return [a for a in alerts.listing() if a["key"] == KEY]


def test_opens_high_alert_after_30_min_without_checks(monkeypatch, notes):
    _gh_says(monkeypatch, _view(31))
    _pass([_card(1, 5), _card(2, 10)], notes)
    [a] = _open()
    assert a["severity"] == "high" and a["title"] == "CI is not running on o/r"
    assert a["fix"] == "check the repository's Actions minutes and workflow settings (Settings → Actions)"
    assert a["cards"] == ["o/r#1", "o/r#2"]


def test_no_second_alert_on_later_passes(monkeypatch, notes):
    _gh_says(monkeypatch, _view(31))
    clock = [NOW]
    monkeypatch.setattr(ci_watch, "_clock", lambda: clock[0])
    for _ in range(3):
        _pass([_card(1)], notes)
        clock[0] += ci_watch.EVERY + 1
        monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    [a] = _open()
    assert a["count"] == 3 and len(notes) == 1


def test_resolves_when_a_queued_check_appears(monkeypatch, notes):
    logged = []
    monkeypatch.setattr(alerts, "_logged", lambda kind, **d: logged.append((kind, d)))
    _gh_says(monkeypatch, _view(31), _view(31, [{"status": "QUEUED"}]))
    _pass([_card(1)], notes)
    assert _open()
    _pass([_card(1)], notes)
    assert not _open()
    assert any(k == "alert_resolved" for k, _ in logged)


def test_under_30_min_no_alert(monkeypatch, notes):
    _gh_says(monkeypatch, _view(10))
    _pass([_card(1)], notes)
    assert not _open()


def test_one_call_per_pass_and_cached_hit_makes_none(monkeypatch, notes):
    calls = _gh_says(monkeypatch, _view(31, [{"status": "COMPLETED"}]))
    cards = [_card(1, 30), _card(2, 5), _card(3, 20)]
    _pass(cards, notes)
    assert len(calls) == 1 and "2" in calls[0]
    _pass(cards, notes)
    assert len(calls) == 1


def test_call_failure_leaves_state_unchanged(monkeypatch, notes):
    _gh_says(monkeypatch, _view(31))
    _pass([_card(1)], notes)
    _gh_says(monkeypatch, {"returncode": 1, "stdout": "", "stderr": "HTTP 404"})
    _pass([_card(1)], notes)
    assert _open()
    monkeypatch.setattr(github, "_budget_wait", lambda kind: (NOW + 60, "budget"))
    _pass([_card(1)], notes)
    assert _open()


def test_failure_opens_nothing(monkeypatch, notes):
    _gh_says(monkeypatch, {"returncode": 1, "stdout": "", "stderr": "HTTP 404"})
    _pass([_card(1)], notes)
    assert not _open()


def test_no_pr_open_card_resolves_and_makes_no_call(monkeypatch, notes):
    calls = _gh_says(monkeypatch, _view(31))
    _pass([_card(1)], notes)
    assert _open()
    _pass([], notes)
    assert not _open() and len(calls) == 1


def test_require_ci_false_skips(monkeypatch, notes):
    C.AUTO_MERGE = {"require_ci": False}
    calls = _gh_says(monkeypatch, _view(31))
    _pass([_card(1)], notes)
    assert not _open() and not calls
