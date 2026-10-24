import argparse
import time
import pytest
from pl import commands, config as C


def test_approval_sends_once_after_the_undo_window(monkeypatch, tmp_path):
    from pl import outbound

    # Set up config state dir
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    monkeypatch.setattr(outbound, "_clock", lambda: fake_now[0])

    card = {
        "id": "card-101",
        "title": "Lead inquiry",
        "list_id": "L1",
        "description": "# PIPELINE: REPLY\nTo: client@example.com\nSubject: Re: Lead\n\nThanks for reaching out!",
        "metadata": {
            "from": "client@example.com",
            "refs": {"thread": "th-abc"},
            "connector": "email",
        },
    }

    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Owner gate")
    monkeypatch.setattr(commands, "col_id", lambda name: "col-approved")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)

    sent_calls = []
    def fake_call_tool(connector, tool, args):
        sent_calls.append((connector, tool, args))
        return {"status": "ok"}
    monkeypatch.setattr(outbound, "call_tool", fake_call_tool)

    # Approve the card (undo window is default 5 minutes = 300s)
    args = argparse.Namespace(id="card-101", force=False, confirm=False)
    commands.cmd_approve(args)

    # At t = 1000: dispatch pass runs; window not expired (not_before = 1300)
    outbound.send_due()
    assert len(sent_calls) == 0

    # At t = 1299: still in undo window
    fake_now[0] = 1299.0
    outbound.send_due()
    assert len(sent_calls) == 0

    # At t = 1301: after the undo window, sends once
    fake_now[0] = 1301.0
    outbound.send_due()
    assert len(sent_calls) == 1
    assert sent_calls[0] == (
        "email",
        "send_draft",
        {
            "thread": "th-abc",
            "to": "client@example.com",
            "subject": "Re: Lead",
            "body": "Thanks for reaching out!",
        },
    )

    # Next dispatcher pass at t = 1305: does not send again
    fake_now[0] = 1305.0
    outbound.send_due()
    assert len(sent_calls) == 1


def test_cancel_within_the_window_sends_nothing(monkeypatch, tmp_path):
    from pl import outbound

    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    monkeypatch.setattr(outbound, "_clock", lambda: fake_now[0])

    card = {
        "id": "card-102",
        "title": "Lead inquiry",
        "list_id": "L1",
        "description": "# PIPELINE: REPLY\nTo: client@example.com\nSubject: Re: Lead\n\nThanks!",
        "metadata": {"from": "client@example.com", "refs": {"thread": "th-123"}},
    }

    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Owner gate")
    monkeypatch.setattr(commands, "col_id", lambda name: "col-approved")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)

    sent_calls = []
    monkeypatch.setattr(outbound, "call_tool", lambda c, t, a: sent_calls.append((c, t, a)))

    # Approve
    commands.cmd_approve(argparse.Namespace(id="card-102", force=False, confirm=False))

    # Cancel at t = 1050 (within 300s window)
    fake_now[0] = 1050.0
    cancel_args = argparse.Namespace(action="cancel", id="card-102")
    commands.cmd_outbox(cancel_args)

    # After window expires at t = 1350, dispatcher pass runs
    fake_now[0] = 1350.0
    outbound.send_due()

    # Nothing should be sent
    assert len(sent_calls) == 0


def test_a_crash_between_sending_and_sent_never_sends_twice(monkeypatch, tmp_path):
    from pl import outbound

    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    monkeypatch.setattr(outbound, "_clock", lambda: fake_now[0])

    card = {
        "id": "card-103",
        "title": "Lead inquiry",
        "list_id": "L1",
        "description": "# PIPELINE: REPLY\nTo: client@example.com\nSubject: Re: Lead\n\nHello",
        "metadata": {"from": "client@example.com", "refs": {"thread": "th-crash"}},
    }

    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Owner gate")
    monkeypatch.setattr(commands, "col_id", lambda name: "col-approved")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)

    # Approve
    commands.cmd_approve(argparse.Namespace(id="card-103", force=False, confirm=False))

    # Advance clock past undo window
    fake_now[0] = 1400.0

    call_count = [0]
    def crashing_call_tool(connector, tool, args):
        call_count[0] += 1
        raise RuntimeError("Crash during send!")

    monkeypatch.setattr(outbound, "call_tool", crashing_call_tool)

    # First pass: runs and crashes
    with pytest.raises(RuntimeError, match="Crash during send!"):
        outbound.send_due()

    assert call_count[0] == 1

    # Second pass (process recovered / next pass): item was marked "sending", must never be sent twice
    monkeypatch.setattr(outbound, "call_tool", lambda c, t, a: call_count.__setitem__(0, call_count[0] + 1))
    outbound.send_due()

    assert call_count[0] == 1
