"""Tests for the card screen header line, actions in place, links and relative dates."""
import re
import time
from pl import config as C
from pl import pipeline
from pl.tui import cards
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


STAGE_AGENT = "marketing-copywriter"


def agent_pipeline(monkeypatch, known=None, draft_column="Draft"):
    monkeypatch.setattr(C, "GATES", {"spec": False})
    monkeypatch.setattr(C, "PIPELINE", {"stages": {
        "draft": {"column": draft_column, "agent": STAGE_AGENT}, "review": {"column": "Review"}}})
    monkeypatch.setattr(cards.agent_defs, "load", lambda: {STAGE_AGENT: {}} if known is None else known)


def test_card_head_names_the_stage_agent(monkeypatch):
    agent_pipeline(monkeypatch)
    head = card_head({"title": "T", "metadata": {}}, "Draft", None).plain
    assert f"agent    {STAGE_AGENT}\n" in head


def test_card_head_uses_the_running_workers_stage_agent(monkeypatch):
    agent_pipeline(monkeypatch)
    running = {"title": "T", "metadata": {"worker": {"stage": "draft"}}}
    assert f"agent    {STAGE_AGENT}\n" in card_head(running, "Review", None).plain
    assert "agent    " not in card_head({"title": "T", "metadata": {}}, "Review", None).plain


def test_card_head_marks_an_unknown_agent(monkeypatch):
    agent_pipeline(monkeypatch, known={})
    card = {"title": "T", "metadata": {}}
    assert f"{STAGE_AGENT} (unknown)" in card_head(card, "Draft", None).plain

    def broken():
        raise ValueError("bad agent file")
    monkeypatch.setattr(cards.agent_defs, "load", broken)
    head = card_head(card, "Draft", None).plain
    assert STAGE_AGENT in head and "(unknown)" not in head


def test_card_head_without_agent_is_unchanged(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": False})
    cases = [({"title": "a", "metadata": {"worker": {"stage": "plan"}}}, "Spec ready"),
             ({"title": "b", "metadata": {}}, "Plan for review"),
             ({"title": "c", "metadata": {}}, "Manual")]
    heads = {}
    for name, pipe in (("engineering", pipeline.engineering()), ("none", None)):
        monkeypatch.setattr(C, "PIPELINE", pipe)
        heads[name] = [card_head(c, col, None).plain for c, col in cases]
        assert all("agent    " not in h for h in heads[name])
    assert heads["engineering"] == heads["none"]


def test_manual_card_with_agent_still_says_you(monkeypatch):
    agent_pipeline(monkeypatch, draft_column="Manual")
    head = card_head({"title": "T", "metadata": {}}, "Manual", None).plain
    assert "draft · you · yours to do" in head
    assert f"agent    {STAGE_AGENT}\n" in head


def test_card_head_shows_a_queued_move():
    head = card_head({"title": "t", "metadata": {}}, "Plan", None, queued="move to Done queued, resets 15:40").plain
    assert "queued   move to Done queued, resets 15:40" in head


def test_card_head_without_queue_is_unchanged():
    assert "queued" not in card_head({"title": "t", "metadata": {}}, "Plan", None).plain


def _rework_card(at_minutes_ago, active=True, prs=("https://github.com/o/r/pull/1",), now=1700000000.0):
    m = {"rework": {"active": active, "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - at_minutes_ago * 60))}}
    if prs:
        m["pr_urls"] = list(prs)
    return {"title": "T", "metadata": m}


def _pushed(minutes_ago, now=1700000000.0):
    return now - minutes_ago * 60


def test_card_head_says_the_finding_is_newer_than_the_last_push(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    head = card_head(_rework_card(20), "PR open", None, pushed=_pushed(30)).plain
    assert re.search(r"(?i)newer.{0,40}last push", head)
    assert "older" not in head.lower()


def test_card_head_says_the_finding_is_older_than_the_last_push(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    head = card_head(_rework_card(30), "PR open", None, pushed=_pushed(20)).plain
    assert re.search(r"(?i)older.{0,40}last push", head)
    assert "newer" not in head.lower()


def test_card_head_equal_times_count_as_newer(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    head = card_head(_rework_card(20), "PR open", None, pushed=_pushed(20)).plain
    assert re.search(r"(?i)newer.{0,40}last push", head)


def test_card_head_has_no_finding_line_without_rework_or_pr(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    no_rework = {"title": "T", "metadata": {"pr_urls": ["https://github.com/o/r/pull/1"]}}
    for c in (no_rework, _rework_card(20, active=False), _rework_card(20, prs=())):
        assert "last push" not in card_head(c, "PR open", None, pushed=_pushed(30)).plain


def test_card_head_says_the_push_time_is_unknown(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    c = _rework_card(20)
    head = card_head(c, "PR open", None, pushed=None).plain
    assert "last push time unknown" in head
    assert c["metadata"]["rework"]["at"] in head
    assert not re.search(r"(?i)newer|older|after|before", head.split("review", 1)[1].split("\n")[0])
