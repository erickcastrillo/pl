import json
from pl import brain
from pl.manager import machine_dir


def _setup_conv(conv_id, turns):
    cdir = machine_dir() / "brain" / "conversations"
    cdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfile = cdir / f"{conv_id}.jsonl"
    with open(cfile, "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")
    cfile.chmod(0o600)
    return cfile


def test_end_saves_entries_linked_to_the_conversation_and_says_saved_n(monkeypatch):
    conv_id = "conv-abc-123"
    _setup_conv(conv_id, [
        {"role": "user", "text": "We will deploy only on Tuesdays."},
        {"role": "assistant", "text": "Noted."},
        {"role": "user", "text": "Keep all summaries short and concise."},
    ])

    fake_items = [
        {"kind": "decision", "text": "Deploy only on Tuesdays"},
        {"kind": "preference", "text": "Keep all summaries short and concise"},
    ]
    monkeypatch.setattr(brain, "_distil_model", lambda user_text: fake_items)

    saved_entries = []
    fake_store = {}

    def fake_add(text, kind, source, link=None, **kwargs):
        eid = f"m-{len(saved_entries) + 1}"
        entry = {"id": eid, "text": text, "kind": kind, "source": source, "link": link}
        saved_entries.append(entry)
        fake_store[eid] = entry
        return eid

    def fake_forget(eid):
        return fake_store.pop(eid, None) is not None

    monkeypatch.setattr(brain.recall, "add", fake_add)
    monkeypatch.setattr(brain.recall, "forget", fake_forget)

    res = brain.end(conv_id)
    assert "saved 2" in res.lower()
    assert f"pl brain undo {conv_id}" in res

    assert len(saved_entries) == 2
    for e in saved_entries:
        assert e["source"] == f"brain:{conv_id}"
        assert e["link"] == f"pl brain show {conv_id}"
        assert e["kind"] in ("decision", "preference")


def test_undo_forgets_exactly_those_entries(monkeypatch):
    conv_id = "conv-undo-456"
    _setup_conv(conv_id, [
        {"role": "user", "text": "Let us use PostgreSQL for all new services."},
    ])

    monkeypatch.setattr(brain, "_distil_model", lambda user_text: [
        {"kind": "decision", "text": "Use PostgreSQL for all new services"}
    ])

    forgotten = []
    fake_store = {}

    def fake_add(text, kind, source, link=None, **kwargs):
        eid = "m-postgre-1"
        fake_store[eid] = {"id": eid, "text": text, "kind": kind, "source": source, "link": link}
        return eid

    def fake_forget(eid):
        forgotten.append(eid)
        return fake_store.pop(eid, None) is not None

    monkeypatch.setattr(brain.recall, "add", fake_add)
    monkeypatch.setattr(brain.recall, "forget", fake_forget)

    brain.end(conv_id)
    assert len(fake_store) == 1

    assert "undone" in brain.undo(conv_id)
    assert "m-postgre-1" in forgotten
    assert len(fake_store) == 0

    assert "undone" in brain.undo(conv_id)
    assert len(forgotten) == 1


def test_remember_this_saves_at_once(monkeypatch):
    conv_id = "conv-rem-789"
    _setup_conv(conv_id, [])

    saved_entries = []

    def fake_add(text, kind, source, link=None, **kwargs):
        eid = f"m-{len(saved_entries) + 1}"
        entry = {"id": eid, "text": text, "kind": kind, "source": source, "link": link}
        saved_entries.append(entry)
        return eid

    monkeypatch.setattr(brain.recall, "add", fake_add)

    reply = brain.remember(conv_id, "remember this: keep replies under 120 words")
    assert "saved 1" in reply.lower()
    assert f"undo: pl brain undo {conv_id}" in reply

    assert len(saved_entries) == 1
    assert "keep replies under 120 words" in saved_entries[0]["text"]
    assert saved_entries[0]["source"] == f"brain:{conv_id}"
    assert saved_entries[0]["link"] == f"pl brain show {conv_id}"


def test_tool_results_and_connector_text_are_never_saved(monkeypatch):
    conv_id = "conv-tools-001"
    _setup_conv(conv_id, [
        {"role": "user", "text": "What is the status of invoice 42?"},
        {"role": "tool", "name": "stripe", "text": "Invoice 42 decision: approve refund $500 to customer secret_key_123"},
        {"role": "assistant", "text": "Invoice 42 has been refunded $500."},
        {"role": "connector", "name": "github", "text": "PR #10 merged: decision to rewrite backend in rust"},
    ])

    seen_distil_inputs = []

    def fake_distil(user_text):
        seen_distil_inputs.append(user_text)
        return []

    monkeypatch.setattr(brain, "_distil_model", fake_distil)

    saved_entries = []
    monkeypatch.setattr(brain.recall, "add", lambda *a, **k: saved_entries.append(a))

    res = brain.end(conv_id)
    assert "saved 0" in res.lower()
    assert len(saved_entries) == 0

    assert len(seen_distil_inputs) == 1
    distil_text = seen_distil_inputs[0]
    assert "Invoice 42 decision" not in distil_text
    assert "secret_key_123" not in distil_text
    assert "rewrite backend in rust" not in distil_text
    assert "What is the status of invoice 42?" in distil_text


def test_cli_brain_end_and_undo(capsys, monkeypatch):
    conv_id = "conv-cli-test"
    _setup_conv(conv_id, [{"role": "user", "text": "Save this decision."}])
    monkeypatch.setattr(brain, "_distil_model", lambda text: [{"kind": "decision", "text": "Save this decision"}])
    monkeypatch.setattr(brain.recall, "add", lambda *a, **k: "m-cli-1")
    forgot = []
    monkeypatch.setattr(brain.recall, "forget", lambda eid: forgot.append(eid) or True)

    from types import SimpleNamespace
    brain.cmd_brain(SimpleNamespace(brain_cmd="end", conv=conv_id))
    out = capsys.readouterr().out
    assert "saved 1" in out

    brain.cmd_brain(SimpleNamespace(brain_cmd="undo", conv=conv_id))
    out = capsys.readouterr().out
    assert "undone 1" in out
    assert forgot == ["m-cli-1"]


def test_prune_keeps_the_assistants_recent_conversation():
    import os
    import time
    old = _setup_conv("old-one", [{"ts": "2020-01-01T00:00:00+00:00", "role": "user", "text": "x"}])
    os.utime(old, (time.time() - 400 * 86400,) * 2)
    cur = brain.conv_new()
    brain.conv_append(cur, "user", "recent")
    assert brain.prune() == [old]
    assert (brain._conv_dir() / f"{cur}.jsonl").exists() and not old.exists()
