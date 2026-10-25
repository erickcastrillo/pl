import json
import stat
import time

from pl import brain, cli


def test_conversation_file_is_0600_and_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    cid = brain.conv_new()
    conv_dir = brain._conv_dir()
    conv_file = conv_dir / f"{cid}.jsonl"

    assert conv_dir.exists()
    assert stat.S_IMODE(conv_dir.stat().st_mode) == 0o700
    assert conv_file.exists()
    assert stat.S_IMODE(conv_file.stat().st_mode) == 0o600

    secret = "sk-1234567890abcdef123456"
    brain.conv_append(cid, "user", f"my token is {secret}")

    assert stat.S_IMODE(conv_file.stat().st_mode) == 0o600
    content = conv_file.read_text()
    assert secret not in content
    assert "***" in content

    brain.conv_append(cid, "assistant", "noted")
    lines = [json.loads(line) for line in conv_file.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["role"] == "user"
    assert lines[1]["role"] == "assistant"
    assert lines[1]["text"] == "noted"


def test_prune_deletes_only_conversations_older_than_180_days_and_dry_run_deletes_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    c_old = brain.conv_new("old")
    c_new = brain.conv_new("new")
    brain.conv_append(c_old, "user", "old message")
    brain.conv_append(c_new, "user", "new message")

    now = time.time()
    old_time = now - (181 * 86400)
    p_old = brain._conv_dir() / f"{c_old}.jsonl"
    p_new = brain._conv_dir() / f"{c_new}.jsonl"

    import os
    os.utime(p_old, (old_time, old_time))

    would_prune = brain.prune(now=now, dry_run=True)
    assert p_old in would_prune or str(p_old) in [str(x) for x in would_prune]
    assert p_old.exists()
    assert p_new.exists()

    pruned = brain.prune(now=now, dry_run=False)
    assert p_old in pruned or str(p_old) in [str(x) for x in pruned]
    assert not p_old.exists()
    assert p_new.exists()


def test_pl_brain_show_prints_the_turns(tmp_path, monkeypatch, capsys):
    # Note: Another harness's tab conversations are kept as the owner's lines only;
    # claude transcript reply turns follow usage.py's paths.
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    cid = brain.conv_new("test_show")
    brain.conv_append(cid, "user", "hello brain")
    brain.conv_append(cid, "assistant", "hello human")

    monkeypatch.setattr("sys.argv", ["pl", "brain", "show", cid])
    cli.main()
    out = capsys.readouterr().out
    assert "user" in out and "hello brain" in out
    assert "assistant" in out and "hello human" in out

    monkeypatch.setattr("sys.argv", ["pl", "brain", "show", cid, "--json"])
    cli.main()
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2
    assert data[0]["role"] == "user"
    assert data[0]["text"] == "hello brain"
    assert data[1]["role"] == "assistant"
    assert data[1]["text"] == "hello human"


def test_pl_brain_prune_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    c = brain.conv_new("cli_old")
    p = brain._conv_dir() / f"{c}.jsonl"
    import os
    os.utime(p, (time.time() - 200 * 86400, time.time() - 200 * 86400))

    monkeypatch.setattr("sys.argv", ["pl", "brain", "prune", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert "would delete 1 conversation" in out
    assert p.exists()

    monkeypatch.setattr("sys.argv", ["pl", "brain", "prune"])
    cli.main()
    out = capsys.readouterr().out
    assert "deleted 1 conversation" in out
    assert not p.exists()


def test_manager_tick_prunes_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    from pl import manager

    calls = []
    monkeypatch.setattr(brain, "prune", lambda now=None: calls.append(now))
    monkeypatch.setattr(manager, "managed", lambda: [])
    monkeypatch.setattr(manager, "_caps", lambda s, p: (0, 8, None, None, {}))
    monkeypatch.setattr(manager, "_machine_check", lambda s: None)
    monkeypatch.setattr(manager, "_write", lambda name, data: None)

    state = {}
    manager.tick(state)
    assert len(calls) == 1
    assert "brain_pruned" in state

    # Second tick within a day does not prune again
    manager.tick(state)
    assert len(calls) == 1

