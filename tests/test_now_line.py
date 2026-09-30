"""One "doing now" line per agent: read from the transcript tail, masked, cached by size and mtime."""
import json
import os
import time

import pytest

from pl import config as C
from pl.tui import pipeline as tui_pipeline
from pl.tui import subagents as sa
from pl.tui.app import PlApp
from test_tui_views import Provider, fake_data, screen_text, settle

SID = "11111111-2222-4333-8444-555555555555"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
CARD = "4a000001aaaa"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    C.PROFILES = {"acme": tmp_path / ".claude-acme", "codex": tmp_path / ".codex-x"}
    C.ACCOUNTS = {"acme": {"harness": "claude", "config_dir": str(C.PROFILES["acme"])},
                  "codex": {"harness": "codex", "config_dir": str(C.PROFILES["codex"])}}
    sa._now_cache.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def use(name, inp, tid="t1", cwd="/w"):
    return {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(text, err=False, tid="t1"):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}


def say(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def todo(*items):
    return use("TodoWrite", {"todos": [{"content": c, "status": s, "activeForm": c} for s, c in items]}, tid="td")


TODOS = [("completed", "a"), ("completed", "b"), ("in_progress", "write the migration"), ("pending", "d")]


def transcript(home, entries, sid=SID, age=5):
    d = home / ".claude-acme" / "projects" / "-w"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    os.utime(f, (time.time() - age,) * 2)
    return f


def test_error_beats_everything(fake_home):
    f = transcript(fake_home, [todo(*TODOS), use("Edit", {"file_path": "/w/a.rb"}), result("Traceback: boom\nmore", err=True), say("ok")][:3])
    assert sa.now_line(f) == "error: Traceback: boom"


def test_api_error_entry(fake_home):
    e = say("Overloaded")
    e["isApiErrorMessage"] = True
    assert sa.now_line(transcript(fake_home, [todo(*TODOS), e])) == "error: Overloaded"


def test_pending_tool_with_short_input_and_todo_position(fake_home):
    f = transcript(fake_home, [todo(*TODOS), use("Edit", {"file_path": "/w/app/models/x.rb"})])
    assert sa.now_line(f) == "Edit app/models/x.rb · todo 3/4"


def test_todo_when_no_tool_is_pending(fake_home):
    f = transcript(fake_home, [todo(*TODOS), use("Read", {"file_path": "/w/a"}), result("fine")])
    assert sa.now_line(f) == "todo 3/4: write the migration"


def test_last_reply_first_line_when_nothing_else(fake_home):
    assert sa.now_line(transcript(fake_home, [say("All done.\nSecond line")])) == "All done."


def test_old_error_followed_by_progress_is_not_shown(fake_home):
    f = transcript(fake_home, [use("Bash", {"command": "x"}), result("bad", err=True), say("Fixed it")])
    assert sa.now_line(f) == "Fixed it"


def test_secret_masked_and_cut_to_60(fake_home):
    f = transcript(fake_home, [use("Bash", {"command": f"curl -H 'Authorization: Bearer {TOKEN}' https://x.test/" + "a" * 100})])
    line = sa.now_line(f)
    assert TOKEN not in line and "***" in line and len(line) <= 60
    assert TOKEN not in sa.now_line(transcript(fake_home, [result(f"token {TOKEN}", err=True)]))


def test_reads_only_the_tail(fake_home, monkeypatch):
    f = transcript(fake_home, [say("filler " * 200) for _ in range(1500)] + [use("Edit", {"file_path": "/w/last.rb"})])
    assert f.stat().st_size > 2 * sa.TAIL_BYTES
    reads, real_open = [], open

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
            reads.append(n)
            return self.fh.read(n)

    monkeypatch.setattr(sa, "open", lambda p, mode="r", **kw: Spy(real_open(p, mode, **kw)), raising=False)
    assert sa.now_line(f) == "Edit last.rb"
    assert reads and all(0 < n <= sa.TAIL_BYTES for n in reads)


def live_row(profile="acme", win="w1"):
    r = next(x for x in fake_data()["snapshot"]["rows"] if x.get("card") and x["card"]["id"] == CARD)
    r["worker"], r["win"] = {"session_id": SID, "profile": profile, "stage": "plan"}, win
    return r


def test_now_lines_skips_unchanged_file_and_other_harnesses(fake_home, monkeypatch):
    f = transcript(fake_home, [todo(*TODOS), use("Edit", {"file_path": "/w/a.rb"})])
    calls = []
    real = sa.read_tail
    monkeypatch.setattr(sa, "read_tail", lambda *a, **k: calls.append(1) or real(*a, **k))
    row = live_row()
    out = sa.now_lines([row])
    assert out[CARD]["line"] == "Edit a.rb · todo 3/4" and out[CARD]["todos"][2] == ("in_progress", "write the migration")
    sa.now_lines([row])
    assert len(calls) == 1                      # unchanged: no second read
    f.write_text(f.read_text() + json.dumps(say("more")) + "\n")
    assert sa.now_lines([row])[CARD]["line"] == "todo 3/4: write the migration" and len(calls) == 2
    assert sa.now_lines([live_row("codex")]) == {}
    assert sa.now_lines([live_row(win="")]) == {}


def test_stale_transcript_shows_nothing(fake_home):
    transcript(fake_home, [say("hi")], age=7 * 3600)
    assert sa.now_lines([live_row()]) == {}


async def test_pipeline_card_and_needs_row_show_the_line_and_todos(fake_home, monkeypatch):
    monkeypatch.setattr(tui_pipeline, "card", lambda cid: {"id": cid, "title": "T", "description": "body text"})
    monkeypatch.setattr(tui_pipeline.trackers, "get", lambda name: type("T", (), {"url": lambda self, i: None})())
    transcript(fake_home, [todo(*TODOS), use("Edit", {"file_path": "/w/app/models/x.rb"})])
    data = fake_data()
    data["now"] = sa.now_lines([live_row()])
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        text = screen_text(app)   # the card is 25 columns wide, so the line wraps
        assert "app/models/x.rb ·" in text and "todo 3/4" in text
        await pilot.press("2")
        await pilot.pause()
        assert "Edit app/models/x.rb" in screen_text(app)
        app.action_tab("pipeline")
        await pilot.pause()
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == CARD).focus()
        await pilot.press("enter")
        await settle(pilot)
        text = screen_text(app)
        assert "[>] write the migration" in text and "[x] a" in text and "[ ] d" in text
