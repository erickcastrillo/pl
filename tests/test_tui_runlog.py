"""Plan 42 slice 2: the read-only screen that shows one loop run's whole masked log."""
from datetime import datetime

import pytest
from textual.app import App

from pl import config as C
from pl.trackers import mcp
from pl.tui import runlog


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    folder = C.STATE_DIR / "loops" / "invoices"
    folder.mkdir(parents=True)
    monkeypatch.setattr(mcp, "_secrets", set())
    yield folder
    for k, v in saved.items():
        setattr(C, k, v)


def test_log_text_reads_the_runs_kept_log_masked(profile, monkeypatch):
    (profile / "20261025090000.log").write_text("start\ntoken s3cr3t-value\ndone\n")
    monkeypatch.setattr(mcp, "_secrets", {"s3cr3t-value"})
    text = runlog.log_text("invoices", {"log": "20261025090000.log"})
    assert "start" in text and "done" in text and "s3cr3t-value" not in text


@pytest.mark.parametrize("run", [{"log": "gone.log"}, {}, {"log": "../x.log"}, {"log": "/etc/passwd"}])
def test_log_text_of_a_pruned_run_is_none(profile, run):
    (profile.parent / "x.log").write_text("outside the loop's folder")
    assert runlog.log_text("invoices", run) is None


def test_run_title_names_loop_time_and_result():
    when = datetime(2026, 10, 25, 9, 0).astimezone()
    ok = runlog.run_title("invoices", {"started_at": when.isoformat(), "ok": True})
    assert ok == f"invoices loop · {when:%Y-%m-%d %H:%M} · ok"
    failed = runlog.run_title("invoices", {"started_at": when.isoformat(), "ok": False, "reason": "timeout"})
    assert failed.endswith("· failed · timeout")
    assert "?" in runlog.run_title("invoices", {"started_at": "bad", "ok": True})


class Host(App):
    pass


async def test_run_log_screen_is_read_only_and_esc_closes(profile):
    log = profile / "a.log"
    log.write_text("one\ntwo\n")
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await app.push_screen(runlog.RunLogScreen("invoices", {"log": "a.log", "ok": True}))
        await pilot.pause()
        before = str(app.screen.query_one("#runlog-body").render())
        await pilot.press("a", "d", "delete", "g", "R", "enter")
        await pilot.pause()
        assert isinstance(app.screen, runlog.RunLogScreen)
        assert str(app.screen.query_one("#runlog-body").render()) == before and "one" in before
        assert log.read_text() == "one\ntwo\n"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, runlog.RunLogScreen)


async def test_run_log_screen_opens_at_the_top(profile):
    (profile / "a.log").write_text("\n".join(f"line {i}" for i in range(300)))
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await app.push_screen(runlog.RunLogScreen("invoices", {"log": "a.log", "ok": True}))
        await pilot.pause()
        scroll = app.screen.query_one("#runlog-scroll")
        assert scroll.scroll_y == 0 and scroll.max_scroll_y > 0
        await pilot.press("end")
        await pilot.pause()
        assert scroll.scroll_y > 0


async def test_run_log_screen_of_a_pruned_run_says_so(profile):
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await app.push_screen(runlog.RunLogScreen("invoices", {"log": "gone.log", "ok": False}))
        await pilot.pause()
        assert runlog.MISSING in str(app.screen.query_one("#runlog-body").render())
