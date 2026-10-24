"""Tests for trigger execution: silence follow-ups, client card attaching, and daily card caps."""
import copy
from datetime import datetime, timezone
import json

import pytest

from pl import config as C
from pl import trackers


class FakeTracker:
    def __init__(self, cols=None):
        self._cols = cols or {"Inbox": "col-inbox", "Proposal sent": "col-prop", "nudge": "col-nudge", "Done": "col-done"}
        self.items, self.created, self.updates = {}, [], []

    def columns(self):
        return dict(self._cols)

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        cid = f"your-org/your-repo#{len(self.items) + 1}"
        col_id = self._cols.get(column, f"col-{column}")
        c = {
            "id": cid,
            "title": title,
            "description": description,
            "tags": list(tags),
            "metadata": dict(metadata or {}),
            "list_id": col_id,
            "column": column,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "assigned_to": assigned_to,
        }
        self.items[cid] = c
        self.created.append(c)
        return copy.deepcopy(c)

    def update(self, item_id, *, verify=True, **fields):
        c = self.items[item_id]
        self.updates.append((item_id, fields))
        if "metadata" in fields:
            c["metadata"] = {**c.get("metadata", {}), **fields.pop("metadata")}
        c.update(fields)
        return copy.deepcopy(c)


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    prof = tmp_path / ".pl-test"
    prof.mkdir()
    (prof / "config.toml").write_text("""
[triggers.sales_mail]
connector = "email"
query = "label:sales is:unread"
every = "5m"
pipeline = "sales"
attach_to = ["sales", "clients"]
cap = 20
""")
    C.load("test")
    fake_t = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda name: fake_t)
    yield fake_t
    for k, v in saved.items():
        setattr(C, k, v)


def test_a_message_from_a_linked_client_is_attached_not_a_new_card(fake_env, monkeypatch):
    from pl import triggers

    client_card = {
        "id": "your-org/your-repo#42",
        "title": "Existing Client Lead",
        "description": "# PIPELINE: INPUT\nExisting client inquiry\n",
        "tags": ["lead"],
        "metadata": {
            "pipeline_mode": "auto",
            "contact_emails": ["alice@example.com"],
            "refs": {"email": "msg-001", "thread": "th-100"},
        },
        "list_id": "col-inbox",
        "column": "Inbox",
        "updated_at": "2026-10-23T12:00:00+00:00",
    }
    fake_env.items[client_card["id"]] = client_card

    def fake_call(connector, tool, args, mode="trigger"):
        return {
            "messages": [
                {
                    "id": "msg-002",
                    "from": "Alice Smith <ALICE@EXAMPLE.COM>",
                    "subject": "Follow-up question on pricing",
                    "date": "2026-10-23T14:00:00+00:00",
                    "thread_id": "th-100",
                    "link": "https://mail.google.com/mail/u/0/#inbox/th-100",
                    "snippet": "Can you send the pricing sheet?",
                }
            ]
        }

    monkeypatch.setattr(triggers, "call_tool", fake_call)

    created = triggers.run_due()
    assert len(created) == 0
    assert len(fake_env.created) == 0

    updated = fake_env.items[client_card["id"]]
    assert updated["metadata"]["refs"]["email_more"] == ["msg-002"]
    desc = updated["description"]
    assert "## Messages" in desc
    assert "- 2026-10-23T14:00:00+00:00: Follow-up question on pricing (https://mail.google.com/mail/u/0/#inbox/th-100)" in desc


def test_replay_rejects_a_bad_window_with_exit_2(fake_env, monkeypatch):
    from pl import triggers
    import argparse

    for bad in ["bad", "-5d", "0d", "10x", "", None]:
        with pytest.raises(SystemExit) as exc:
            triggers.cmd_triggers(argparse.Namespace(triggers_cmd="test", name="sales_mail", replay=bad, json=False))
        assert exc.value.code == 2


def test_replay_lists_cards_it_would_create_and_writes_nothing(fake_env, monkeypatch, capsys):
    from pl import triggers
    import argparse

    # 1. Setup existing cards on tracker:
    # An open card with contact_emails set to client@example.com (for attach)
    card_client = {
        "id": "your-org/your-repo#10",
        "title": "Existing Client Deal",
        "description": "## INPUT\nClient info",
        "tags": ["client"],
        "metadata": {
            "pipeline_mode": "auto",
            "contact_emails": ["client@example.com"],
            "refs": {},
        },
        "list_id": "col-sales",
        "column": "Inbox",
    }
    # An existing card already created from email msg-old-123 (for dedupe)
    card_old = {
        "id": "your-org/your-repo#11",
        "title": "Old inquiry",
        "description": "## INPUT\nInquiry",
        "tags": ["email"],
        "metadata": {
            "pipeline_mode": "auto",
            "refs": {"email": "msg-old-123"},
        },
        "list_id": "col-sales",
        "column": "Inbox",
    }
    fake_env.items[card_client["id"]] = card_client
    fake_env.items[card_old["id"]] = card_old

    # 2. Setup triggers.json with recent_ids (second dedupe layer)
    state_file = C.STATE_DIR / "triggers.json"
    state_data = {"recent_ids": ["msg-recent-456"], "cards_today": {}}
    state_file.write_text(json.dumps(state_data))
    before_state = state_file.read_text()

    # 3. Mock connector call_tool
    tool_calls = []

    def fake_call(connector, tool, args, mode="trigger"):
        tool_calls.append((connector, tool, args, mode))
        return {
            "messages": [
                # 1. New lead -> would create
                {
                    "id": "msg-new-1",
                    "from": "New Lead <lead@example.com>",
                    "subject": "Inquiry about enterprise tier",
                    "date": "2026-10-20T10:00:00Z",
                    "snippet": "We want to buy licenses.",
                },
                # 2. Existing client -> would attach to card #10
                {
                    "id": "msg-attach-2",
                    "from": "Client Person <client@example.com>",
                    "subject": "Follow up on contract",
                    "date": "2026-10-21T11:00:00Z",
                    "snippet": "Can you check the draft?",
                },
                # 3. Already on board (refs.email) -> deduped
                {
                    "id": "msg-old-123",
                    "from": "Stranger <stranger@example.com>",
                    "subject": "Already handled",
                    "date": "2026-10-19T09:00:00Z",
                    "snippet": "Old message.",
                },
                # 4. In recent_ids -> deduped
                {
                    "id": "msg-recent-456",
                    "from": "Other <other@example.com>",
                    "subject": "Recent message",
                    "date": "2026-10-22T08:00:00Z",
                    "snippet": "Already seen.",
                },
            ]
        }

    monkeypatch.setattr(triggers, "call_tool", fake_call)

    # Run replay via CLI handler
    triggers.cmd_triggers(argparse.Namespace(triggers_cmd="test", name="sales_mail", replay="7d", json=False))

    captured = capsys.readouterr()
    out = captured.out

    # Output asserts
    assert "would create" in out
    assert "New Lead: Inquiry about enterprise tier" in out or "Inquiry about enterprise tier" in out
    assert "would attach to" in out
    assert "your-org/your-repo#10" in out
    assert "msg-old-123" not in out
    assert "msg-recent-456" not in out

    # Assert search called with mode="trigger" and query
    assert len(tool_calls) == 1
    assert tool_calls[0][0] == "email"
    assert tool_calls[0][1] == "search"
    assert tool_calls[0][2]["query"] == "label:sales is:unread"
    assert "since" in tool_calls[0][2]
    assert tool_calls[0][3] == "trigger"

    # Writes nothing asserts
    assert len(fake_env.created) == 0, "must not create cards on tracker"
    assert len(fake_env.updates) == 0, "must not update cards on tracker"
    assert state_file.read_text() == before_state, "triggers.json must be unchanged"

    # Test --json as well
    triggers.cmd_triggers(argparse.Namespace(triggers_cmd="test", name="sales_mail", replay="7d", json=True))
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert any("would create" in str(v).lower() for v in data.values()) or "would_create" in data
    assert any("would attach" in str(v).lower() for v in data.values()) or "would_attach" in data
    assert len(fake_env.created) == 0
    assert len(fake_env.updates) == 0
    assert state_file.read_text() == before_state
