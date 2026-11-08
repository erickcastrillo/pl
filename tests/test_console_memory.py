"""The console's memory: glibc malloc arenas are capped before its worker threads start, and a refresh parses the
event log once (a console grew to 1.6 GB in 3 days with one arena per pool thread)."""
import argparse
import sys

from pl import events, watch
from pl.tui import app as tui_app


class FakeLibc:
    def __init__(self, calls, ok=1):
        self.calls, self.ok = calls, ok

    def mallopt(self, param, value):
        self.calls.append(("mallopt", param, value))
        return self.ok


def test_the_console_caps_malloc_arenas_before_it_starts(monkeypatch):
    calls = []

    class FakeApp:
        restart_into = None

        def __init__(self, **kw):
            pass

        def run(self):
            calls.append("run")

    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(watch, "_libc", lambda: FakeLibc(calls))
    monkeypatch.setattr(tui_app, "PlApp", FakeApp)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    watch.cmd_watch(argparse.Namespace(interval=None, once=False, plain=False, open=None, tab=None, back=False))
    assert calls == [("mallopt", watch.M_ARENA_MAX, watch.CONSOLE_ARENAS), "run"]


def test_the_arena_cap_leaves_a_set_environment_and_other_systems_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "_libc", lambda: FakeLibc(calls))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("MALLOC_ARENA_MAX", "4")
    assert watch.cap_malloc_arenas() is False
    monkeypatch.delenv("MALLOC_ARENA_MAX")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert watch.cap_malloc_arenas() is False
    assert calls == []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(watch, "_libc", lambda: object())   # a C library without mallopt
    assert watch.cap_malloc_arenas() is False
    monkeypatch.setattr(watch, "_libc", lambda: FakeLibc(calls, ok=0))   # mallopt refused the value
    assert watch.cap_malloc_arenas() is False


def test_one_refresh_parses_the_event_log_once(monkeypatch, tmp_path):
    from pl import board, ghquota, manager, memory, outward
    reads = []
    monkeypatch.setattr(events, "_read", lambda demo=False: reads.append(1) or [])
    monkeypatch.setattr(ghquota, "set_role", lambda role: None)
    monkeypatch.setattr(board, "share", lambda mode: None)
    monkeypatch.setattr(watch, "watch_snapshot", lambda: {"rows": []})
    monkeypatch.setattr(watch, "pr_activity", lambda: None)
    monkeypatch.setattr(memory, "status", lambda: None)
    monkeypatch.setattr(manager, "read_status", lambda: None)
    monkeypatch.setattr(outward, "state", lambda: None)
    for name in ("_usage", "_alerts", "_watchdog", "_queued_moves"):
        monkeypatch.setattr(tui_app, name, lambda: None)
    monkeypatch.setattr(tui_app, "_now", lambda rows: {})
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tui_app.default_provider()
    assert reads == [1]
    assert set(data["metrics_by_window"]) == {"1h", "24h", "7d"} and data["daily"]["specs"] == [0] * 14
