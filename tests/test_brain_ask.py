import json
import pytest
from pathlib import Path
from pl import brain, manager


def _fake_completed(stdout_dict, returncode=0):
    import subprocess
    return subprocess.CompletedProcess([], returncode=returncode, stdout=json.dumps(stdout_dict), stderr="")


def test_last_payment_answer_has_date_amount_and_both_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')

    # Fake profile and account
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        # Fake harness execution: log sources for payment record and client card
        conv = "conv-payment-1"
        brain.log_source(conv, {"title": "Payment #123", "url": "https://payments.example/123", "kind": "payment", "date": "2026-10-01"})
        brain.log_source(conv, {"title": "Client X", "url": "https://github.com/org/repo/issues/10", "kind": "card", "date": "2026-09-15"})
        return _fake_completed({
            "answer": "The last payment from client X was $500 on 2026-10-01.",
            "sources": [
                {"title": "Payment #123", "url": "https://payments.example/123", "kind": "payment", "date": "2026-10-01"},
                {"title": "Client X", "url": "https://github.com/org/repo/issues/10", "kind": "card", "date": "2026-09-15"}
            ],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("When was the last payment from client X?", conv="conv-payment-1")

    assert "$500" in res["answer"]
    assert "2026-10-01" in res["answer"]
    urls = [s["url"] for s in res["sources"]]
    assert "https://payments.example/123" in urls
    assert "https://github.com/org/repo/issues/10" in urls


def test_client_status_covers_and_links_each_linked_card(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        conv = "conv-status-1"
        brain.log_source(conv, {"title": "Engineering card", "url": "https://github.com/org/repo/issues/101", "kind": "card", "date": "2026-10-20"})
        brain.log_source(conv, {"title": "Billing card", "url": "https://github.com/org/billing/issues/202", "kind": "card", "date": "2026-10-21"})
        return _fake_completed({
            "answer": "Client X has engineering card in progress (https://github.com/org/repo/issues/101) and billing card open (https://github.com/org/billing/issues/202).",
            "sources": [
                {"title": "Engineering card", "url": "https://github.com/org/repo/issues/101", "kind": "card", "date": "2026-10-20"},
                {"title": "Billing card", "url": "https://github.com/org/billing/issues/202", "kind": "card", "date": "2026-10-21"}
            ],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("What is the status of client X?", conv="conv-status-1")

    assert "https://github.com/org/repo/issues/101" in res["answer"]
    assert "https://github.com/org/billing/issues/202" in res["answer"]
    assert len(res["sources"]) == 2


def test_what_needs_me_lists_every_gate_and_alert_oldest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        conv = "conv-needs-1"
        brain.log_source(conv, {"title": "Alert 1", "url": "https://pl.example/alert/1", "kind": "alert", "date": "2026-10-18"})
        brain.log_source(conv, {"title": "Gate 2", "url": "https://pl.example/gate/2", "kind": "gate", "date": "2026-10-19"})
        brain.log_source(conv, {"title": "Gate 3", "url": "https://pl.example/gate/3", "kind": "gate", "date": "2026-10-20"})
        return _fake_completed({
            "answer": "Here is what needs you today:\n1. Alert 1 (2026-10-18)\n2. Gate 2 (2026-10-19)\n3. Gate 3 (2026-10-20)",
            "sources": [
                {"title": "Alert 1", "url": "https://pl.example/alert/1", "kind": "alert", "date": "2026-10-18"},
                {"title": "Gate 2", "url": "https://pl.example/gate/2", "kind": "gate", "date": "2026-10-19"},
                {"title": "Gate 3", "url": "https://pl.example/gate/3", "kind": "gate", "date": "2026-10-20"}
            ],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("What needs me today?", conv="conv-needs-1")

    dates = [s["date"] for s in res["sources"]]
    assert dates == sorted(dates)
    assert res["sources"][0]["title"] == "Alert 1"


def test_no_source_says_so_instead_of_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        return _fake_completed({
            "answer": "I guess it was 42.",
            "sources": [],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("What is the secret answer?", conv="conv-guess-1")

    assert res["answer"] == "I found no source for that, so I won't guess."
    assert res["sources"] == []


def test_an_invented_source_url_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        conv = "conv-invented-1"
        brain.log_source(conv, {"title": "Real doc", "url": "https://pl.example/real", "kind": "doc"})
        return _fake_completed({
            "answer": "Information based on real doc and invented blog.",
            "sources": [
                {"title": "Real doc", "url": "https://pl.example/real", "kind": "doc"},
                {"title": "Invented blog", "url": "https://fake.example/invented", "kind": "doc"}
            ],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("Tell me about the docs", conv="conv-invented-1")

    assert len(res["sources"]) == 1
    assert res["sources"][0]["url"] == "https://pl.example/real"
    assert res["answer"] == "Information based on real doc and invented blog."

    # When all sources are invented, answer becomes the fallback
    def fake_run_all_invented(argv, env=None, input=None, **kw):
        return _fake_completed({
            "answer": "Totally made up.",
            "sources": [{"title": "Invented", "url": "https://fake.example/only-invented", "kind": "doc"}],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run_all_invented)
    res2 = brain.ask("Another question", conv="conv-invented-2")
    assert res2["sources"] == []
    assert res2["answer"] == "I found no source for that, so I won't guess."


def test_send_an_invoice_files_a_billing_card_and_calls_no_write_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        conv = "conv-invoice-1"
        brain.log_card(conv, {"pipeline": "billing", "id": "card-456", "url": "https://github.com/org/billing/issues/456"})
        brain.log_source(conv, {"title": "Client X", "url": "https://github.com/org/repo/issues/10", "kind": "card"})
        return _fake_completed({
            "answer": "Created a card in billing pipeline: https://github.com/org/billing/issues/456",
            "sources": [
                {"title": "Client X", "url": "https://github.com/org/repo/issues/10", "kind": "card"}
            ],
            "cards": [
                {"pipeline": "billing", "id": "card-456", "url": "https://github.com/org/billing/issues/456"},
                {"pipeline": "billing", "id": "hallucinated-card", "url": "https://fake.example/card"}
            ]
        })

    monkeypatch.setattr(brain, "_run", fake_run)
    res = brain.ask("Send client X an invoice", conv="conv-invoice-1")

    assert len(res["cards"]) == 1
    assert res["cards"][0]["id"] == "card-456"
    assert "https://github.com/org/billing/issues/456" in res["answer"]


def test_brain_argv_has_only_the_pl_brain_server(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # 1. Claude harness
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    claude_calls = []

    def fake_run_claude(argv, env=None, input=None, **kw):
        claude_calls.append((argv, env))
        brain.log_source("conv-argv-1", {"title": "Doc", "url": "https://pl.example/doc"})
        return _fake_completed({"answer": "ok", "sources": [{"title": "Doc", "url": "https://pl.example/doc"}], "cards": []})

    monkeypatch.setattr(brain, "_run", fake_run_claude)
    brain.ask("Question 1", conv="conv-argv-1")
    assert len(claude_calls) == 1
    argv_c = claude_calls[0][0]
    assert "--mcp-config" in argv_c
    mcp_idx = argv_c.index("--mcp-config") + 1
    mcp_data = json.loads(argv_c[mcp_idx])
    assert mcp_data == {"mcpServers": {"pl-brain": {"command": "pl", "args": ["mcp", "serve", "--brain", "--stdio"]}}}
    assert "--strict-mcp-config" in argv_c
    assert "--allowedTools" in argv_c
    allowed_idx = argv_c.index("--allowedTools") + 1
    assert argv_c[allowed_idx] == "mcp__pl-brain__*"
    assert not any(t in argv_c for t in ("Bash", "Edit", "Write"))

    # 2. Codex harness
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "codex"\n')
    codex_calls = []

    def fake_run_codex(argv, env=None, input=None, **kw):
        codex_calls.append((argv, env))
        brain.log_source("conv-argv-2", {"title": "Doc", "url": "https://pl.example/doc"})
        return _fake_completed({"answer": "ok", "sources": [{"title": "Doc", "url": "https://pl.example/doc"}], "cards": []})

    monkeypatch.setattr(brain, "_run", fake_run_codex)
    brain.ask("Question 2", conv="conv-argv-2")
    assert len(codex_calls) == 1
    argv_x = codex_calls[0][0]
    assert any(a.startswith("mcp_servers.pl-brain=") for a in argv_x)
    assert "--sandbox" in argv_x
    sb_idx = argv_x.index("--sandbox") + 1
    assert argv_x[sb_idx] == "read-only"

    # 3. Harness with no way to restrict tools is refused with exit 2
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "antigravity"\n')
    with pytest.raises(SystemExit) as exc:
        brain.ask("Question 3", conv="conv-argv-3")
    assert exc.value.code == 2


def test_cmd_ask_plain_and_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    (tmp_path / "machine.toml").write_text('[brain]\nprofile = "test"\naccount = "main"\n')
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    (pdir / "config.toml").write_text('[tracker]\ntype = "github-issues"\nrepo = "org/repo"\n[accounts.main]\nharness = "claude"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(argv, env=None, input=None, **kw):
        brain.log_source("conv-cli-1", {"title": "Doc 1", "url": "https://pl.example/doc1"})
        brain.log_source("conv-cli-1", {"title": "Doc 2", "url": "https://pl.example/doc2"})
        return _fake_completed({
            "answer": "Here is the answer.",
            "sources": [
                {"title": "Doc 1", "url": "https://pl.example/doc1"},
                {"title": "Doc 2", "url": "https://pl.example/doc2"}
            ],
            "cards": []
        })

    monkeypatch.setattr(brain, "_run", fake_run)

    # 1. Plain output
    brain.cmd_ask(["What is the answer?", "--conversation", "conv-cli-1"])
    out, _ = capsys.readouterr()
    assert "Here is the answer." in out
    assert "1. Doc 1: https://pl.example/doc1" in out
    assert "2. Doc 2: https://pl.example/doc2" in out

    # 2. JSON output
    brain.cmd_ask(["What is the answer?", "--json", "--conversation", "conv-cli-1"])
    out_json, _ = capsys.readouterr()
    parsed = json.loads(out_json)
    assert parsed["answer"] == "Here is the answer."
    assert len(parsed["sources"]) == 2
    assert parsed["conversation"] == "conv-cli-1"

