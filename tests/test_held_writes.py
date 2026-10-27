"""Plan 54 slice 3: a write refused by pl's own budget hold is queued, replayed after the reset, and listed by pl doctor.

The fake gh, its ledger helper and the profile come from test_gh_rate_limit; nothing here touches the real HOME,
tmux or the network.
"""
import argparse
import json
import time
import types

import pytest
from test_gh_rate_limit import _ledger, _move
from test_gh_rate_limit import gh as _fake_gh

from pl import alerts, board, commands, dispatch, doctor, events, heldwrites, rework
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


# ---------- Plan 54 slice 8: dispatcher and rework writes join the queue ----------

PR = "https://github.com/acme/app/pull/{}"


@pytest.fixture
def held(gh, monkeypatch, clock):
    """board.update, move_to and the rework gh calls faked; ns.until set = pl's budget hold refuses every write."""
    ns = types.SimpleNamespace(until=None, writes=[], moves=[], gh=[], flagged=set(), cards={}, now=clock)

    def refuse():
        if ns.until:
            raise github.OverBudget(ns.until, "test hold")

    def update(cid, **fields):
        refuse()
        ns.writes.append((cid, fields))
        return "updated"

    def move_to(ref, col, cid=None):
        refuse()
        ns.moves.append((ref, col))

    def fake_gh(args):
        if args[:1] == ["api"] and len(args) == 2:
            return json.dumps([{"number": n, "html_url": PR.format(n), "pull_request": {"url": "u"}, "labels": [{"name": "pl:needs-rework"}]}
                               for r, n in ns.flagged if r in args[1]])
        refuse()
        ns.gh.append(args)
        return "{}"
    monkeypatch.setattr(board, "update", update)
    monkeypatch.setattr(rework, "update", update)
    monkeypatch.setattr(commands, "move_to", move_to)
    monkeypatch.setattr(rework.github, "_gh", fake_gh)
    monkeypatch.setattr(rework, "col_name", lambda lid: lid)
    monkeypatch.setattr(rework, "card", lambda cid: ns.cards[cid])
    monkeypatch.setattr(rework, "rounds", lambda: 3)
    monkeypatch.setattr(rework.notifications, "send", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "stop_worker", lambda *a, **k: None)
    rework._LAST["at"] = None
    return ns


def _rcard(cid, prs=(1,), col="PR open", **meta):
    return {"id": cid, "title": f"card {cid}", "list_id": col,
            "metadata": {"pipeline_mode": "auto", "pr_urls": [PR.format(n) for n in prs], **meta}}


def _hold(held, clock):
    held.until = clock[0] + 600
    return held.until


def test_dispatcher_move_under_hold_is_queued_and_the_pass_goes_on(held, clock, capsys, monkeypatch):
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    _hold(held, clock)
    cards = [{"id": CARD3, "list_id": "In progress", "description": ""}, {"id": CARD4, "list_id": "In progress", "description": ""}]
    for c in cards:
        assert dispatch.stop_at_cap(c, {"stage": "run"}, "capped")
    got = heldwrites.entries()
    assert [(e["card"], e["op"], e["args"]["column"]) for e in got] == [(CARD3, "update", "Manual"), (CARD4, "update", "Manual")]
    assert len(_kinds("write_queued")) == 2 and not _kinds("error")
    assert [e["card"] for e in _kinds("capped")] == [CARD3, CARD4]
    assert "queued" in capsys.readouterr().out


def test_dispatcher_bookkeeping_write_under_hold_still_raises(held, clock):
    _hold(held, clock)
    with pytest.raises(github.OverBudget):
        dispatch.update(CARD3, metadata={"worker": {"stage": "run"}})
    assert not heldwrites.path().exists()


def test_queued_dispatcher_write_replays_after_the_reset(held, clock):
    heldwrites.add(CARD3, "update", {"column": "Manual", "tags": ["a", "b"]}, clock[0] + 60)
    heldwrites.replay(lambda *a: None)
    assert held.writes == [] and len(heldwrites.entries()) == 1
    clock[0] += 61
    heldwrites.replay(lambda *a: None)
    assert held.writes == [(CARD3, {"column": "Manual", "tags": ["a", "b"]})]
    assert len(_kinds("write_replayed")) == 1 and heldwrites.entries() == []


def test_dispatcher_write_with_budget_left_queues_nothing(held, capsys):
    assert dispatch.update(CARD3, column="Manual") == "updated"
    assert capsys.readouterr().out == "" and not heldwrites.path().exists()


def test_doctor_names_the_target_of_each_new_op(gh):
    until = time.time() + 600
    heldwrites.add(CARD3, "update", {"column": "Manual"}, until)
    heldwrites.add(CARD3, "update", {"tags": ["a", "b"]}, until)
    heldwrites.add(CARD3, "update", {"metadata": {"pr_urls": [PR.format(1), PR.format(2)]}}, until)
    heldwrites.add(CARD3, "rework", {"round": 2, "prs": [PR.format(1)]}, until)
    heldwrites.add(CARD3, "give_up", {"prs": [PR.format(1), PR.format(2)], "rounds": 3}, until)
    rows = heldwrites.doctor_lines()
    assert [r.split("queued")[0].split("  ", 1)[1].strip() for r in rows] == [
        "update -> Manual", "update -> labels a, b", f"update -> link {PR.format(2)}",
        "rework -> In progress (round 2)", "give_up -> failed label on #1, #2"]


def test_pl_rework_under_hold_queues_the_send_back_and_exits_0(held, clock, monkeypatch, capsys):
    held.flagged = {("acme/app", 1)}
    monkeypatch.setattr(board, "cards", lambda: [_rcard("c1")])
    reset = _hold(held, clock)
    rework.cmd_rework(argparse.Namespace(dry_run=False))
    out = capsys.readouterr().out
    assert "queued" in out and time.strftime("%H:%M", time.localtime(reset)) in out
    (e,) = heldwrites.entries()
    assert (e["card"], e["op"], e["args"]) == ("c1", "rework", {"round": 1, "prs": [PR.format(1)]})
    assert held.writes == [] and held.moves == []


def test_sweep_queues_a_give_up_and_carries_on(held, clock):
    held.flagged = {("acme/app", 1), ("acme/app", 2)}
    old = _rcard("c1", prs=(1,), rework={"round": 3})
    new = _rcard("c2", prs=(2,), rework={"round": 0})
    _hold(held, clock)
    assert rework.sweep([old, new], dry=False) == []
    assert [(e["card"], e["op"]) for e in heldwrites.entries()] == [("c1", "give_up"), ("c2", "rework")]
    assert heldwrites.entries()[0]["args"] == {"prs": [PR.format(1)], "rounds": 3}
    assert len(_kinds("write_queued")) == 2 and not _kinds("error")


def test_queued_send_back_replays_after_the_reset(held, clock):
    held.flagged = {("acme/app", 1)}
    held.cards["c1"] = _rcard("c1")
    heldwrites.add("c1", "rework", {"round": 2, "prs": [PR.format(1)]}, clock[0])
    heldwrites.replay(lambda *a: None)
    (cid, fields), = held.writes
    assert cid == "c1" and fields["metadata"]["rework"]["round"] == 2 and fields["metadata"]["rework"]["prs"] == [PR.format(1)]
    assert held.moves == [("c1", "In progress")]
    assert len(_kinds("write_replayed")) == 1 and heldwrites.entries() == []


def test_queued_give_up_labels_and_comments_each_pr_once(held, clock):
    held.cards["c1"] = _rcard("c1", prs=(1, 2))
    heldwrites.add("c1", "give_up", {"prs": [PR.format(1), PR.format(2)], "rounds": 3}, clock[0])
    heldwrites.replay(lambda *a: None)
    labels = [a for a in held.gh if any(x.endswith("/labels") for x in a)]
    comments = [a for a in held.gh if any(x.endswith("/comments") for x in a)]
    assert len(labels) == 2 and len(comments) == 2 and heldwrites.entries() == []
    assert len(_kinds("write_replayed")) == 1


def test_queued_send_back_is_skipped_when_the_card_left_pr_open(held, clock):
    held.flagged = {("acme/app", 1)}
    held.cards["c1"] = _rcard("c1", col="In progress")
    heldwrites.add("c1", "rework", {"round": 1, "prs": [PR.format(1)]}, clock[0])
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    (skipped,) = _kinds("write_replay_skipped")
    assert skipped["why"] and heldwrites.entries() == [] and held.moves == [] and notes == [] and alerts.listing() == []


def test_queued_send_back_is_skipped_when_the_pr_is_no_longer_flagged(held, clock):
    held.cards["c1"] = _rcard("c1")
    heldwrites.add("c1", "rework", {"round": 1, "prs": [PR.format(1)]}, clock[0])
    heldwrites.replay(lambda *a: None)
    assert len(_kinds("write_replay_skipped")) == 1 and held.moves == []


def test_failed_rework_replay_is_dropped_and_alerted(held, clock, monkeypatch):
    held.flagged = {("acme/app", 1)}
    held.cards["c1"] = _rcard("c1")
    first = heldwrites.add("c1", "rework", {"round": 1, "prs": [PR.format(1)]}, clock[0])
    monkeypatch.setattr(commands, "move_to", lambda *a, **k: (_ for _ in ()).throw(SystemExit("no such column")))
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    assert heldwrites.entries() == [] and len(notes) == 1
    assert alerts.get(f"queued_write_failed:c1:{first['seq']}")


def test_held_again_rework_replay_keeps_the_entry_with_no_alert(held, clock):
    held.flagged = {("acme/app", 1)}
    held.cards["c1"] = _rcard("c1")
    heldwrites.add("c1", "rework", {"round": 1, "prs": [PR.format(1)]}, clock[0])
    _hold(held, clock)
    notes = []
    heldwrites.replay(lambda *a: notes.append(a))
    assert len(heldwrites.entries()) == 1 and notes == [] and alerts.listing() == []


def test_pl_rework_with_budget_left_queues_nothing(held, monkeypatch, capsys):
    held.flagged = {("acme/app", 1)}
    monkeypatch.setattr(board, "cards", lambda: [_rcard("c1")])
    rework.cmd_rework(argparse.Namespace(dry_run=False))
    assert "back to In progress (round 1)" in capsys.readouterr().out
    assert held.moves == [("c1", "In progress")] and not heldwrites.path().exists()
