"""Tests for the card screen header line, actions in place, links and relative dates."""
import time
from pl import config as C
from pl.tui.cards import CardScreen, card_head
from pl.tui.parity import PARITY


def test_header_reads_stage_who_next(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": False})

    # Working agent on plan
    c_plan = {"title": "Build feature", "metadata": {"worker": {"stage": "plan"}}}
    head = card_head(c_plan, "Spec ready", None).plain
    assert "plan · plan agent · agent writes PLAN" in head

    # Working agent on spec
    c_spec = {"title": "Spec feature", "metadata": {"worker": {"stage": "spec"}}}
    head = card_head(c_spec, "Inbox", None).plain
    assert "spec · spec agent · agent writes SPEC" in head

    # Working agent on run
    c_run = {"title": "Run feature", "metadata": {"worker": {"stage": "run"}}}
    head = card_head(c_run, "Approved", None).plain
    assert "run · run agent · agent builds PLAN" in head

    # Card at gate: Plan for review
    c_gate = {"title": "Reviewed card", "metadata": {}}
    head = card_head(c_gate, "Plan for review", None).plain
    assert "Plan for review · you · approve or send back" in head

    # Card at spec gate: Spec ready when spec gate is on
    monkeypatch.setattr(C, "GATES", {"spec": True})
    c_spec_gate = {"title": "Gated spec", "metadata": {}}
    head = card_head(c_spec_gate, "Spec ready", None).plain
    assert "Spec ready · you · approve or send back" in head

    # Queued card in Inbox
    c_inbox = {"title": "Queued card", "metadata": {}}
    head = card_head(c_inbox, "Inbox", None).plain
    assert "spec · nobody · waits for a slot" in head

    # Queued card in Approved
    c_app = {"title": "Queued run", "metadata": {}}
    head = card_head(c_app, "Approved", None).plain
    assert "run · nobody · waits for a slot" in head

    # Manual card
    c_manual = {"title": "Manual card", "metadata": {}}
    head = card_head(c_manual, "Manual", None).plain
    assert "Manual · you · yours to do" in head


def test_gate_card_offers_approve_and_send_back_in_place(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})

    # BINDINGS on CardScreen
    action_names = {b.action for b in CardScreen.BINDINGS}
    assert {"approve", "send_back", "hold", "restart", "drop"}.issubset(action_names)

    # Parity map
    assert PARITY["card.approve"] == "pl approve"
    assert PARITY["card.send_back"] == "pl reject"
    assert PARITY["card.hold"] == "pl hold"
    assert PARITY["card.restart"] == "pl restart"
    assert PARITY["card.drop"] == "pl drop"

    # Card at Plan for review gate
    screen = CardScreen("test#1", "Plan for review")
    assert screen.check_action("approve", ()) is True
    assert screen.check_action("send_back", ()) is True
    assert screen.check_action("hold", ()) is True
    assert screen.check_action("drop", ()) is True

    # Card at Spec ready gate
    screen_spec = CardScreen("test#2", "Spec ready")
    assert screen_spec.check_action("approve", ()) is True
    assert screen_spec.check_action("send_back", ()) is True

    # Card not at gate
    screen_inbox = CardScreen("test#3", "Inbox")
    assert not screen_inbox.check_action("approve", ())
    assert not screen_inbox.check_action("send_back", ())
    assert screen_inbox.check_action("hold", ()) is True
    assert screen_inbox.check_action("drop", ()) is True

    # Card in Done
    screen_done = CardScreen("test#4", "Done")
    assert not screen_done.check_action("approve", ())
    assert not screen_done.check_action("send_back", ())
    assert not screen_done.check_action("hold", ())
    assert not screen_done.check_action("drop", ())


def test_links_are_listed():
    c = {"title": "Linked card", "metadata": {"links": [{"pipeline": "eng", "id": "repo#42", "relation": "built-for"}]}}
    head = card_head(c, "Inbox", None).plain
    assert "links" in head
    assert "eng:repo#42 (built-for)" in head

    c_no_links = {"title": "Plain card", "metadata": {}}
    head_no = card_head(c_no_links, "Inbox", None).plain
    assert "links    -" in head_no


def test_dates_read_as_relative_times(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    c = {"title": "Time card", "updated_at": "2023-11-14T22:08:20+00:00"}
    head = card_head(c, "Inbox", None).plain
    assert "updated  5m ago" in head
    assert "2023-11-14T22:08:20+00:00" in head
