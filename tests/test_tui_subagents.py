"""WP30: the Background tab lists a harness session's sub-agents and shows the selected one's transcript tail."""
import json
import os
import subprocess
import time

import pytest

from pl import config as C
from pl import harnesses
from pl.tui import loops
from pl.tui import subagents as sa
from pl.tui.app import PlApp
from pl.tui.chrome import resolve
from pl.tui.live import LiveScreen
from tui_wait import settle

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


async def _open_tab(pilot, app):
    app.action_tab("subagents")
    await settle(pilot, lambda: app.subtabs.get(resolve("subagents")[0]) == "subagents", "the tab to activate")


def _screen(app):
    return str(app.query_one("#subagents-screen").render())


async def test_tab_lists_agents_shows_tail_and_skips_unchanged_redraw(fake_home, monkeypatch):
    three_agents(fake_home)
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await _open_tab(pilot, app)
        view = app.query_one(sa.SubagentsView)
        await settle(pilot, lambda: len(view.rows) == 3, "the three agents")
        app.query_one("#subagents-table").focus()
        assert [r["state"] for r in view.rows] == ["running", "waiting", "error"]
        text = _screen(app)
        assert "tool Bash" in text and TOKEN not in text and "***" in text
        n = view.redraws
        view.tick()
        await settle(pilot)   # nothing changes on screen, so there is no state to wait for
        assert view.redraws == n          # the file did not change: no redraw
        await pilot.press("down")
        await settle(pilot, lambda: "tool Read: /x/app.py" in _screen(app), "the second agent's tail")
        assert "tool Read: /x/app.py" in _screen(app)
        copied = []
        monkeypatch.setattr(PlApp, "copy_to_clipboard", lambda self, t: copied.append(t))
        monkeypatch.setattr(loops.shutil, "which", lambda name: None)
        await pilot.press("y")
        await settle(pilot, lambda: copied, "the copy")
        assert copied and "tool Read: /x/app.py" in copied[0]


async def test_header_state_updates_with_time_alone(fake_home, monkeypatch):
    write_agent(fake_home, "t1", [_prompt("go"), _tool_use("Read", {"file_path": "/a"}, "t")], 10, "Timed")
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await _open_tab(pilot, app)
        await settle(pilot, lambda: "(running)" in _screen(app), "the running header")
        assert "(running)" in _screen(app)
        later = time.time() + 600
        monkeypatch.setattr(sa.time, "time", lambda: later)
        app.query_one(sa.SubagentsView).tick()
        await settle(pilot, lambda: "(waiting)" in _screen(app), "the waiting header")
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
        await settle(pilot)   # a tick on a hidden tab must do nothing, so there is no state to wait for
        assert calls == []


class FakeTmux:
    """Per pane id: screen text and foreground command; a pane missing from `screens` answers code 1."""

    def __init__(self, screens=None, cmds=None, raises=None):
        self.screens, self.cmds, self.raises, self.calls = screens or {}, cmds or {}, raises, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        pane = argv[argv.index("-t") + 1]
        if pane not in self.screens:
            return subprocess.CompletedProcess(argv, 1, "", "")
        out = self.screens[pane] if argv[1] == "capture-pane" else self.cmds.get(pane, "claude") + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def captures(self):
        return [a for a in self.calls if a[1] == "capture-pane"]


def _worker(pane, stage="run", started=None):
    return {"pane": pane, "window": "@" + pane[1:], "stage": stage, "profile": "acct", "model": "sonnet",
            "started_at": started or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7200))}


def _wrow(card_id, pane, win="x", **kw):
    return {"kind": "card", "card": {"id": card_id, "title": "t"}, "worker": _worker(pane, **kw), "win": win}


def _wdata(*rows):
    return {"snapshot": {"rows": list(rows)}}


def test_window_rows_one_per_agent_window_with_fields(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", FakeTmux({"%1": "● Reading\n● Editing a.py\n", "%2": "thinking\nlast two\n"}))
    rows = sa.window_rows(_wdata(_wrow("aaaaaaaa1111", "%1", stage="plan"), _wrow("bbbbbbbb2222", "%2")))
    assert [(r["pane"], r["card"], r["stage"], r["who"], r["age"], r["status"], r["last"]) for r in rows] == [
        ("%1", "aaaaaaaa", "plan", "acct/sonnet", "2h", "working", "● Editing a.py"),
        ("%2", "bbbbbbbb", "run", "acct/sonnet", "2h", "working", "last two")]


def test_window_rows_mask_secrets_in_last_line(monkeypatch):
    token = "ghp_" + "a" * 36
    monkeypatch.setattr(harnesses, "_run", FakeTmux({"%1": f"working\nexport GITHUB_TOKEN={token}\n"}))
    (row,) = sa.window_rows(_wdata(_wrow("aaaaaaaa1111", "%1")))
    assert "***" in row["last"] and token not in row["last"]


def test_window_rows_finished_waiting_limit_prereq(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", FakeTmux(
        {"%1": "All done.\n", "%2": "Which file?\n", "%3": "Claude usage limit reached\n", "%4": "zsh: command not found: uv\n"},
        {"%1": "zsh"}))
    rows = sa.window_rows(_wdata(*(_wrow(f"c{i}000000aaaa", f"%{i}") for i in range(1, 5))))
    assert [r["status"] for r in rows] == [
        "finished: All done.", "waiting for you: Which file?", "stopped: usage limit",
        "stopped: missing prerequisite: zsh: command not found: uv"]


def test_window_rows_no_window_cases(monkeypatch):
    tmux = FakeTmux({"%1": "x\n"})
    monkeypatch.setattr(harnesses, "_run", tmux)
    rows = sa.window_rows(_wdata(_wrow("aaaaaaaa1111", "%1", win=""), _wrow("bbbbbbbb2222", "%9")))
    assert [r["status"] for r in rows] == ["no agent window", "no agent window"]
    assert [a[a.index("-t") + 1] for a in tmux.captures()] == ["%9"]
    monkeypatch.setattr(harnesses, "_run", FakeTmux(raises=FileNotFoundError("tmux")))
    assert sa.window_rows(_wdata(_wrow("aaaaaaaa1111", "%1")))[0]["status"] == "no agent window"
    for data in (_wdata(), None):
        (row,) = sa.window_rows(data)
        assert (row["pane"], row["status"]) == (None, "no agent window")


def test_window_rows_only_rows_with_worker_window(monkeypatch):
    tmux = FakeTmux({"%1": "x\n"})
    monkeypatch.setattr(harnesses, "_run", tmux)
    no_window = _wrow("bbbbbbbb2222", "%2")
    del no_window["worker"]["window"]
    rows = sa.window_rows(_wdata({"kind": "loop_idle", "card": None, "worker": {}, "win": ""}, no_window, _wrow("aaaaaaaa1111", "%1")))
    assert [r["card"] for r in rows] == ["aaaaaaaa"]


def _table_cells(app):
    t = app.query_one("#agentwin-table")
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


async def _open_background(pilot, app, data):
    await settle(pilot)
    await _open_tab(pilot, app)
    app.query_one(sa.SubagentsView).show(data)
    app.query_one(sa.SubagentsView).tick()
    table = app.query_one("#agentwin-table")
    await settle(pilot, lambda: table.row_count == max(1, len(data["snapshot"]["rows"])), "the agent window rows")


def _bg_app(monkeypatch):
    app = PlApp(snapshot_provider=lambda: {"snapshot": {"rows": []}}, interval=3600, autostart=False)
    monkeypatch.setattr(PlApp, "render_views", lambda self: _noop())
    return app


async def test_background_view_lists_agent_windows(fake_home, monkeypatch):
    monkeypatch.setattr(harnesses, "_run", FakeTmux({"%1": "● Reading\n", "%2": "Which file?\n"}))
    app = _bg_app(monkeypatch)
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_background(pilot, app, _wdata(_wrow("aaaaaaaa1111", "%1"), _wrow("bbbbbbbb2222", "%2")))
        cells = _table_cells(app)
        assert [(c[0], c[4]) for c in cells] == [("aaaaaaaa", "working"), ("bbbbbbbb", "waiting for you: Which file?")]


async def test_hidden_background_view_runs_no_capture(fake_home, monkeypatch):
    tmux = FakeTmux({"%1": "x\n", "%2": "y\n"})
    monkeypatch.setattr(harnesses, "_run", tmux)
    app = _bg_app(monkeypatch)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        view = app.query_one(sa.SubagentsView)
        view.show(_wdata(_wrow("aaaaaaaa1111", "%1"), _wrow("bbbbbbbb2222", "%2")))
        view.tick()
        await settle(pilot)   # a hidden view must run no capture, so there is no state to wait for
        assert tmux.captures() == []
        await _open_tab(pilot, app)
        await settle(pilot, lambda: app.query_one("#agentwin-table").row_count == 2, "the shown view's rows")
        tmux.calls.clear()
        view.tick()
        await settle(pilot, lambda: len(tmux.captures()) >= 2, "the captures")
        assert sorted(a[a.index("-t") + 1] for a in tmux.captures()) == ["%1", "%2"]


async def test_enter_opens_live_view_and_esc_returns_to_same_row(fake_home, monkeypatch):
    tmux = FakeTmux({"%1": "one\n", "%2": "two\n"})
    monkeypatch.setattr(harnesses, "_run", tmux)
    app = _bg_app(monkeypatch)
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_background(pilot, app, _wdata(_wrow("aaaaaaaa1111", "%1"), _wrow("bbbbbbbb2222", "%2")))
        app.query_one("#agentwin-table").focus()
        await pilot.press("down", "enter")
        await settle(pilot, lambda: isinstance(app.screen, LiveScreen), "the live screen")
        assert isinstance(app.screen, LiveScreen) and app.screen.pane == "%2"
        await pilot.press("escape")
        await settle(pilot, lambda: not isinstance(app.screen, LiveScreen), "the live screen to close")
        assert not isinstance(app.screen, LiveScreen)
        assert app.query_one("#agentwin-table").cursor_row == 1
    assert not any("send-keys" in a for a in tmux.calls)


async def test_background_view_without_tmux_shows_no_agent_window(fake_home, monkeypatch):
    monkeypatch.setattr(harnesses, "_run", FakeTmux(raises=FileNotFoundError("tmux")))
    app = _bg_app(monkeypatch)
    notes = []
    monkeypatch.setattr(PlApp, "notify", lambda self, *a, **k: notes.append(a))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_background(pilot, app, _wdata())
        assert [c[4] for c in _table_cells(app)] == ["no agent window"]
        assert notes == []


async def _noop():
    return None
