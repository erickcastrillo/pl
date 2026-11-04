"""Tests for the append-only audit log (audit.py) and hooks."""
import json
import os
import stat
import threading
from datetime import datetime

import pytest

from pl import audit, commands, config as C, connectors_proxy
from pl.trackers import _marked


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "PL_STAGE", "PL_ASSISTANT", "PL_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(C, "USER_EMAIL", "owner@example.com")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _audit_lines():
    p = C.STATE_DIR / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_card_write_connector_call_and_approval_each_append_one_entry_with_who_what_when_session(monkeypatch):
    monkeypatch.setenv("PL_SESSION", "sess-audit-test")

    # 1. Card write through _marked
    fake_tracker_update = _marked(lambda card_id, **kw: {"id": card_id, **kw}, name="update")
    fake_tracker_update("card-101", title="Title only", description="Desc")

    # 2. Connector call through connectors_proxy
    connectors_proxy.record_tool_call("gh-connector", "send_message", access="write", result="ok")

    # 3. Approval through cmd_approve
    card = {"id": "card-102", "title": "Card 102", "list_id": "L1", "description": "## PLAN\nWP1", "metadata": {}}
    monkeypatch.setattr(commands, "find_card", lambda cid: card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Plan for review")
    monkeypatch.setattr(commands, "col_id", lambda n: "col-approved")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a: None)
    monkeypatch.setattr(commands, "plan_path", lambda c: C.STATE_DIR / "nope.md")
    commands.cmd_approve(type("A", (), {"id": "card-102", "force": False})())

    entries = _audit_lines()
    assert len(entries) == 3

    for entry in entries:
        assert entry.get("who") == "owner@example.com"
        assert "what" in entry and entry["what"]
        assert "when" in entry and datetime.fromisoformat(entry["when"]).utcoffset() is not None
        assert entry.get("session") == "sess-audit-test"
        assert "host" in entry
        assert "hash" in entry

    assert entries[0]["card"] == "card-101"
    assert "card.update" in entries[0]["what"]
    assert entries[1]["target"] == "gh-connector"
    assert "connector.tool" in entries[1]["what"]
    assert entries[2]["card"] == "card-102"
    assert "approved" in entries[2]["what"]


def test_entries_hold_no_field_values_or_email_addresses(monkeypatch):
    secret_val = "SUPER_SECRET_PAYLOAD_12345"
    contact_email = "victim-contact@example.org"

    # Card write with values and contact email
    fake_update = _marked(lambda cid, **kw: {"id": cid, **kw}, name="update")
    fake_update("card-secret", description=f"Contains {secret_val} and {contact_email}", metadata={"contact": contact_email})

    # Direct append with detail containing values
    audit.append("card.custom", card="card-custom", detail={"secret_field": secret_val, "contact": contact_email})

    text = (C.STATE_DIR / "audit.jsonl").read_text()
    assert secret_val not in text
    assert contact_email not in text
    # Field names should be present
    assert "description" in text
    assert "metadata" in text
    assert "secret_field" in text
    assert "contact" in text


def test_editing_a_line_breaks_verify():
    audit.append("action1", card="c1")
    audit.append("action2", card="c2")
    audit.append("action3", card="c3")

    assert audit.verify() is True

    # Tamper with the file
    path = C.STATE_DIR / "audit.jsonl"
    lines = path.read_text().splitlines()
    bad_entry = json.loads(lines[1])
    bad_entry["what"] = "tampered"
    lines[1] = json.dumps(bad_entry)
    path.write_text("\n".join(lines) + "\n")

    assert audit.verify() is False


def test_concurrent_writers_keep_the_chain():
    n_threads = 6
    per_thread = 10
    barrier = threading.Barrier(n_threads)

    def worker(tid):
        barrier.wait()
        for i in range(per_thread):
            audit.append(f"action-{tid}-{i}", card=f"card-{tid}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = _audit_lines()
    assert len(entries) == n_threads * per_thread
    assert audit.verify() is True


def test_the_file_is_0600():
    audit.append("initial_action", card="c1")
    path = C.STATE_DIR / "audit.jsonl"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_who_attribution(monkeypatch):
    monkeypatch.setenv("PL_STAGE", "run")
    assert audit.who() == "agent:run"

    monkeypatch.delenv("PL_STAGE", raising=False)
    monkeypatch.setenv("PL_ASSISTANT", "1")
    assert audit.who() == "assistant"

    monkeypatch.delenv("PL_ASSISTANT", raising=False)
    monkeypatch.setenv("PL_MCP", "1")
    assert audit.who() == "mcp-session"


def test_audit_cli_verify(capsys):
    audit.append("act1")
    audit.append("act2")
    # Verify valid chain
    audit.cmd_audit(type("A", (), {"verify": True, "json": False, "since": None})())
    out, _ = capsys.readouterr()
    assert "chain intact" in out

    # Break chain
    path = C.STATE_DIR / "audit.jsonl"
    lines = path.read_text().splitlines()
    bad = json.loads(lines[0])
    bad["what"] = "tampered"
    path.write_text(json.dumps(bad) + "\n" + lines[1] + "\n")

    with pytest.raises(SystemExit) as exc:
        audit.cmd_audit(type("A", (), {"verify": True, "json": False, "since": None})())
    assert exc.value.code == 1


def test_connectors_commands_audit(monkeypatch):
    from pl import connectors
    monkeypatch.setattr(connectors, "add", lambda *a, **k: {"approved_snapshot": {}})
    monkeypatch.setattr(connectors, "revoke", lambda name: None)
    monkeypatch.setattr(connectors, "approve_tools", lambda name, **k: audit.append("connectors.approve_tools", target=name))
    def ns(**kw):
        return type("A", (), kw)()
    connectors.cmd_connectors(ns(connectors_cmd="add", name="slack", command=None, url="https://x.example", pin=None, yes=True))
    connectors.cmd_connectors(ns(connectors_cmd="revoke", name="slack", yes=True))
    connectors.cmd_connectors(ns(connectors_cmd="approve-tools", name="slack", yes=True))

    entries = _audit_lines()
    assert len(entries) == 3
    assert entries[0]["what"] == "connectors.add" and entries[0]["target"] == "slack"
    assert entries[1]["what"] == "connectors.revoke" and entries[1]["target"] == "slack"
    assert entries[2]["what"] == "connectors.approve_tools" and entries[2]["target"] == "slack"
