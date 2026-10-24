"""Tests for trigger execution: silence follow-ups, client card attaching, and daily card caps."""
import copy
from datetime import datetime, timezone

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
