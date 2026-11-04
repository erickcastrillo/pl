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


def test_a_recipient_outside_the_domain_is_flagged_and_needs_a_second_confirmation(monkeypatch, tmp_path):
    from pl import outbound

    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    card = {
        "id": "card-104",
        "title": "Lead inquiry",
        "list_id": "L1",
        "description": "# PIPELINE: REPLY\nTo: eve@attacker.com\nSubject: Re: Lead\n\nSensitive info",
        "metadata": {
            "from": "lead@client-domain.com",
            "contact_emails": ["partner@client-domain.com", "advisor@allowed.com"],
            "refs": {"thread": "th-104"},
        },
    }

    flagged = outbound.flagged_recipients(card)
    assert flagged == ["outside: eve@attacker.com"]

    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Owner gate")
    monkeypatch.setattr(commands, "col_id", lambda name: "col-approved")
    updated = []
    monkeypatch.setattr(commands, "update", lambda *a, **k: updated.append((a, k)))
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)

    # Approving without --confirm raises SystemExit
    with pytest.raises(SystemExit) as exc:
        commands.cmd_approve(argparse.Namespace(id="card-104", force=False, confirm=False))
    assert "outside: eve@attacker.com" in str(exc.value)
    assert "--confirm" in str(exc.value)
    assert len(updated) == 0

    # Approving with --confirm succeeds
    commands.cmd_approve(argparse.Namespace(id="card-104", force=False, confirm=True))
    assert len(updated) > 0


def test_the_sender_himself_is_not_flagged(monkeypatch, tmp_path):
    from pl import outbound

    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    card = {
        "id": "card-105",
        "title": "Lead inquiry",
        "list_id": "L1",
        "description": "# PIPELINE: REPLY\nTo: alice@client-domain.com, colleague@client-domain.com\nSubject: Re: Lead\n\nHello",
        "metadata": {
            "from": "alice@client-domain.com",
            "refs": {"thread": "th-105"},
        },
    }

    flagged = outbound.flagged_recipients(card)
    assert flagged == []

    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Owner gate")
    monkeypatch.setattr(commands, "col_id", lambda name: "col-approved")
    updated = []
    monkeypatch.setattr(commands, "update", lambda *a, **k: updated.append((a, k)))
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)

    # Approving without --confirm succeeds directly
    commands.cmd_approve(argparse.Namespace(id="card-105", force=False, confirm=False))
    assert len(updated) > 0


def _cap_setup(monkeypatch, tmp_path):
    from pl import outbound, util

    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    monkeypatch.setattr(outbound, "_clock", lambda: fake_now[0])
    monkeypatch.setattr(outbound, "get_send_cap", lambda conn: 2)
    sent_calls, sent, direct = [], [], []
    monkeypatch.setattr(outbound, "call_tool", lambda c, t, a: sent_calls.append((c, t, a)))
    monkeypatch.setattr("pl.notifications.notify", lambda t, m: sent.append((t, m)))
    monkeypatch.setattr(util, "notify", lambda t, m: direct.append((t, m)))
    for n, to in enumerate(("a", "b", "c"), 1):
        card = {"id": f"c{n}", "list_id": "L1", "description": f"# PIPELINE: REPLY\nTo: {to}@ex.com\n\n{n}",
                "metadata": {"connector": "email"}}
        outbound.enqueue(card, now=1000.0)
    fake_now[0] = 1400.0
    return fake_now, sent_calls, sent, direct


def test_send_cap_with_alert_opened_off_sends_nothing(monkeypatch, tmp_path):
    from pl import alerts, outbound

    _, _, sent, direct = _cap_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(C, "NOTIFY", {"events": ["merged"]})
    outbound.send_due()
    assert sent == [] and direct == []
    assert not (tmp_path / "machine" / "chat-outbox.jsonl").exists()
    assert alerts.get("send_cap:email") is not None


def test_sends_past_the_daily_cap_wait_and_tell_the_owner(monkeypatch, tmp_path):
    from pl import alerts, outbound

    fake_now, sent_calls, sent, direct = _cap_setup(monkeypatch, tmp_path)
    outbound.send_due()
    assert [c[2]["to"] for c in sent_calls] == ["a@ex.com", "b@ex.com"]
    items = outbound._load(outbound.queue_path())
    assert [i["status"] for i in items] == ["sent", "sent", "waiting_cap"]
    assert alerts.get("send_cap:email") is not None
    assert len(sent) == 1 and "send cap" in sent[0][0].lower()
    assert direct == []
    fake_now[0] += 60
    outbound.send_due()
    assert len(sent) == 1


