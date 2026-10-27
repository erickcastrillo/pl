"""Plan 54 slice 3: a write refused by pl's own budget hold is queued, replayed after the reset, and listed by pl doctor.

The fake gh, its ledger helper and the profile come from test_gh_rate_limit; nothing here touches the real HOME,
tmux or the network.
"""
import argparse
import json
import time

import pytest
from test_gh_rate_limit import _ledger, _move
from test_gh_rate_limit import gh as _fake_gh

from pl import alerts, board, commands, dispatch, doctor, events, heldwrites
from pl.trackers import github

CARD3, CARD4 = "acme/app#3", "acme/app#4"
gh = _fake_gh   # the fake gh fixture and profile of test_gh_rate_limit


def _kinds(kind):
    return [e for e in events._read() if e["kind"] == kind]


def _edits(gh):
    return [a[a.index("--id") + 1] + ":" + a[-1] for a in gh.calls() if a[:2] == ["project", "item-edit"]]


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(heldwrites, "_now", lambda: now[0])
    return now


def _queue(card, column, until, pr=None):
    return heldwrites.add(card, "move", {"column": column, "pr": pr}, until)


# ---------- WP1: pl move under a held budget ----------

def test_held_move_is_queued_and_exits_0(gh, capsys):
    reset = _ledger(10, {})
    _move("3", "Approved")
    out = capsys.readouterr().out
    assert "queued" in out and time.strftime("%H:%M", time.localtime(reset)) in out
    (entry,) = heldwrites.entries()
    assert entry["card"] == CARD3 and entry["op"] == "move" and entry["args"]["column"] == "Approved" and entry["at"]
    assert len(_kinds("write_queued")) == 1
    assert gh.count("project", "item-edit") == 0


def test_queue_is_read_back_from_disk(gh):
    _queue(CARD3, "Approved", 100)
    _queue(CARD4, "Done", 200)
    assert [(e["seq"], e["card"]) for e in heldwrites.entries()] == [(1, CARD3), (2, CARD4)]
    assert oct(heldwrites.path().stat().st_mode & 0o777) == "0o600"
    with heldwrites.path().open("a") as f:
        f.write('{"seq": 3, "ca')   # a write cut short by a crash
    assert len(heldwrites.entries()) == 2


def test_move_with_budget_left_queues_nothing(gh, capsys):
    _ledger(4000, {})
    _move("3", "Approved")
    assert "moved acme/app#3 to Approved" in capsys.readouterr().out
    assert not heldwrites.path().exists()


def test_rate_limit_is_not_queued(gh):
    github._LIMIT.update(until=time.time() + 600, hits=1)
    with pytest.raises(github.RateLimited):
        _move("3", "Approved")
    assert not heldwrites.path().exists()


def test_held_move_with_a_pr_keeps_it_for_the_replay(gh, capsys):
    _ledger(10, {})
    pr = "https://github.com/acme/app/pull/9"
    commands.cmd_move(argparse.Namespace(id="3", column="PR open", pr=pr))
    (entry,) = heldwrites.entries()
    assert entry["args"] == {"column": "PR open", "pr": pr}


# ---------- WP2: replay, alert, doctor ----------

def test_replay_waits_for_the_reset_then_applies_in_order(gh, clock):
    _queue(CARD3, "Approved", clock[0] + 60)
    _queue(CARD4, "Done", clock[0] + 60)
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    assert gh.count("project", "item-edit") == 0 and len(heldwrites.entries()) == 2
    clock[0] += 61
    heldwrites.replay(lambda *a: notes.append(a))
    assert _edits(gh) == ["PVTI_3:opt-Approved", "PVTI_4:opt-Done"]
    assert heldwrites.entries() == [] and notes == []
    assert [e["card"] for e in _kinds("write_replayed")] == [CARD3, CARD4]


def test_two_moves_of_one_card_end_in_the_later_column(gh, clock):
    _queue(CARD3, "In progress", clock[0])
    _queue(CARD3, "PR open", clock[0])
    heldwrites.replay(lambda *a: None)
    assert _edits(gh) == ["PVTI_3:opt-In progress", "PVTI_3:opt-PR open"]
    assert heldwrites.entries() == []


def test_failed_replay_drops_the_write_and_alerts(gh, clock):
    first = _queue(CARD3, "No such column", clock[0])
    _queue(CARD4, "Done", clock[0])
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    alert = alerts.get(f"queued_write_failed:{CARD3}:{first['seq']}")
    assert alert and alerts._live(alert)
    assert "app#3" in alert["title"] and "move" in alert["title"] and "No such column" in alert["title"] + alert["fix"]
    assert len(notes) == 1
    (failed,) = _kinds("queued_write_failed")
    assert failed["card"] == CARD3 and failed["op"] == "move" and failed["error"]
    assert heldwrites.entries() == [] and _edits(gh) == ["PVTI_4:opt-Done"]
    assert [e["card"] for e in _kinds("write_replayed")] == [CARD4]


def test_budget_held_again_on_replay_keeps_the_queue_and_opens_no_alert(gh, clock):
    _queue(CARD3, "Approved", clock[0])
    _queue(CARD4, "Done", clock[0])
    _ledger(10, {})
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    assert [e["card"] for e in heldwrites.entries()] == [CARD3, CARD4]
    assert notes == [] and alerts.listing() == [] and not _kinds("queued_write_failed")


def test_doctor_lists_queued_writes(gh):
    assert doctor.queued_writes() == (True, ["Queued writes: none"])
    until = time.time() + 600
    _queue(CARD3, "Approved", until)
    ok, lines = doctor.queued_writes()
    assert ok and lines[0].startswith("Queued writes:")
    (row,) = lines[1:]
    assert "app#3" in row and "move -> Approved" in row and time.strftime("%H:%M", time.localtime(until)) in row
    assert heldwrites.entries()[0]["at"] in row


def test_dispatch_once_replays_the_queue(gh, monkeypatch):
    _queue(CARD3, "Approved", time.time() - 5)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "cards", lambda: [])
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    dispatch.dispatch_once(1, False, pull=False)
    assert heldwrites.entries() == [] and _edits(gh) == ["PVTI_3:opt-Approved"]


def test_a_dry_pass_leaves_the_queue_alone(gh, monkeypatch):
    _queue(CARD3, "Approved", time.time() - 5)
    monkeypatch.setattr(dispatch, "cards", lambda: [])
    dispatch.dispatch_once(1, True, pull=False)
    assert len(heldwrites.entries()) == 1 and json.loads(heldwrites.path().read_text().splitlines()[0])["card"] == CARD3


# ---------- review round 1: a move is not replayed over a person's move ----------

def _save_board(*cards):
    heldwrites.C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (heldwrites.C.STATE_DIR / "board-cache.json").write_text(json.dumps({"key": board._key(), "cards": list(cards)}))


def test_held_move_records_the_column_the_shared_board_save_had(gh):
    _save_board({"id": CARD3, "list_id": "opt-Spec ready"})
    _ledger(10, {})
    _move("3", "Approved")
    assert heldwrites.entries()[0]["was"] == "opt-Spec ready"


def test_held_move_without_a_board_save_records_no_column(gh):
    _ledger(10, {})
    _move("3", "Approved")
    assert "was" not in heldwrites.entries()[0]


def test_replay_skips_a_move_when_the_card_was_moved_since(gh, clock):
    heldwrites.add(CARD3, "move", {"column": "Approved", "pr": None}, clock[0], was="opt-Spec ready")
    gh.set(status={"3": "Done"})
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    assert gh.count("project", "item-edit") == 0 and heldwrites.entries() == []
    (skipped,) = _kinds("write_replay_skipped")
    assert skipped["card"] == CARD3 and skipped["op"] == "move" and skipped["why"]
    assert notes == [] and alerts.listing() == [] and not _kinds("write_replayed")


def test_replay_applies_a_move_when_the_card_is_still_where_it_was(gh, clock):
    heldwrites.add(CARD3, "move", {"column": "Approved", "pr": None}, clock[0], was="opt-Spec ready")
    gh.set(status={"3": "Spec ready"})
    heldwrites.replay(lambda *a: None)
    assert _edits(gh) == ["PVTI_3:opt-Approved"] and heldwrites.entries() == []
