"""Token spend from Claude transcripts (usage numbers and model id only) and loop context care."""
import argparse
import json
import os
import shlex
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from pl import config as C
from pl import dispatch, events, usage
from pl.tui import dashboard, loops

S1 = "11111111-2222-4333-8444-555555555555"
S2 = "22222222-2222-4333-8444-555555555555"
LOOP_SID = "33333333-2222-4333-8444-555555555555"
TOKEN = "sk-ant-" + "Q9w8E7r6T5y4U3i2O1p0" * 2


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    C.PROFILES = {"main": tmp_path / ".claude-main", "cx": tmp_path / ".codex-home"}
    C.ACCOUNTS = {"main": {"harness": "claude"}, "cx": {"harness": "codex"}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _ts(ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat().replace("+00:00", "Z")


def turn(mid, inp=100, out=10, cr=0, cw=0, model="claude-test-1", sid=S1, ago=0, text="ok"):
    return {"type": "assistant", "sessionId": sid, "timestamp": _ts(ago), "requestId": "req-" + mid,
            "message": {"id": mid, "role": "assistant", "model": model, "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw}}}


def transcript(home, sid=S1, project="-Users-me-code"):
    d = home / ".claude-main" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.jsonl"


def write(f, entries, mode="a"):
    with open(f, mode) as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in entries))


def total(st, by="account", since=86400, cards=None):
    return {k: sum(v[:4]) for k, v in usage.totals(st, time.time() - since, by, cards).items()}


def test_incremental_reads_never_recount(fake_home):
    f = transcript(fake_home)
    # one API reply written as two entries (two content blocks) with the same message id counts once
    write(f, [turn("m1", inp=100, out=10), turn("m1", inp=100, out=10), {"type": "user", "message": {"role": "user", "content": "hi"}}])
    assert total(usage.scan()) == {"main": 110}
    assert total(usage.scan()) == {"main": 110}                 # nothing new: nothing added
    line = json.dumps(turn("m2", inp=1000, out=1))
    with open(f, "a") as fh:
        fh.write(line[:40])                                       # a half-written line is not read yet
    assert total(usage.scan()) == {"main": 110}
    with open(f, "a") as fh:
        fh.write(line[40:] + "\n")
    assert total(usage.scan()) == {"main": 1111}
    offsets = json.loads((C.STATE_DIR / "usage.json").read_text())["files"]
    assert list(offsets.values())[0]["offset"] == f.stat().st_size


def test_subagent_transcripts_count_toward_the_parent_session(fake_home):
    d = fake_home / ".claude-main" / "projects" / "-Users-me-code" / S1 / "subagents"
    d.mkdir(parents=True)
    write(d / "agent-abc.jsonl", [turn("a1", inp=5, out=5, cr=190_000)])
    write(transcript(fake_home), [turn("m1", inp=10, out=0, cr=1000)])
    st = usage.scan()
    assert total(st, "session") == {S1: 191_020}
    assert usage.context_pct(st, S1) == 1                        # the parent's own last turn, not the sub-agent's


def test_card_and_loop_mapping(fake_home):
    write(transcript(fake_home, S1), [turn("m1", inp=100, out=0)])
    write(transcript(fake_home, S2), [turn("m2", inp=20, out=0, sid=S2)])
    write(transcript(fake_home, LOOP_SID), [turn("m3", inp=3, out=0, sid=LOOP_SID)])
    write(transcript(fake_home, "44444444-2222-4333-8444-555555555555"), [turn("m4", inp=1, out=0, sid="44444444-2222-4333-8444-555555555555")])
    events.emit("started", "abcdef0123456789", stage="spec", harness="claude", session=S1)
    cards = [{"id": "fedcba9876543210", "title": "T", "metadata": {"worker": {"stage": "plan", "session_id": S2}}}]
    C.STATE_FILE.parent.mkdir(exist_ok=True)
    C.STATE_FILE.write_text(json.dumps({"services": {"triage": {"profile": "main", "sessions": [LOOP_SID]}}}))
    st = usage.scan()
    m = usage.session_map(cards)
    assert total(st, "card", cards=cards) == {"abcdef01 spec": 100, "fedcba98 plan": 20, "loop triage": 3, "other": 1}
    assert total(st, "loop") == {"triage": 3, "not a loop": 121}
    assert m[S2] == ("card", "fedcba98 plan")


def test_price_math(fake_home):
    write(transcript(fake_home), [turn("m1", inp=1_000_000, out=500_000, cr=400_000, cw=100_000, model="claude-big-2"),
                                  turn("m2", inp=1_000_000, out=0, model="other-model")])
    C.USAGE = {"prices": {"claude-big": 3}}                     # a prefix covers every model id starting with it
    assert usage.cost("claude-big-2", 2_000_000) == pytest.approx(6.0)
    assert usage.cost("other-model", 1_000_000) is None
    out = usage.table(usage.scan(), time.time() - 3600, "model")
    assert "$6.00" in out and "claude-big-2" in out
    row = next(line for line in out.splitlines() if line.startswith("other-model"))
    assert row.rstrip().endswith("-")


def test_secret_text_never_appears_in_output_or_state(fake_home, capsys):
    f = transcript(fake_home)
    bad = turn("m1", text=f"here is {TOKEN}", model=TOKEN)
    bad["message"]["content"].append({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": f"echo {TOKEN}"}})
    write(f, [bad, {"type": "user", "message": {"role": "user", "content": TOKEN}}])
    # a credential file hard-linked into projects is never read
    root = fake_home / ".claude-main"
    (root / ".credentials.json").write_text(json.dumps(turn("m9", inp=777, model=TOKEN)) + "\n")
    os.link(root / ".credentials.json", root / "projects" / "-Users-me-code" / (S2 + ".jsonl"))
    st = usage.scan()
    usage.cmd_usage(argparse.Namespace(since="24h", by="model"))
    out = capsys.readouterr().out
    assert TOKEN not in out and TOKEN not in (C.STATE_DIR / "usage.json").read_text()
    assert "unknown" in out and "777" not in out
    assert total(st) == {"main": 110}


def test_a_linked_projects_folder_is_skipped(fake_home):
    real = fake_home / "elsewhere" / "p"
    real.mkdir(parents=True)
    write(real / f"{S1}.jsonl", [turn("m1")])
    (fake_home / ".claude-main").mkdir()
    (fake_home / ".claude-main" / "projects").symlink_to(fake_home / "elsewhere")
    assert total(usage.scan()) == {}


def test_context_percent(fake_home):
    write(transcript(fake_home), [turn("m1", inp=10, cr=10), turn("m2", inp=10_000, out=99_999, cr=100_000, cw=10_000)])
    st = usage.scan()
    assert usage.context_pct(st, S1) == 60                       # input + cache of the last turn over 200k; output is not context
    C.USAGE = {"windows": {"claude-test": 1_000_000}}
    assert usage.context_pct(st, S1) == 12
    assert usage.context_pct(st, "no-such-session") is None


def test_other_harnesses_show_na(fake_home, capsys):
    write(transcript(fake_home), [turn("m1")])
    usage.cmd_usage(argparse.Namespace(since="24h", by="account"))
    out = capsys.readouterr().out
    assert "main" in out and "110" in out
    assert next(line for line in out.splitlines() if line.startswith("cx")).split()[1:] == ["n/a"] * 5
    line = dashboard.health_tokens({"usage": usage.summary(usage.scan())}, 1)
    assert line == ("tokens 24h", "110 (main 110 · cx n/a)")


def _loop_env(monkeypatch, status, pct_tokens, mid="m1"):
    """One running loop window %5 whose Claude session LOOP_SID is idle or busy; its last turn holds pct_tokens."""
    write(transcript(C.PROFILES["main"].parent, LOOP_SID), [turn(mid, inp=pct_tokens, out=5, sid=LOOP_SID)])
    killed = []

    def tmux(*args, check=True):
        if args[0] == "kill-window":
            killed.append(args[-1])
        if args[0] == "send-keys" and "-l" in args:
            assert shlex.split(args[-1])[0] == "sh"               # only the launch line: nothing is typed into a loop
        return {"new-window": "@8", "list-panes": "%6"}.get(args[0], "")

    def run(argv, **kw):
        return types.SimpleNamespace(returncode=0, stdout="@1 %5 node triage\n" if "list-panes" in argv else "", stderr="")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(dispatch.subprocess, "run", run)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda *a: None)
    C.SERVICES = {"triage": {"prompt": "/loop 30m /triage", "profile": "main", "max_context": 60}}
    reg = {LOOP_SID: {"sessionId": LOOP_SID, "status": status, "tmux": "pl:%5"}}
    return killed, reg


def test_idle_loop_over_max_context_restarts_fresh(fake_home, monkeypatch):
    write(transcript(fake_home, LOOP_SID), [turn("m0", inp=10, out=1, sid=LOOP_SID)])   # the session started small
    killed, reg = _loop_env(monkeypatch, "idle", 150_000)
    st = {}
    dispatch.ensure_services(st, [], reg, False, None)
    assert killed == ["@1"]
    assert st["services"]["triage"]["sessions"] == [LOOP_SID]
    ev = [e for e in events._read() if e["kind"] == "loop_restart"]
    assert ev and ev[0]["loop"] == "triage" and ev[0]["context"] == 75


def test_no_restart_while_busy_or_under_the_limit(fake_home, monkeypatch):
    write(transcript(fake_home, LOOP_SID), [turn("m0", inp=10, out=1, sid=LOOP_SID)])
    killed, reg = _loop_env(monkeypatch, "busy", 190_000)
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == []
    killed, reg = _loop_env(monkeypatch, "idle", 100_000, mid="m2")       # 50%, under max_context 60
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == []
    C.SERVICES["triage"]["max_context"] = 40
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == ["@1"]


def test_loops_tab_shows_context_tokens_and_dead(fake_home):
    write(transcript(fake_home, LOOP_SID), [turn("m1", inp=120_000, out=0, sid=LOOP_SID)])
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    C.STATE_FILE.parent.mkdir(exist_ok=True)
    C.STATE_FILE.write_text(json.dumps({"services": {"triage": {"sessions": [LOOP_SID], "started_at": old},
                                                     "quiet": {"sessions": [S2], "started_at": old}}}))
    C.SERVICES = {"triage": {"prompt": "/loop 30m /triage", "profile": "main"},
                  "quiet": {"prompt": "/loop 30m /quiet", "profile": "main"}}
    on = [{"loop": n, "kind": "loop_idle", "worker": {"pane": p, "window": w}} for n, p, w in (("triage", "%1", "@1"), ("quiet", "%2", "@2"))]
    rows = {r["name"]: r for r in loops.loop_rows({"snapshot": {"rows": on}, "usage": usage.summary(usage.scan())})}
    assert (rows["triage"]["context"], rows["triage"]["tokens"]) == ("60%", "120.0k")
    assert rows["quiet"]["state"] == "dead?" and rows["quiet"]["context"] == "n/a"   # no turn seen yet


# ---- round 1 fixes ----

S3 = "55555555-2222-4333-8444-555555555555"


def _ago_iso(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_a_session_past_200k_is_measured_against_a_1m_window(fake_home):
    write(transcript(fake_home), [turn("m1", inp=10, cr=250_000, model="claude-opus-4-6"),
                                  turn("m2", inp=10, cr=500_000, model="claude-opus-4-6")])
    write(transcript(fake_home, S2), [turn("m3", inp=150_000, sid=S2, model="claude-opus-4-6")])
    st = usage.scan()
    assert usage.context_pct(st, S1) == 50                       # 500k of 1M, never 250%
    assert usage.context_pct(st, S2) == 75                       # never past 200k: still 200k


def test_a_loop_without_max_context_restarts_fresh_over_80_percent(fake_home, monkeypatch):
    assert usage.MAX_CONTEXT == 80
    write(transcript(fake_home, LOOP_SID), [turn("m0", inp=10, out=1, sid=LOOP_SID)])
    killed, reg = _loop_env(monkeypatch, "idle", 150_000)             # 75%: under the default 80
    del C.SERVICES["triage"]["max_context"]
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == []
    killed, reg = _loop_env(monkeypatch, "idle", 190_000, mid="m2")   # 95%: over it
    del C.SERVICES["triage"]["max_context"]
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == ["@1"]


def test_max_context_0_turns_context_care_off(fake_home, monkeypatch):
    write(transcript(fake_home, LOOP_SID), [turn("m0", inp=10, out=1, sid=LOOP_SID)])
    killed, reg = _loop_env(monkeypatch, "idle", 190_000)
    C.SERVICES["triage"]["max_context"] = 0
    dispatch.ensure_services({}, [], reg, False, None)
    assert killed == []
    rows = {r["name"]: r for r in loops.loop_rows({"snapshot": {"rows": [{"loop": "triage", "kind": "loop_idle", "worker": {}}]},
                                                    "usage": {"loops": {"triage": {"context": 95, "tokens_1h": 1, "dead": False}}}})}
    assert rows["triage"]["context"] == "95%"                    # the column shows either way


@pytest.mark.parametrize("v, ok", [(0, True), (80, True), (100, True), (-1, False), (101, False), (True, False)])
def test_config_accepts_max_context_0_to_100(v, ok):
    import tomlkit
    doc = tomlkit.parse('[loops.triage]\nprompt = "/loop 30m /triage"\n')
    doc["loops"]["triage"]["max_context"] = v
    assert (not any("max_context" in e for e in C.validate(doc))) is ok


def _shared(fake_home):
    """Account two's projects folder is a link to main's; each account's sessions/*.json names its own sessions."""
    C.PROFILES = {"main": fake_home / ".claude-main", "two": fake_home / ".claude-two", "cx": fake_home / ".codex-home"}
    C.ACCOUNTS = {"main": {"harness": "claude"}, "two": {"harness": "claude"}, "cx": {"harness": "codex"}}
    write(transcript(fake_home, S1), [turn("m1", inp=100, out=0)])
    write(transcript(fake_home, S2), [turn("m2", inp=20, out=0, sid=S2)])
    write(transcript(fake_home, S3), [turn("m3", inp=3, out=0, sid=S3)])
    two = fake_home / ".claude-two"
    two.mkdir()
    (two / "projects").symlink_to(fake_home / ".claude-main" / "projects")
    for root, sid in ((fake_home / ".claude-main", S1), (two, S2)):
        (root / "sessions").mkdir()
        (root / "sessions" / "1.json").write_text(json.dumps({"sessionId": sid, "pid": 1}))


def test_a_shared_projects_folder_is_read_once_and_split_by_account(fake_home):
    _shared(fake_home)
    st = usage.scan()
    assert total(st) == {"main": 100, "two": 20, "main+two": 3}
    line = dashboard.health_tokens({"usage": usage.summary(st)}, 1)
    assert line == ("tokens 24h", "123 (main 100 · two 20 · main+two 3 · cx n/a)")


def test_health_shows_every_configured_account(fake_home):
    C.PROFILES["idle"] = fake_home / ".claude-idle"
    C.ACCOUNTS["idle"] = {"harness": "claude"}
    write(transcript(fake_home), [turn("m1")])
    line = dashboard.health_tokens({"usage": usage.summary(usage.scan())}, 1)
    assert line == ("tokens 24h", "110 (main 110 · cx n/a · idle n/a)")


def test_context_restarts_wait_one_interval_and_cap_at_3_an_hour(fake_home, monkeypatch):
    write(transcript(fake_home, LOOP_SID), [turn("m0", inp=10, out=1, sid=LOOP_SID)])
    killed, reg = _loop_env(monkeypatch, "idle", 150_000)
    st = {"services": {"triage": {"profile": "main", "started_at": _ago_iso(600)}}}   # under one 30m interval
    dispatch.ensure_services(st, [], reg, False, None)
    assert killed == []
    now = time.time()
    st = {"services": {"triage": {"profile": "main", "started_at": _ago_iso(3600), "context_restarts": [now - 60, now - 120, now - 180]}}}
    dispatch.ensure_services(st, [], reg, False, None)
    assert killed == []
    st = {"services": {"triage": {"profile": "main", "started_at": _ago_iso(3600), "context_restarts": [now - 4000]}}}
    dispatch.ensure_services(st, [], reg, False, None)
    assert killed == ["@1"]
    assert len(st["services"]["triage"]["context_restarts"]) == 1   # the old one fell out of the hour


def test_a_fresh_session_over_the_limit_warns_instead_of_restarting(fake_home, monkeypatch):
    killed, reg = _loop_env(monkeypatch, "idle", 150_000)          # its first turn is already 75%
    st = {"services": {"triage": {"profile": "main", "started_at": _ago_iso(3600)}}}
    dispatch.ensure_services(st, [], reg, False, None)
    dispatch.ensure_services(st, [], reg, False, None)
    assert killed == []
    warn = [e for e in events._read() if e["kind"] == "loop_context_warning"]
    assert len(warn) == 1 and warn[0]["loop"] == "triage" and warn[0]["context"] == 75


def test_dry_run_writes_no_usage_state(fake_home, monkeypatch):
    killed, reg = _loop_env(monkeypatch, "idle", 150_000)
    dispatch.ensure_services({}, [], reg, True, None)
    assert not (C.STATE_DIR / "usage.json").exists()


def test_dead_only_for_claude_loops_with_recorded_sessions(fake_home):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    C.STATE_FILE.parent.mkdir(exist_ok=True)
    C.STATE_FILE.write_text(json.dumps({"services": {"nosess": {"started_at": old}, "cxl": {"started_at": old, "sessions": [S2]}}}))
    C.SERVICES = {"nosess": {"prompt": "/loop 30m /a", "profile": "main"}, "cxl": {"prompt": "/loop 30m /b", "profile": "cx"}}
    s = usage.summary(usage.scan())
    assert not s["loops"]["nosess"]["dead"] and not s["loops"]["cxl"]["dead"]


def test_the_dispatcher_never_loads_textual_for_usage(fake_home):
    import subprocess
    import sys
    line = json.dumps(turn("m1")).encode()
    code = f"import sys; from pl import usage; usage._turn({line!r}); print('textual' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "HOME": str(fake_home)})
    assert r.stdout.strip() == "False", r.stderr


def test_the_same_message_id_in_two_files_of_one_session_counts_once(fake_home):
    write(transcript(fake_home), [turn("m1", inp=100, out=0)])
    write(transcript(fake_home, project="-Users-me-other"), [turn("m1", inp=100, out=0)])
    assert total(usage.scan()) == {"main": 100}


def test_bad_message_and_session_ids_are_not_stored(fake_home):
    write(transcript(fake_home), [turn("x" * 65, inp=1, out=0), turn("a b", inp=1, out=0, sid="not-a-uuid")])
    st = usage.scan()
    raw = (C.STATE_DIR / "usage.json").read_text()
    assert "x" * 65 not in raw and "a b" not in raw and "not-a-uuid" not in raw
    assert total(st, "session") == {S1: 1, "unknown": 1}


def test_a_bad_usage_state_file_is_ignored(fake_home):
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (C.STATE_DIR / "usage.json").write_text(json.dumps({"files": [], "buckets": {"x": "y"}, "last": 3, "ids": []}))
    write(transcript(fake_home), [turn("m1")])
    assert total(usage.scan()) == {"main": 110}


def test_the_console_survives_any_usage_error(fake_home, monkeypatch):
    from pl.tui import app

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(usage, "scan", boom)
    assert app._usage() is None


def test_a_held_lock_skips_the_scan(fake_home, monkeypatch):
    import fcntl
    write(transcript(fake_home), [turn("m1")])
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(C.STATE_DIR / "usage.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    monkeypatch.setattr(usage, "LOCK_WAIT", 0.2)
    try:
        assert total(usage.scan()) == {}
    finally:
        os.close(fd)
    assert total(usage.scan()) == {"main": 110}


def test_a_shorter_or_replaced_file_is_read_from_the_start(fake_home):
    f = transcript(fake_home)
    write(f, [turn("m1", inp=100, out=0), turn("m2", inp=100, out=0)])
    assert total(usage.scan()) == {"main": 200}
    write(f, [turn("m3", inp=5, out=0)], mode="w")               # same file, now shorter
    assert total(usage.scan()) == {"main": 205}
    f.unlink()
    write(f, [turn("m4", inp=7, out=0), turn("m5", inp=7, out=0), turn("m6", inp=7, out=0)])   # a new file, longer
    assert total(usage.scan()) == {"main": 226}


def test_a_replaced_longer_file_with_a_reused_inode_is_read_from_the_start(fake_home):
    f = transcript(fake_home)
    write(f, [turn("m1", inp=100, out=0)])
    assert total(usage.scan()) == {"main": 100}
    f.unlink()
    write(f, [turn("m2", inp=7, out=0), turn("m3", inp=7, out=0)])
    st = json.loads(usage._path().read_text())
    new = f.stat()
    st["files"][str(f)]["ident"] = [new.st_dev, new.st_ino]   # as on Linux, where the new file gets the old inode
    usage._path().write_text(json.dumps(st))
    assert total(usage.scan()) == {"main": 114}


def test_a_line_longer_than_one_read_is_skipped(fake_home, monkeypatch):
    monkeypatch.setattr(usage, "CHUNK", 600)
    f = transcript(fake_home)
    write(f, [turn("big", inp=999, out=0, text="y" * 2000), turn("m1", inp=100, out=0)])
    for _ in range(6):
        usage.scan()
    assert total(usage.scan()) == {"main": 100}


def test_old_buckets_and_files_are_dropped_after_14_days(fake_home):
    f = transcript(fake_home)
    write(f, [turn("m0", inp=50, out=0, ago=15 * 86400), turn("m1", inp=100, out=0)])
    st = usage.scan()
    assert total(st, since=30 * 86400) == {"main": 100}
    old = transcript(fake_home, S2)
    write(old, [turn("m2", inp=9, out=0, sid=S2)])
    t = time.time() - 15 * 86400
    os.utime(old, (t, t))
    assert total(usage.scan(), since=30 * 86400) == {"main": 100}
