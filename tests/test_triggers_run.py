"""Silence trigger runner: follow-up card created when no reply arrives in a column after a duration."""
import copy
from datetime import datetime, timezone

import pytest

from pl import config as C
from pl import trackers
from pl import triggers


class FakeTracker:
    def __init__(self, cols=None):
        self._cols = cols or {"Inbox": "col-inbox", "Proposal sent": "col-prop", "nudge": "col-nudge"}
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
[triggers.proposal_silence]
kind = "silence"
column = "Proposal sent"
after = "5d"
skill = "nudge"
connector = "email"
""")
    C.load("test")
    fake_t = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda name: fake_t)
    yield fake_t
    for k, v in saved.items():
        setattr(C, k, v)


def test_a_proposal_with_no_reply_after_5_days_gets_one_linked_followup(fake_env, monkeypatch):
    now = 1_700_000_000.0
    entered = now - (6 * 86400)   # 6 days ago (longer than 5d)

    prop = {
        "id": "your-org/your-repo#100",
        "title": "Enterprise contract proposal",
        "description": "# PIPELINE: INPUT\nProposal details\n",
        "tags": ["proposal"],
        "metadata": {
            "pipeline_mode": "auto",
            "entered_column_at": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
            "refs": {"thread": "th-abc-123"},
        },
        "list_id": "col-prop",
        "column": "Proposal sent",
        "updated_at": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
    }
    fake_env.items[prop["id"]] = prop

    tool_calls = []

    def fake_call(connector, tool, args, mode="trigger"):
        tool_calls.append((connector, tool, args, mode))
        return {
            "messages": [
                {
                    "direction": "outbound",
                    "date": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
                    "body": "Here is the proposal",
                }
            ]
        }

    monkeypatch.setattr(triggers, "call_tool", fake_call)

    created = triggers.run_due(now=now)

    assert len(created) == 1
    followup = created[0]
    assert followup["column"] == "nudge"
    assert "Enterprise contract proposal" in followup["title"]
    assert followup["metadata"]["followup_of"] == prop["id"]
    assert any(link.get("card") == prop["id"] and link.get("relation") == "followup-of"
               for link in followup["metadata"].get("links", []))
    assert fake_env.items[prop["id"]]["metadata"]["followup_of"] == followup["id"]
    assert tool_calls == [("email", "get_thread", {"thread_id": "th-abc-123"}, "trigger")]

    # Dedupe: second run creates no second followup
    second_created = triggers.run_due(now=now)
    assert len(second_created) == 0
    assert len(fake_env.created) == 1


def test_a_reply_inside_5_days_makes_no_followup(fake_env, monkeypatch):
    now = 1_700_000_000.0
    entered = now - (6 * 86400)   # entered 6 days ago
    reply_time = now - (2 * 86400)  # client replied 2 days ago (inside the 5-day silence window)

    prop = {
        "id": "your-org/your-repo#101",
        "title": "Services proposal",
        "description": "# PIPELINE: INPUT\nProposal text\n",
        "tags": ["proposal"],
        "metadata": {
            "pipeline_mode": "auto",
            "entered_column_at": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
            "refs": {"thread": "th-xyz-789"},
        },
        "list_id": "col-prop",
        "column": "Proposal sent",
        "updated_at": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
    }
    fake_env.items[prop["id"]] = prop

    def fake_call(connector, tool, args, mode="trigger"):
        return {
            "messages": [
                {
                    "direction": "outbound",
                    "date": datetime.fromtimestamp(entered, timezone.utc).isoformat(),
                },
                {
                    "direction": "inbound",
                    "date": datetime.fromtimestamp(reply_time, timezone.utc).isoformat(),
                    "body": "Thanks, reviewing now!",
                },
            ]
        }

    monkeypatch.setattr(triggers, "call_tool", fake_call)

    created = triggers.run_due(now=now)
    assert len(created) == 0
    assert len(fake_env.created) == 0
