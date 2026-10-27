"""Needs-you "to send" groups for the pipeline's outward stages, and approving them into the outbound queue."""
import argparse
import json
from types import SimpleNamespace

import pytest

from pl import commands, confirm, outbound, outward, pipeline
from pl import config as C
from pl.tui import dashboard, needs, review

NOW = 5000.0
DRAFT = "# PIPELINE: PROPOSAL\nTo: lead@acme.com\nSubject: Proposal\n\nHere is the proposal."


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(outbound, "_clock", lambda: NOW)
    monkeypatch.setattr(outward, "state", lambda: {"on": False, "since": None, "by": None})
    monkeypatch.delenv("PL_ASSISTANT", raising=False)
    monkeypatch.delenv("PL_SOURCE", raising=False)
    yield
    for k, v in saved.items():
        setattr(C, k, v)


def use(monkeypatch, p):
    monkeypatch.setattr(C, "PIPELINE", p)
    return p


def new_client(monkeypatch):
    return use(monkeypatch, pipeline.templates()["new-client"])


def stage(section, column, done, **kw):
    return {"column": column, "done": done, "skill": "pl-stage", "section": section, "gate": "owner", **kw}


def two_outward(monkeypatch):
    return use(monkeypatch, {"name": "t", "template": "t", "columns": ["A", "Reply review", "B", "Proposal review", "Done"], "stages": {
        "reply": stage("REPLY", "A", "Reply review", outward=True),
        "proposal": stage("PROPOSAL", "B", "Proposal review", outward=True)}})


def card(cid="card-1", desc=DRAFT, **meta):
    return {"id": cid, "title": "Acme", "list_id": "L", "description": desc, "tags": [],
            "metadata": {"from": "lead@acme.com", **meta}}


def row(c, col):
    return {"card": c, "col": col, "kind": "idle"}


def fake_board(monkeypatch, col, c):
    moved = []
    monkeypatch.setattr(commands, "find_card", lambda cid: c)
    monkeypatch.setattr(commands, "col_name", lambda lid: col)
    monkeypatch.setattr(commands, "col_id", lambda name: f"id-{name}")
    monkeypatch.setattr(commands, "update", lambda cid, **kw: moved.append(kw))
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    return moved


def approve(**kw):
    commands.cmd_approve(argparse.Namespace(**{"id": "card-1", "force": False, "confirm": False, **kw}))


def queue():
    p = outbound.queue_path()
    return json.loads(p.read_text()) if p.exists() else []


def test_parse_reply_reads_the_named_section():
    c = card(desc=DRAFT + "\n# PIPELINE: REPLY\nTo: r@acme.com\n\nreply body")
    got = outbound.parse_reply(c, "PROPOSAL")
    assert (got["to"], got["subject"], got["body"]) == ("lead@acme.com", "Proposal", "Here is the proposal.")
    assert outbound.parse_reply(c)["to"] == "r@acme.com"


def test_send_due_sends_nothing_while_the_kill_switch_is_on(monkeypatch):
    outbound.enqueue(card(), "PROPOSAL")
    monkeypatch.setattr(outward, "state", lambda: {"on": True})
    calls = []
    monkeypatch.setattr(outbound, "call_tool", lambda *a: calls.append(a))
    assert outbound.send_due(now=NOW + 1000) == []
    assert calls == [] and queue()[0]["status"] == "queued"


def test_approving_a_proposal_queues_one_outbound_item(monkeypatch, capsys):
    new_client(monkeypatch)
    moved = fake_board(monkeypatch, "Proposal review", card())
    approve()
    assert moved[0]["list_id"] == "id-Onboarding"
    [item] = queue()
    assert item["status"] == "queued" and item["not_before"] == NOW + 300
    assert item["args"]["to"] == "lead@acme.com" and item["args"]["body"] == "Here is the proposal."
    assert "queued to send after the undo window" in capsys.readouterr().out


def test_outside_recipient_is_refused_without_confirm_and_queues_nothing(monkeypatch):
    new_client(monkeypatch)
    moved = fake_board(monkeypatch, "Proposal review", card(desc=DRAFT.replace("lead@acme.com", "other@else.com")))
    with pytest.raises(commands.BadInput, match="outside: other@else.com"):
        approve()
    assert moved == [] and queue() == []
    approve(confirm=True)
    assert len(queue()) == 1


def test_a_proposal_without_a_recipient_is_refused(monkeypatch):
    new_client(monkeypatch)
    c = card(desc="# PIPELINE: PROPOSAL\nJust text")
    c["metadata"] = {}
    fake_board(monkeypatch, "Proposal review", c)
    with pytest.raises(commands.BadInput, match="no recipient"):
        approve()
    assert queue() == []


def test_kill_switch_on_approves_and_says_the_action_waits(monkeypatch, capsys):
    new_client(monkeypatch)
    fake_board(monkeypatch, "Proposal review", card())
    monkeypatch.setattr(outward, "state", lambda: {"on": True})
    approve()
    assert "its outward action waits: the kill switch is on" in capsys.readouterr().out
    assert len(queue()) == 1
    assert outbound.send_due(now=NOW + 1000) == []


def test_agent_session_approval_of_a_proposal_is_held(monkeypatch):
    new_client(monkeypatch)
    moved = fake_board(monkeypatch, "Proposal review", card())
    monkeypatch.setenv("PL_ASSISTANT", "1")
    monkeypatch.setattr(confirm, "new_code", lambda c: "123456")
    monkeypatch.setattr(confirm, "send_to_chat", lambda t: None)
    approve()
    assert len(moved) == 1 and moved[0]["metadata"]["approval_pending"] and "list_id" not in moved[0]
    assert queue() == []


def test_non_outward_gate_approval_queues_nothing(monkeypatch):
    new_client(monkeypatch)
    moved = fake_board(monkeypatch, "Qualify review", card())
    approve()
    assert moved[0]["list_id"] == "id-Proposal" and queue() == []


def test_engineering_groups_are_unchanged(monkeypatch):
    use(monkeypatch, None)
    rows = [row(card(), "Plan for review")]
    assert tuple(needs.needs_groups(rows)) == needs.GROUPS + ("merge",)
    assert needs.send_groups() == []
    assert [r[0] for r in dashboard._groups(rows)] == ["merge", "specs", "plans", "rework", "decide", "manual"]


def test_new_client_shows_proposals_to_send(monkeypatch):
    new_client(monkeypatch)
    sent, qualify = row(card("c1"), "Proposal review"), row(card("c2"), "Qualify review")
    g = needs.needs_groups([sent, qualify])
    assert g["send:PROPOSAL"] == [sent]
    assert needs.send_groups() == [("send:PROPOSAL", "Proposals to send")]
    data = {"snapshot": {"rows": [sent, qualify]}}
    assert "Proposals to send · 1" in [r[2] for r in dashboard.decide_rows(data)]


def test_two_outward_sections_make_two_groups_each_card_once(monkeypatch):
    two_outward(monkeypatch)
    a, b = row(card("c1"), "Reply review"), row(card("c2"), "Proposal review")
    assert [t for _, t in needs.send_groups()] == ["Replies to send", "Proposals to send"]
    g = needs.needs_groups([a, b])
    assert g["send:REPLY"] == [a] and g["send:PROPOSAL"] == [b]


def test_detail_pane_shows_the_draft_and_recipient(monkeypatch):
    new_client(monkeypatch)
    text = needs.detail(row(card(), "Proposal review")).plain
    assert "to       lead@acme.com" in text and "subject  Proposal" in text and "Here is the proposal." in text


def test_configured_pipeline_has_no_engineering_groups(monkeypatch):
    for setup in (new_client, two_outward):
        setup(monkeypatch)
        r = row(card(), "Proposal review")
        assert all(k.startswith("send:") for k, *_ in dashboard._groups([r]))
        assert needs.needs_me(r) is (setup is new_client or setup is two_outward)


def test_review_group_overrides_the_title(monkeypatch):
    p = new_client(monkeypatch)
    use(monkeypatch, {**p, "stages": {**p["stages"], "proposal": {**p["stages"]["proposal"], "review_group": "Quotes to send"}}})
    assert needs.send_groups() == [("send:PROPOSAL", "Quotes to send")]


class FakeApp:
    def __init__(self):
        self.screens = []

    def push_screen(self, screen, callback=None):
        self.screens.append((screen, callback))

    def call_from_thread(self, fn, *a, **k):
        return fn(*a, **k)

    def notify(self, *a, **k):
        pass


@pytest.mark.parametrize("yes", [True, False])
def test_console_approve_names_outside_recipients_and_passes_confirm(monkeypatch, yes):
    app, calls = FakeApp(), []
    monkeypatch.setattr(commands, "cmd_approve", lambda a: calls.append(a))
    monkeypatch.setattr(review, "run_command", lambda app, fn, after=None, kind=None, **kw: fn(SimpleNamespace(**kw)))
    review.confirm_and_approve(app, "card-1", "Acme", "send", outside=["other@else.com"])
    screen, callback = app.screens[0]
    assert "Outside recipients: other@else.com" in screen.message
    callback(yes)
    assert (len(calls) == 1 and calls[0].confirm is True) if yes else calls == []
