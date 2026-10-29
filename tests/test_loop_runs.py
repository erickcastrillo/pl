"""Plan 6 slice 4: a loop with `every` runs as a fresh headless session; each run is recorded."""
import json
import os
import shlex
import stat
import subprocess
import types
from datetime import datetime, timedelta

import pytest

from pl import accounts, dispatch, harnesses
from pl import config as C

FAKE_CLI = '#!/bin/sh\necho "working"\necho "3 invoices overdue"\n'


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    C.PROFILES = {"cx": tmp_path / ".codex-cx"}
    C.ACCOUNTS = {"cx": {"harness": "codex", "config_dir": str(tmp_path / ".codex-cx")}}
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def tmux_calls(monkeypatch):
    calls, panes = [], {"text": ""}

    def tmux(*args, check=True):
        calls.append(args)
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=panes["text"], stderr=""))
    return calls, panes


def _script(calls):
    typed = [c[-1] for c in calls if c[0] == "send-keys" and "-l" in c]
    return shlex.split(typed[0])[1] if typed else None


def test_headless_script_runs_unattended_and_records_outcome(profile):
    cli = profile / "fakecodex"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    h = harnesses.Harness("codex", str(cli), "CODEX_HOME", ["x"], [str(cli), "exec", "{prompt}"], [])
    log, record = profile / "run.log", profile / "run.json"
    script = harnesses.headless_script(h, "cx", "/pl-billing-chase", "invoices", log, record)
    assert "--ask-for-approval never" in script and "--sandbox workspace-write" in script
    path = profile / "launch.sh"
    path.write_text(script)
    subprocess.run(["sh", str(path)], check=True)
    run = json.loads(record.read_text())
    assert run["exit"] == 0 and run["result"] == "3 invoices overdue" and run["tokens"] == "n/a"
    assert run["started_at"] <= run["ended_at"]
    assert "working" in log.read_text() and not path.exists()


def test_headless_script_creates_mode_0600_log(profile):
    cli = profile / "failing"
    cli.write_text("#!/bin/sh\nexit 3\n")
    cli.chmod(0o755)
    h = harnesses.Harness("codex", str(cli), None, ["x"], [str(cli), "exec", "{prompt}"], [])
    log, record = profile / "run.log", profile / "run.json"
    script = harnesses.headless_script(h, "cx", "p", "invoices", log, record)
    subprocess.run(["sh", "-c", script], check=True)
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600 and stat.S_IMODE(os.stat(record).st_mode) == 0o600
    assert json.loads(record.read_text())["exit"] == 3


def _loop(**kw):
    C.SERVICES = {"invoices": {"prompt": "/pl-billing-chase", "profile": "cx", "every": "daily 09:00", **kw}}


def test_scheduled_loop_starts_when_due_and_records_run_codex(profile, tmux_calls):
    calls, panes = tmux_calls
    _loop()
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    script = open(_script(calls)).read()
    assert "codex" in script and "--ask-for-approval" in script and "exec" in script
    assert ("new-window", "-d", "-t", f"{C.TMUX_SESSION}:", "-n", "invoices") == calls[[c[0] for c in calls].index("new-window")][:6]
    s = st["services"]["invoices"]
    assert s["started_at"] and datetime.fromisoformat(s["next_at"]) > datetime.now()
    record = C.STATE_DIR / "loops" / "invoices.run.json"
    record.write_text(json.dumps({"started_at": "a", "ended_at": "b", "result": "ok", "tokens": "n/a", "exit": 0}))
    panes["text"] = "@7 %9 sh invoices\n"
    calls.clear()
    dispatch.ensure_services(st, [], {}, False, None)
    assert st["services"]["invoices"]["last_run"]["result"] == "ok" and st["services"]["invoices"]["runs"][-1]["exit"] == 0
    assert ("kill-window", "-t", "@7") == calls[0][:3] and not record.exists()
    assert _script(calls) is None   # not due again yet


def test_scheduled_loop_skips_run_outside_quiet_hours(profile, tmux_calls, monkeypatch):
    calls, _ = tmux_calls
    _loop(hours="08:00-18:00 weekdays")
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 10, 26, 20, 0))   # a Monday evening
    events = []
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: events.append(a))
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    assert _script(calls) is None and events == []
    assert st["services"]["invoices"]["next_at"] == datetime(2026, 10, 27, 9, 0).isoformat()


def test_missed_run_fires_once_on_wake(profile, tmux_calls, monkeypatch):
    calls, _ = tmux_calls
    _loop(hours="08:00-18:00")
    monkeypatch.setattr(dispatch, "_local_now", lambda: datetime(2026, 10, 28, 10, 0))
    st = {"services": {"invoices": {"next_at": datetime(2026, 10, 24, 9, 0).isoformat()}}}   # four days asleep
    dispatch.ensure_services(st, [], {}, False, None)
    assert len([c for c in calls if c[0] == "new-window"]) == 1
    assert st["services"]["invoices"]["next_at"] == datetime(2026, 10, 29, 9, 0).isoformat()


def _headless_script(name, **kw):
    h = harnesses.BUILTINS[name]
    return harnesses.headless_script(h, "cx", "/pl-billing-chase", "invoices", C.STATE_DIR / "r.log", C.STATE_DIR / "r.json", **kw)


def test_a_read_only_loop_gets_the_read_only_flags_not_the_unattended_ones(profile):
    codex = _headless_script("codex", writes=False)
    assert "--sandbox read-only" in codex and "--ask-for-approval" not in codex and "workspace-write" not in codex
    assert "export PL_LOOP=invoices" in codex and "export PL_LOOP_WRITES=0" in codex
    claude = _headless_script("claude", writes=False)
    assert "--permission-mode plan" in claude and "--permission-mode auto" not in claude
    assert "PL_LOOP_WRITES" not in _headless_script("codex")


def test_a_harness_without_read_only_flags_waits_instead_of_running(profile, tmux_calls):
    calls, _ = tmux_calls
    C.ACCOUNTS = {"cx": {"harness": "antigravity", "config_dir": str(profile / ".agy-cx")}}
    _loop(writes=False)
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    assert _script(calls) is None and "no read-only mode" in st["services"]["invoices"]["waits"]


@pytest.fixture
def read_only_loop(monkeypatch):
    monkeypatch.setenv("PL_LOOP", "invoices")
    monkeypatch.setenv("PL_LOOP_WRITES", "0")


def test_a_read_only_loop_cannot_move_or_approve_a_card(profile, read_only_loop, monkeypatch, capsys):
    from pl import cli
    (profile / ".pl-t").mkdir()
    (profile / ".pl-t" / "config.toml").write_text("")
    monkeypatch.setenv("PL_CONFIG_DIR", str(profile / ".pl-t"))
    for argv in (["pl", "move", "1", "Done"], ["pl", "approve", "1"], ["pl", "section", "1", "PLAN"], ["pl", "caps", "set", "1", "--minutes", "5"]):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 2
        assert "read-only loop: file the action as an idea" in capsys.readouterr().err


class FakeTracker:
    def __init__(self):
        self.created = []

    def create(self, column, **kw):
        self.created.append((column, kw))
        return {"id": "9"}

    def url(self, _id):
        return "u"


@pytest.fixture
def idea(profile, monkeypatch):
    from pl import commands, events, trackers
    t = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda _name: t)
    monkeypatch.setattr(trackers, "me", lambda _t: "me")
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    args = types.SimpleNamespace(text="Chase Acme invoice", title=None, doc=[], account=None, repo=[], start=True)
    return t, lambda: commands.cmd_idea(args)


def test_an_idea_from_a_read_only_loop_is_filed_unstarted_and_tagged(idea, read_only_loop):
    t, file_idea = idea
    file_idea()
    (column, kw), = t.created
    assert column == "Inbox" and kw["assigned_to"] is None
    assert {"proposed", "loop:invoices"} <= set(kw["tags"])


def test_a_dry_run_idea_is_printed_not_filed(idea, read_only_loop, monkeypatch, capsys):
    t, file_idea = idea
    monkeypatch.setenv("PL_DRY_RUN", "1")
    file_idea()
    assert t.created == [] and "would file: Chase Acme invoice" in capsys.readouterr().out


def _run_record(n=0, **kw):
    return {"started_at": f"2026-10-26T09:{n:02d}:00", "ended_at": f"2026-10-26T09:{n:02d}:30", "result": "ok",
            "tokens": "n/a", "exit": 0, **kw}


def test_a_finished_run_keeps_its_masked_output_and_log(profile, tmux_calls, monkeypatch):
    from pl import loops
    from pl.trackers import mcp
    calls, panes = tmux_calls
    _loop()
    monkeypatch.setattr(mcp, "_secrets", {"s3cr3t-value"})
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    (C.STATE_DIR / "loops" / "invoices.log").write_text("line a\ntoken s3cr3t-value\n3 invoices overdue\n")
    record = C.STATE_DIR / "loops" / "invoices.run.json"
    record.write_text(json.dumps(_run_record(result="token s3cr3t-value")))
    panes["text"] = "@7 %9 sh invoices\n"
    dispatch.ensure_services(st, [], {}, False, None)
    dispatch.ensure_services(st, [], {}, False, None)
    kept = loops.read_runs("invoices")
    assert kept[0]["output"][-1] == "3 invoices overdue"
    folder = loops.run_log_dir("invoices")
    log = folder / kept[0]["log"]
    assert "s3cr3t-value" not in (folder / "runs.json").read_text() and "s3cr3t-value" not in log.read_text()
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600 and stat.S_IMODE(os.stat(folder / "runs.json").st_mode) == 0o600
    assert st["services"]["invoices"]["last_run"]["ok"] is True
    assert "s3cr3t-value" not in json.dumps(st) and "output" not in st["services"]["invoices"]["last_run"]
    assert len(kept) == 1


def test_a_run_log_that_cannot_be_kept_still_ends_the_run(profile, tmux_calls, monkeypatch):
    from pl import events, loops
    calls, panes = tmux_calls
    _loop()
    st = {}
    dispatch.ensure_services(st, [], {}, False, None)
    record = C.STATE_DIR / "loops" / "invoices.run.json"
    record.write_text(json.dumps(_run_record()))
    panes["text"] = "@7 %9 sh invoices\n"
    emitted = []
    monkeypatch.setattr(loops, "keep_run", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, **d: emitted.append((kind, d)))
    dispatch.ensure_services(st, [], {}, False, None)
    assert not record.exists() and st["services"]["invoices"]["last_run"]["exit"] == 0
    assert emitted and emitted[0][0] == "error" and "disk full" in emitted[0][1]["message"]


def test_run_log_keeps_the_last_20_runs(profile):
    from pl import loops
    h = harnesses.BUILTINS["codex"]
    for n in range(22):
        loops.keep_run("invoices", _run_record(n), C.STATE_DIR / "none.log", h)
    kept = loops.read_runs("invoices")
    assert len(kept) == 20 and kept[0]["started_at"].endswith("09:21:00") and kept[-1]["started_at"].endswith("09:02:00")
    assert len(list(loops.run_log_dir("invoices").glob("*.log"))) == 20
    assert not (loops.run_log_dir("invoices") / "20261026090000.log").exists()


def test_failed_runs_get_a_plain_reason(profile):
    from pl import loops
    h = harnesses.Harness("fake", "fake", limit_patterns=[r"usage limit reached"])
    log = C.STATE_DIR / "run.log"
    log.write_text("oops\nUsage limit reached for today\n")
    assert loops.keep_run("a", _run_record(0, exit=1), log, h)["reason"] == "usage limit"
    assert loops.keep_run("a", _run_record(1, exit=124), C.STATE_DIR / "none.log", h)["reason"] == "timeout"
    assert loops.keep_run("a", _run_record(2, exit=3), C.STATE_DIR / "none.log", h)["reason"] == "error (exit 3)"
    ok = loops.keep_run("a", _run_record(3), log, h)
    assert ok["reason"] == "" and ok["ok"] is True


def test_read_runs_of_a_loop_with_no_runs_is_empty(profile):
    from pl import loops
    assert loops.read_runs("nothing") == []
    folder = loops.run_log_dir("bad")
    folder.mkdir(parents=True)
    (folder / "runs.json").write_text("{not json")
    assert loops.read_runs("bad") == []
    (folder / "runs.json").write_text('{"a": 1}')
    assert loops.read_runs("bad") == []


def test_a_scheduled_loop_script_exports_the_xdist_share(profile):
    from pl import manager
    manager.machine_dir().mkdir(parents=True, exist_ok=True)
    (manager.machine_dir() / "machine.toml").write_text("[tests]\nmax_workers = 6\n[limits]\nmax_live_agents = 3\n")
    assert "export PYTEST_XDIST_AUTO_NUM_WORKERS=2\n" in _headless_script("codex")


def _review_loop(**kw):
    C.SERVICES = {"auto-review": {"prompt": "/pl-review repo=x", "profile": "cx", "every": "30m", **kw}}


def _pr_card(cid, *urls, round=None):
    meta = {"pr_urls": list(urls)}
    if round is not None:
        meta["rework"] = {"round": round}
    return {"id": cid, "list_id": "pro", "metadata": meta}


def _starts(calls):
    return len([c for c in calls if c[0] == "new-window"])


@pytest.fixture
def arrivals(monkeypatch):
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "PR open" if lid == "pro" else "Other")


def _state(seen, **kw):
    return {"services": {"auto-review": {"arrivals": list(seen), "next_at": (datetime.now() + timedelta(hours=1)).isoformat(), **kw}}}


def test_a_new_pull_request_starts_auto_review_before_next_at(profile, tmux_calls, arrivals, capsys):
    calls, _ = tmux_calls
    _review_loop()
    st = _state([])
    dispatch.ensure_services(st, [_pr_card("c1", "https://github.com/o/r/pull/1")], {}, False, None)
    assert _starts(calls) == 1
    assert "starting auto-review loop run" in capsys.readouterr().out


def test_a_rework_round_back_in_review_starts_one_run(profile, tmux_calls, arrivals):
    calls, panes = tmux_calls
    _review_loop()
    url = "https://github.com/o/r/pull/1"
    st = _state([url])
    cards = [_pr_card("c1", url, round=2)]
    dispatch.ensure_services(st, cards, {}, False, None)
    assert _starts(calls) == 1
    record = C.STATE_DIR / "loops" / "auto-review.run.json"
    record.write_text(json.dumps({"started_at": "a", "ended_at": "b", "result": "ok", "tokens": "n/a", "exit": 0}))
    panes["text"] = "@7 %9 sh auto-review\n"
    dispatch.ensure_services(st, cards, {}, False, None)
    panes["text"] = ""
    calls.clear()
    dispatch.ensure_services(st, cards, {}, False, None)
    assert _starts(calls) == 0
    assert "run_now" not in st["services"]["auto-review"]


def test_nothing_new_waits_for_next_at(profile, tmux_calls, arrivals):
    calls, _ = tmux_calls
    _review_loop()
    url = "https://github.com/o/r/pull/1"
    dispatch.ensure_services(_state([url]), [_pr_card("c1", url)], {}, False, None)
    assert _starts(calls) == 0


def test_three_new_pull_requests_start_one_run(profile, tmux_calls, arrivals):
    calls, _ = tmux_calls
    _review_loop()
    cards = [_pr_card(f"c{i}", f"https://github.com/o/r/pull/{i}") for i in range(3)]
    dispatch.ensure_services(_state([]), cards, {}, False, None)
    assert _starts(calls) == 1


def test_a_new_pull_request_during_a_run_waits_for_it_to_end(profile, tmux_calls, arrivals):
    calls, panes = tmux_calls
    _review_loop()
    panes["text"] = "@7 %9 sh auto-review\n"
    st = _state([])
    cards = [_pr_card("c1", "https://github.com/o/r/pull/1")]
    dispatch.ensure_services(st, cards, {}, False, None)
    assert _starts(calls) == 0 and st["services"]["auto-review"]["run_now"]
    (C.STATE_DIR / "loops").mkdir()
    record = C.STATE_DIR / "loops" / "auto-review.run.json"
    record.write_text(json.dumps({"started_at": "a", "ended_at": "b", "result": "ok", "tokens": "n/a", "exit": 0}))
    dispatch.ensure_services(st, cards, {}, False, None)
    panes["text"] = ""
    dispatch.ensure_services(st, cards, {}, False, None)
    assert _starts(calls) == 1


def test_review_on_arrival_does_nothing_when_the_loop_is_off(profile, tmux_calls, arrivals, capsys):
    calls, _ = tmux_calls
    C.SERVICES = {}
    st = {"services": {}}
    dispatch.ensure_services(st, [_pr_card("c1", "https://github.com/o/r/pull/1")], {}, False, None)
    assert _starts(calls) == 0 and st == {"services": {}}
    assert capsys.readouterr().err == ""


def test_dry_run_prints_the_review_it_would_start(profile, tmux_calls, arrivals, capsys):
    _review_loop()
    st = _state([])
    before = json.loads(json.dumps(st))
    dispatch.ensure_services(st, [_pr_card("c1", "https://github.com/o/r/pull/1")], {}, True, None)
    assert "would start auto-review loop run" in capsys.readouterr().out
    assert st == before


def test_first_pass_after_upgrade_records_without_running(profile, tmux_calls, arrivals):
    calls, _ = tmux_calls
    _review_loop()
    st = {"services": {"auto-review": {"next_at": (datetime.now() + timedelta(hours=1)).isoformat()}}}
    url = "https://github.com/o/r/pull/1"
    dispatch.ensure_services(st, [_pr_card("c1", url)], {}, False, None)
    assert _starts(calls) == 0 and st["services"]["auto-review"]["arrivals"] == [url]


def test_an_idle_long_lived_review_session_gets_one_turn_typed(profile, tmux_calls, arrivals, monkeypatch):
    calls, panes = tmux_calls
    C.SERVICES = {"auto-review": {"prompt": "/loop 30m /pl-review repo=x", "profile": "cx", "every": None}}
    panes["text"] = "@7 %9 claude auto-review\n"
    typed = []
    monkeypatch.setattr(dispatch, "_type_into", lambda pane, text: typed.append((pane, text)))
    st = _state([])
    cards = [_pr_card("c1", "https://github.com/o/r/pull/1")]
    busy = {"s": {"tmux": "x:%9", "sessionId": "s", "status": "busy"}}
    dispatch.ensure_services(st, cards, busy, False, None)
    assert typed == [] and st["services"]["auto-review"]["run_now"]
    idle = {"s": {"tmux": "x:%9", "sessionId": "s", "status": "idle"}}
    dispatch.ensure_services(st, cards, idle, False, None)
    assert typed == [("%9", "/pl-review repo=x")]
    assert "run_now" not in st["services"]["auto-review"]
