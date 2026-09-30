"""WP30: the Background tab lists a harness session's sub-agents and shows the selected one's transcript tail."""
import json
import os
import time

import pytest

from pl import config as C
from pl.tui import loops
from pl.tui import subagents as sa
from pl.tui.app import PlApp

SESSION = "11111111-2222-4333-8444-555555555555"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    acct = tmp_path / ".claude-main"
    C.PROFILES = {"main": acct}
    C.ACCOUNTS = {"main": {"harness": "claude", "config_dir": str(acct)}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _tool_use(name, inp, tid):
    return {"type": "assistant", "timestamp": "2026-09-29T10:00:01Z",
            "message": {"role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _tool_result(tid, text, err=False):
    return {"type": "user", "timestamp": "2026-09-29T10:00:02Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}


def _prompt(text):
    return {"type": "user", "timestamp": "2026-09-29T10:00:00Z", "message": {"role": "user", "content": text}}


def _text(text):
    return {"type": "assistant", "timestamp": "2026-09-29T10:00:03Z",
            "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}}


def write_agent(home, agent, entries, age, desc, kind="general-purpose", session=SESSION):
    d = home / ".claude-main" / "projects" / "-Users-me-code" / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"agent-{agent}.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    (d / f"agent-{agent}.meta.json").write_text(json.dumps({"agentType": kind, "description": desc}))
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def three_agents(home):
    write_agent(home, "run1", [_prompt("go"), _tool_use("Bash", {"command": f"echo {TOKEN}"}, "t1")], 10, "Run the tests")
    write_agent(home, "wait1", [_prompt("go"), _tool_use("Read", {"file_path": "/x/app.py"}, "t2")], 600, "Read the app")
    write_agent(home, "err1", [_prompt("go"), _tool_use("Bash", {"command": "make"}, "t3"),
                               _tool_result("t3", "make: *** no rule\nmore", err=True)], 900, "Build it")
    write_agent(home, "old1", [_prompt("go"), _text("all done")], 7 * 3600, "Ancient")


def test_scan_lists_recent_agents_newest_first_with_states(fake_home):
    three_agents(fake_home)
    rows, notes = sa.scan()
    assert notes == []
    assert [(r["agent"], r["state"]) for r in rows] == [("run1", "running"), ("wait1", "waiting"), ("err1", "error")]
    assert rows[0]["parent"] == SESSION[:8]
    assert rows[1]["what"] == "general-purpose: Read the app"


def test_state_rules():
    old, fresh = 600, 10
    assert sa.state([_prompt("go"), _text("finished")], old) == "done"
    assert sa.state([_prompt("go"), _text("finished")], fresh) == "running"
    assert sa.state([_prompt("go"), _tool_use("Read", {}, "a"), _tool_result("a", "ok")], old) == "waiting"
    assert sa.state([_prompt("go"), _text("May I have permission to run this?")], old) == "done"   # sub-agents cannot ask the user
    api = {"type": "assistant", "isApiErrorMessage": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "API Error: 529"}]}}
    assert sa.state([_prompt("go"), api], fresh) == "error"
    assert sa.state([], old) == "done"


def test_card_mapping_from_worker_session_id(fake_home):
    three_agents(fake_home)
    data = {"snapshot": {"rows": [{"card": {"id": "abcdef1234", "title": "Fix it"}, "worker": {"session_id": SESSION}}]}}
    rows, _ = sa.scan(data)
    assert rows[0]["card"] == "abcdef12 Fix it"


def test_other_harness_shows_one_line_and_nothing_else(fake_home):
    three_agents(fake_home)
    C.ACCOUNTS["main"]["harness"] = "codex"
    rows, notes = sa.scan()
    assert rows == [] and notes == ["no background-agent view for codex"]


def test_tail_read_never_reads_more_than_the_cap(fake_home, monkeypatch):
    big = [_prompt("go")] + [_text(f"{i} " + "x" * 1000) for i in range(2000)] + [_text("END")]
    f = write_agent(fake_home, "big1", big, 10, "Big")
    assert f.stat().st_size > sa.TAIL_BYTES
    reads = []
    real_open = open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

        def __getattr__(self, n):
            return getattr(self.fh, n)

        def read(self, n=-1):
            out = self.fh.read(n)
            reads.append(len(out))
            return out

    monkeypatch.setattr(sa, "open", lambda p, mode="r", **kw: Spy(real_open(p, mode, **kw)), raising=False)
    entries = sa.read_tail(f)
    assert sum(reads) <= sa.TAIL_BYTES
    assert entries[-1]["message"]["content"][0]["text"] == "END"
    assert entries[0]["message"]["content"][0]["text"].startswith(("17", "18", "19"))   # a cut first line is dropped, not the start


def test_secret_looking_token_is_masked():
    lines = sa.render([_tool_use("Bash", {"command": f"curl -H 'Authorization: Bearer {TOKEN}'"}, "t")])
    assert TOKEN not in "\n".join(lines)
    assert "***" in "\n".join(lines)


def test_never_reads_outside_projects_or_credential_files(fake_home):
    acct = fake_home / ".claude-main"
    (acct / "projects").mkdir(parents=True)
    (acct / ".credentials.json").write_text("{}")
    link = acct / "projects" / "-x" / SESSION / "subagents"
    link.mkdir(parents=True)
    os.link(acct / ".credentials.json", link / "agent-cred.jsonl")   # a hard link to a credential file
    outside = fake_home / "elsewhere.jsonl"
    outside.write_text(json.dumps(_prompt("hi")) + "\n")
    os.symlink(outside, link / "agent-sym.jsonl")
    rows, _ = sa.scan()
    assert rows == []


def test_meta_hard_linked_to_credentials_is_not_read(fake_home):
    f = write_agent(fake_home, "m1", [_prompt("go")], 10, "Fine")
    acct = fake_home / ".claude-main"
    (acct / ".credentials.json").write_text(json.dumps({"agentType": "SECRETX", "description": "leak"}))
    m = f.with_name("agent-m1.meta.json")
    m.unlink()
    os.link(acct / ".credentials.json", m)
    rows, _ = sa.scan()
    assert [r["agent"] for r in rows] == ["m1"] and "SECRETX" not in rows[0]["what"]


def test_symlinked_projects_folder_is_skipped(fake_home):
    elsewhere = fake_home / "elsewhere"
    write_agent(fake_home, "s1", [_prompt("go")], 10, "Real")
    (fake_home / ".claude-main" / "projects").rename(elsewhere)
    os.symlink(elsewhere, fake_home / ".claude-main" / "projects")
    assert sa.scan()[0] == []


def test_mask_before_cut_pem_and_escaped_secret(monkeypatch):
    monkeypatch.setattr(sa.mcp, "_secrets", {'pa"ss\\word12'})
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabcSECRET\n-----END RSA PRIVATE KEY-----"
    lines = sa.render([_tool_use("Bash", {"command": "x" * 170 + " " + TOKEN}, "t"), _text("key:\n" + pem),
                       _tool_use("Odd", {"x": 'pa"ss\\word12'}, "u")])
    out = "\n".join(lines)
    assert "ghp_A1b" not in out and "MIIabc" not in out and "word12" not in out


def test_odd_lines_do_not_raise(fake_home):
    f = write_agent(fake_home, "odd1", [], 10, "Odd")
    f.write_text(json.dumps({"message": "a string"}) + "\n" + "[" * 100000 + "\n" + json.dumps(_text("ok")) + "\n")
    entries = sa.read_tail(f)
    assert [sa._blocks(e)[0]["text"] for e in entries] == ["ok"]
    sa.render(entries)
    sa.state(entries, 600)


def test_last_entry_bigger_than_cap_is_unknown(fake_home):
    write_agent(fake_home, "huge1", [_text("x" * (sa.TAIL_BYTES + 10))], 600, "Huge")
    rows, _ = sa.scan()
    assert rows[0]["state"] == "unknown"
    assert sa.tail_lines(rows[0]["path"]) == ["last entry larger than 256 KB"]


def _screen(app):
    return str(app.query_one("#subagents-screen").render())


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def test_tab_lists_agents_shows_tail_and_skips_unchanged_redraw(fake_home, monkeypatch):
    three_agents(fake_home)
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("subagents")
        await settle(pilot)
        view = app.query_one(sa.SubagentsView)
        assert [r["state"] for r in view.rows] == ["running", "waiting", "error"]
        text = _screen(app)
        assert "tool Bash" in text and TOKEN not in text and "***" in text
        n = view.redraws
        view.tick()
        await settle(pilot)
        assert view.redraws == n          # the file did not change: no redraw
        await pilot.press("down")
        await settle(pilot)
        assert "tool Read: /x/app.py" in _screen(app)
        copied = []
        monkeypatch.setattr(PlApp, "copy_to_clipboard", lambda self, t: copied.append(t))
        monkeypatch.setattr(loops.shutil, "which", lambda name: None)
        await pilot.press("y")
        await settle(pilot)
        assert copied and "tool Read: /x/app.py" in copied[0]


async def test_header_state_updates_with_time_alone(fake_home, monkeypatch):
    write_agent(fake_home, "t1", [_prompt("go"), _tool_use("Read", {"file_path": "/a"}, "t")], 10, "Timed")
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("subagents")
        await settle(pilot)
        assert "(running)" in _screen(app)
        later = time.time() + 600
        monkeypatch.setattr(sa.time, "time", lambda: later)
        app.query_one(sa.SubagentsView).tick()
        await settle(pilot)
        assert "(waiting)" in _screen(app)


async def test_hidden_tab_does_not_scan(fake_home, monkeypatch):
    three_agents(fake_home)
    calls = []
    monkeypatch.setattr(sa, "scan", lambda data=None: calls.append(1) or ([], []))
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one(sa.SubagentsView).tick()
        await settle(pilot)
        assert calls == []


async def _noop():
    return None
