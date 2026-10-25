"""Stage caps: [caps.<stage>] in the profile, enforced by the dispatcher (stopped like pl drop, card to Manual), shown by pl card."""
import argparse
import io
import json
import time
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from pl import agents, commands, dispatch, events, move_agent, usage
from pl import config as C
from pl.util import save_state

RUN_W = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "attempts": 1}


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _card(cid, worker, col="In progress"):
    return {"id": cid, "title": f"card {cid}", "list_id": col, "tags": [], "updated_at": "2026-10-24", "description": "# PIPELINE: PLAN\nWP1",
            "metadata": {"pipeline_mode": "auto", "profile": "acme", "worker": worker}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"acme": tmp_path / ".claude"}
    C.ACCOUNTS = {"acme": {"harness": "claude"}}
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake(monkeypatch):
    ns = types.SimpleNamespace(writes=[], stops=[], tokens={}, events=[])
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: None)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: None)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: None)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: ns.writes.append((cid, f)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(move_agent, "stop", lambda pane, sid: ns.stops.append((pane, sid)) or True)
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))
    monkeypatch.setattr(usage, "_load", lambda: {"buckets": {f"{_iso(time.time())[:13]}|acme|m|{sid}": [n, 0, 0, 0] for sid, n in ns.tokens.items()}})
    monkeypatch.setattr(dispatch, "cards", lambda: ns.cards)
    ns.run = lambda cs: (setattr(ns, "cards", cs), dispatch.dispatch_once(1, False, pull=False))
    return ns


def _moved(ns, cid):
    return next(f for c, f in ns.writes if c == cid and f.get("column") == "Manual")


# ---------- the profile's caps ----------

def test_parse_caps_config(tmp_path):
    (tmp_path / ".pl-t" / "config.toml").write_text('[caps.run]\nminutes = 90\ntokens = "2M"\n[caps.spec]\nminutes = 30\ntokens = "500k"\n[caps.plan]\ntokens = 4000\n')
    C.load("t")
    assert C.CAPS == {"run": {"minutes": 90, "tokens": 2_000_000}, "spec": {"minutes": 30, "tokens": 500_000}, "plan": {"tokens": 4000}}


def test_no_caps_by_default():
    assert C.CAPS == {}


@pytest.mark.parametrize("raw,want", [("2M", 2_000_000), ("1.5m", 1_500_000), ("500k", 500_000), (7, 7), ("1B", 10**9), ("x", None), (True, None)])
def test_parse_tokens(raw, want):
    assert C.parse_tokens(raw) == want


def test_ignore_repo_caps_warning(capsys):
    kept = C.without_repo_caps({"caps": {"run": {"minutes": 1}}, "image": "x"}, ".pl/e2e.yaml")
    assert kept == {"image": "x"}
    assert ".pl/e2e.yaml" in capsys.readouterr().err
    assert C.without_repo_caps({"image": "x"}, ".pl/e2e.yaml") == {"image": "x"}


# ---------- the dispatcher stops an agent at its cap ----------

def test_dispatch_stops_at_time_cap(fake):
    C.CAPS = {"run": {"minutes": 90}}
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 91 * 60)})])
    assert fake.stops == [("%1", "s1")]
    moved = _moved(fake, "aaaa0001")
    assert "stopped at the 90-minute cap" in moved["description"]
    meta = next(f["metadata"] for c, f in fake.writes if "metadata" in f and f["metadata"].get("cap_note"))
    assert meta["cap_note"] == "stopped at the 90-minute cap" and meta["worker"] is None


def test_under_the_cap_nothing_happens(fake):
    C.CAPS = {"run": {"minutes": 90, "tokens": 2_000_000}}
    fake.tokens = {"s1": 1_000}
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 10 * 60)})])
    assert fake.stops == [] and fake.writes == []


def test_no_caps_for_the_stage_nothing_happens(fake):
    C.CAPS = {"spec": {"minutes": 1}}
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 600 * 60)})])
    assert fake.stops == []


def test_dispatch_stops_at_token_cap(fake):
    C.CAPS = {"run": {"tokens": 2_000_000}}
    fake.tokens = {"s1": 2_500_000}
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 60)})])
    assert fake.stops == [("%1", "s1")]
    assert "token cap" in _moved(fake, "aaaa0001")["description"]


def test_agy_skips_token_cap(fake):
    C.CAPS = {"run": {"tokens": 10}}
    fake.tokens = {"s1": 10_000}
    fake.run([_card("aaaa0001", {**RUN_W, "harness": "agy", "started_at": _iso(time.time() - 60)})])
    assert fake.stops == []


def test_parked_time_excluded_from_time_cap(fake):
    C.CAPS = {"run": {"minutes": 90}}
    started = time.time() - 100 * 60
    save_state({"notified": {}, "cap_clock": {"aaaa0001": {"started_at": _iso(started), "at": time.time() - 120, "parked": 30 * 60}}})
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(started), "limit_hit": {"account": "acme", "until": "x"}})])
    assert fake.stops == []   # 100 minutes old, 32 of them parked: 68 active


# ---------- pl card ----------

def _card_out(monkeypatch, c, as_json):
    monkeypatch.setattr(commands, "find_card", lambda ref: c)
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(usage, "_load", lambda: {"buckets": {}})
    out = io.StringIO()
    with redirect_stdout(out):
        commands.cmd_card(argparse.Namespace(id=c["id"], delete=False, json=as_json))
    return out.getvalue()


def test_card_json_includes_caps_and_spent(monkeypatch):
    C.CAPS = {"run": {"minutes": 90, "tokens": 2_000_000}}
    c = _card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 600)})
    data = json.loads(_card_out(monkeypatch, c, True))
    assert data["caps"] == {"minutes": 90, "tokens": 2_000_000}
    assert data["spent"]["tokens"] == 0 and 9 <= data["spent"]["minutes"] <= 11


def test_card_text_shows_caps_and_harness_note(monkeypatch):
    C.CAPS = {"run": {"minutes": 90, "tokens": 2_000_000}}
    c = _card("aaaa0001", {**RUN_W, "harness": "codex", "started_at": _iso(time.time() - 600)})
    out = _card_out(monkeypatch, c, False)
    assert "tokens: not counted for this harness" in out and "90 min" in out
    assert json.loads(_card_out(monkeypatch, c, True))["spent"]["tokens"] is None


# ---------- card overrides: pl caps set / reset ----------

def _caps_cmd(monkeypatch, c, **args):
    writes = []
    monkeypatch.setattr(commands, "find_card", lambda ref: c)
    monkeypatch.setattr(commands, "update", lambda cid, **f: writes.append((cid, f)))
    commands.cmd_caps(argparse.Namespace(id=c["id"], minutes=args.get("minutes"), tokens=args.get("tokens"), caps_cmd=args["cmd"]))
    return writes


def test_caps_set_and_reset_metadata(monkeypatch):
    c = _card("aaaa0001", None)
    assert _caps_cmd(monkeypatch, c, cmd="set", minutes="180", tokens="4M") == [("aaaa0001", {"metadata": {"caps": {"minutes": 180, "tokens": 4_000_000}}})]
    c["metadata"]["caps"] = {"minutes": 180, "tokens": 4_000_000}
    assert _caps_cmd(monkeypatch, c, cmd="set", minutes="240")[0][1]["metadata"]["caps"] == {"minutes": 240, "tokens": 4_000_000}
    assert _caps_cmd(monkeypatch, c, cmd="reset") == [("aaaa0001", {"metadata": {"caps": None}})]


@pytest.mark.parametrize("minutes,tokens", [("-5", None), ("0", None), ("abc", None), ("1.5", None), (None, "-4M"), (None, "4X"), (None, "0"), (None, None)])
def test_caps_set_invalid_inputs_exit_1(monkeypatch, minutes, tokens):
    c = _card("aaaa0001", None)
    writes = []
    monkeypatch.setattr(commands, "update", lambda cid, **f: writes.append(f))
    monkeypatch.setattr(commands, "find_card", lambda ref: c)
    with pytest.raises(SystemExit) as e:
        commands.cmd_caps(argparse.Namespace(id="aaaa0001", minutes=minutes, tokens=tokens, caps_cmd="set"))
    assert isinstance(e.value.code, str)   # a message on stderr and exit code 1
    assert writes == []


def test_caps_set_unknown_card_exits_1(monkeypatch):
    def missing(ref):
        raise commands.BadInput("pl: 0 cards match 'zzz'")
    monkeypatch.setattr(commands, "find_card", missing)
    with pytest.raises(SystemExit) as e:
        commands.cmd_caps(argparse.Namespace(id="zzz", minutes="5", tokens=None, caps_cmd="set"))
    assert isinstance(e.value.code, str)


def test_card_json_reflects_card_caps(monkeypatch):
    C.CAPS = {"run": {"minutes": 90, "tokens": 2_000_000}}
    c = _card("aaaa0001", None)
    c["metadata"]["caps"] = {"minutes": 180, "tokens": 4_000_000}
    assert json.loads(_card_out(monkeypatch, c, True))["caps"] == {"minutes": 180, "tokens": 4_000_000}
    del c["metadata"]["caps"]
    assert json.loads(_card_out(monkeypatch, c, True))["caps"] == {"minutes": 90, "tokens": 2_000_000}


# ---------- the dispatcher and the card's override ----------

def test_dispatch_enforces_card_override_caps(fake):
    C.CAPS = {"run": {"minutes": 90}}
    started = _iso(time.time() - 100 * 60)
    longer = _card("aaaa0001", {**RUN_W, "started_at": started})
    longer["metadata"]["caps"] = {"minutes": 180}
    shorter = _card("aaaa0002", {**RUN_W, "session_id": "s2", "pane": "%2", "started_at": _iso(time.time() - 40 * 60)})
    shorter["metadata"]["caps"] = {"minutes": 30}
    fake.run([longer, shorter])
    assert fake.stops == [("%2", "s2")]
    assert "stopped at the 30-minute cap" in _moved(fake, "aaaa0002")["description"]
    assert not any(c == "aaaa0001" for c, _ in fake.writes)


def test_cap_stop_records_where_the_card_came_from(fake):
    C.CAPS = {"run": {"minutes": 10}}
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 20 * 60)})])
    meta = next(f["metadata"] for c, f in fake.writes if "metadata" in f and f["metadata"].get("cap_note"))
    assert meta["cap_stopped"] is True and meta["manual_from_column"] == "In progress"


def test_near_cap_80_percent_alert_opens_without_duplicates(fake, monkeypatch):
    C.CAPS = {"run": {"minutes": 100}}
    sent = []
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: sent.append(a))
    cs = [_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 85 * 60)})]
    fake.run(cs)
    fake.run(cs)
    assert fake.stops == [] and len(sent) == 1
    assert alerts_open() == ["near_cap:aaaa0001:run"]


def test_near_cap_alert_on_tokens(fake, monkeypatch):
    C.CAPS = {"run": {"tokens": 1_000_000}}
    fake.tokens = {"s1": 850_000}
    sent = []
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: sent.append(a))
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 60)})])
    assert len(sent) == 1 and fake.stops == []


def test_below_80_percent_no_alert(fake, monkeypatch):
    C.CAPS = {"run": {"minutes": 100}}
    sent = []
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: sent.append(a))
    fake.run([_card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 70 * 60)})])
    assert sent == [] and alerts_open() == []


def alerts_open():
    from pl import alerts
    return [k for k, a in alerts._load().items() if alerts._live(a)]


# ---------- pl retry resumes a card stopped at a cap ----------

def test_retry_resumes_cap_stopped_card_from_manual(monkeypatch):
    c = _card("aaaa0001", None, col="Manual")
    c["metadata"].update(cap_stopped=True, manual_from_column="In progress", cap_note="stopped at the 90-minute cap",
                         manual_reason="stopped at the 90-minute cap", caps={"minutes": 240})
    writes = []
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "cards", lambda: [c])
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "update", lambda cid, **f: writes.append((cid, f)))
    out = commands.retry("aaaa0001")
    assert writes == [("aaaa0001", {"column": "In progress", "metadata": {"cap_stopped": None, "manual_from_column": None, "cap_note": None, "manual_reason": None, "worker": None}})]
    assert "In progress" in out[0]


# ---------- pl usage --by card --json ----------

def test_usage_by_card_json_reports_spent_and_caps(monkeypatch, capsys):
    C.CAPS = {"run": {"minutes": 90, "tokens": 2_000_000}}
    c = _card("aaaa0001", {**RUN_W, "started_at": _iso(time.time() - 600)})
    c["metadata"]["caps"] = {"minutes": 180}
    monkeypatch.setattr(usage, "scan", lambda: {"buckets": {f"{_iso(time.time())[:13]}|acme|m|s1": [700, 300, 0, 0]}})
    monkeypatch.setattr(usage, "_load", lambda: {"buckets": {f"{_iso(time.time())[:13]}|acme|m|s1": [700, 300, 0, 0]}})
    monkeypatch.setattr(usage.events, "_read", lambda: [])
    monkeypatch.setattr("pl.board.cards", lambda: [c])
    monkeypatch.setattr("pl.board.col_name", lambda lid: lid)
    usage.cmd_usage(argparse.Namespace(by="card", since="24h", json=True, github=False))
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["card"] == "aaaa0001" and rows[0]["stage"] == "run"
    assert rows[0]["tokens"] == 1000 and 9 <= rows[0]["minutes"] <= 11
    assert rows[0]["caps"] == {"minutes": 180, "tokens": 2_000_000}
