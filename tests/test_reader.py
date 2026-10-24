import json
from pathlib import Path
import pytest

from pl import board, config as C
from pl.board import render, sections


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir(parents=True)
    (d / "config.toml").write_text(
        '[stages.reader]\nreader = true\naccount = "main"\n\n'
        '[accounts.main]\nharness = "claude"\nconfig_dir = "~/.claude-main"\n'
    )
    (tmp_path / ".claude-main").mkdir()
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake_board(monkeypatch):
    from pl import trackers

    cards_store = {}
    moved = []

    def fake_update(cid, **kw):
        c = cards_store.setdefault(cid, {"id": cid, "metadata": {}, "description": "", "list_id": "Inbox"})
        if "metadata" in kw:
            c["metadata"] = {**c.get("metadata", {}), **kw["metadata"]}
        if "description" in kw:
            c["description"] = kw["description"]
        return c

    def fake_card(cid):
        return cards_store.setdefault(cid, {"id": cid, "metadata": {}, "description": "", "list_id": "Inbox"})

    def fake_move(cid, col):
        c = cards_store.setdefault(cid, {"id": cid, "metadata": {}, "description": "", "list_id": "Inbox"})
        c["list_id"] = col
        moved.append((cid, col))
        return c

    class FakeTracker:
        def move(self, cid, col):
            return fake_move(cid, col)

        def update(self, cid, **kw):
            return fake_update(cid, **kw)

    monkeypatch.setattr(board, "update", fake_update)
    monkeypatch.setattr(board, "card", fake_card)
    monkeypatch.setattr(trackers, "get", lambda kind="tracker": FakeTracker())
    return {"cards": cards_store, "moved": moved}


def test_reader_writes_name_company_ask_summary(monkeypatch, fake_board):
    from pl import reader

    card = {
        "id": "c1",
        "title": "Email lead",
        "list_id": "Inbox",
        "description": render({"INPUT": "Hello, I am Alice from WonderCorp. Can you give me pricing?"}),
        "metadata": {"pipeline_mode": "auto"},
    }
    fake_board["cards"]["c1"] = dict(card)

    fake_output = {
        "name": "Alice",
        "company": "WonderCorp",
        "ask": "Pricing inquiry",
        "summary": "Alice from WonderCorp asked for pricing",
    }

    class FakeRes:
        returncode = 0
        stdout = json.dumps(fake_output)
        stderr = ""

    monkeypatch.setattr(reader, "_run", lambda *a, **kw: FakeRes())

    ok = reader.run(card)
    assert ok is True

    c = fake_board["cards"]["c1"]
    sec = sections(c["description"])
    assert "FIELDS" in sec
    assert "name: Alice" in sec["FIELDS"]
    assert "company: WonderCorp" in sec["FIELDS"]
    assert "ask: Pricing inquiry" in sec["FIELDS"]
    assert "summary: Alice from WonderCorp asked for pricing" in sec["FIELDS"]

    fields = c["metadata"].get("fields")
    assert fields == fake_output
    assert fake_board["moved"] == [("c1", "Spec ready")]


def test_reader_session_has_no_write_tools_and_no_builtin_tools(monkeypatch, fake_board, tmp_path):
    from pl import reader

    card = {"id": "c2", "list_id": "Inbox", "description": "", "metadata": {}}
    fake_board["cards"]["c2"] = dict(card)

    recorded = {}

    class FakeRes:
        returncode = 0
        stdout = json.dumps({"name": "Bob", "company": "Acme", "ask": "Help", "summary": "Needs help"})
        stderr = ""

    def fake_run(argv, **kw):
        recorded["argv"] = list(argv)
        return FakeRes()

    monkeypatch.setattr(reader, "_run", fake_run)

    # 1. Claude
    reader.run(card)
    argv = recorded["argv"]
    assert "--strict-mcp-config" in argv
    assert '--tools' in argv and argv[argv.index('--tools') + 1] == '""'
    mcp_idx = argv.index("--mcp-config")
    mcp_file = Path(argv[mcp_idx + 1])
    assert mcp_file.is_file()
    mcp_cfg = json.loads(mcp_file.read_text())
    srv = mcp_cfg["mcpServers"]["email"]
    assert srv.get("mode") == "reader" or "--mode" in srv.get("args", [])
    # tools exposed must only be read tools (get_thread), no write tools
    tools = srv.get("tools") or []
    assert all(t in ("get_thread", "read") for t in tools)
    assert not any(w in tools for w in ("send", "write", "create", "delete", "update"))

    # 2. Codex
    C.STAGES["reader"]["account"] = "codex_acc"
    C.ACCOUNTS["codex_acc"] = {"harness": "codex", "config_dir": "~/.codex"}
    C.PROFILES["codex_acc"] = tmp_path / ".codex"
    (tmp_path / ".codex").mkdir(exist_ok=True)
    reader.run(card)
    argv_codex = recorded["argv"]
    assert "--sandbox" in argv_codex and argv_codex[argv_codex.index("--sandbox") + 1] == "read-only"


def test_extra_keys_and_long_values_are_dropped_or_cut(monkeypatch, fake_board):
    from pl import reader

    card = {"id": "c3", "list_id": "Inbox", "description": "", "metadata": {}}
    fake_board["cards"]["c3"] = dict(card)

    raw_output = {
        "name": "A" * 120,
        "company": "B" * 120,
        "ask": "C" * 400,
        "summary": "D" * 700,
        "extra_key": "drop_me",
        "secret_instruction": "eval()",
    }

    class FakeRes:
        returncode = 0
        stdout = json.dumps(raw_output)
        stderr = ""

    monkeypatch.setattr(reader, "_run", lambda *a, **kw: FakeRes())

    reader.run(card)
    c = fake_board["cards"]["c3"]
    fields = c["metadata"]["fields"]
    assert len(fields["name"]) == 80
    assert len(fields["company"]) == 80
    assert len(fields["ask"]) == 300
    assert len(fields["summary"]) == 600
    assert "extra_key" not in fields
    assert "secret_instruction" not in fields


def test_non_json_output_fails_the_attempt(monkeypatch, fake_board):
    from pl import reader

    card = {"id": "c4", "list_id": "Inbox", "description": "", "metadata": {}}
    fake_board["cards"]["c4"] = dict(card)

    class FakeRes:
        returncode = 0
        stdout = "I am a helpful assistant and I cannot find any details."
        stderr = ""

    monkeypatch.setattr(reader, "_run", lambda *a, **kw: FakeRes())

    ok = reader.run(card)
    assert ok is False

    c = fake_board["cards"]["c4"]
    assert fake_board["moved"] == []
    w = c["metadata"].get("worker") or {}
    assert w.get("attempts") == 1
    assert "fields" not in c["metadata"]


def test_reader_refuses_other_harnesses(monkeypatch, fake_board, tmp_path):
    from pl import reader

    card = {"id": "c5", "list_id": "Inbox", "description": "", "metadata": {}}
    fake_board["cards"]["c5"] = dict(card)

    C.STAGES["reader"]["account"] = "agy_acc"
    C.ACCOUNTS["agy_acc"] = {"harness": "antigravity", "config_dir": "~/.gemini"}
    C.PROFILES["agy_acc"] = tmp_path / ".gemini"
    (tmp_path / ".gemini").mkdir(exist_ok=True)

    with pytest.raises(SystemExit) as exc:
        reader.run(card)
    assert "reader stages support only claude and codex" in str(exc.value)

