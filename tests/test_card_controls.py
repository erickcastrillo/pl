"""pl restart (stop a card's agent so its stage starts fresh), pl hold / pl unhold (the parked tag) and pl adopt."""
import argparse
import copy
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pl import agents, commands, move_agent, trackers
from pl import config as C

A_ID = "aaaa1111-0000-0000-0000-000000000001"
W = {"stage": "plan", "session_id": "s1", "pane": "%5", "window": "@7", "profile": "acme", "attempts": 2,
     "started_at": "2026-09-30T10:00:00+00:00"}


class FakeTracker:
    def __init__(self):
        self.items, self.writes = {}, []

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        self.writes.append((item_id, copy.deepcopy(fields)))
        c = self.items[item_id]
        if "metadata" in fields:
            c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
        c.update(fields)
        return copy.deepcopy(c)


def mk(col, tags=(), **meta):
    return {"id": A_ID, "title": "card one", "description": "", "tags": list(tags), "list_id": f"col-{col}",
            "updated_at": "", "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.USER_EMAIL = "me@example.test"
    monkeypatch.setattr(commands, "registry", lambda: {})
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


@pytest.fixture
def agent(monkeypatch):
    got = {"stopped": [], "ok": True, "refusal": None, "status": "alive", "locked": []}
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: (got["status"], w.get("session_id")))
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: got["refusal"])
    monkeypatch.setattr(move_agent, "stop", lambda pane, sid: got["stopped"].append((pane, sid)) or got["ok"])
    monkeypatch.setattr(move_agent, "lock", lambda cid: got["locked"].append(cid) or True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: got["locked"].append(("unlock", cid)))
    return got


def events():
    p = C.STATE_DIR / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def run_cli(fn, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(argparse.Namespace(**kw))
    return out.getvalue()


# ---------- restart ----------

def test_restart_stops_a_live_agent_and_clears_its_worker_so_the_stage_starts_fresh(board, agent):
    board.items[A_ID] = mk("Spec ready", worker=dict(W))
    lines = commands.restart(A_ID[:8])
    m = board.items[A_ID]["metadata"]
    assert agent["stopped"] == [("%5", "s1")]
    assert m["worker"] is None and m["finished_workers"] == [W]   # the dispatcher's cleanup closes the window
    assert agent["locked"] == [A_ID, ("unlock", A_ID)]
    assert "stopped its plan agent" in lines[0] and "attempt 1" in lines[0]
    assert [(e["kind"], e["stage"], e["reason"]) for e in events() if e["kind"] == "agent_restarted"] == [("agent_restarted", "plan", "by hand")]


def test_restart_of_a_dead_agent_just_clears_it(board, agent):
    agent["status"] = "dead"
    board.items[A_ID] = mk("Spec ready", worker=dict(W))
    commands.restart(A_ID[:8])
    assert agent["stopped"] == [] and board.items[A_ID]["metadata"]["worker"] is None


@pytest.mark.parametrize("refusal, ok", [("its pane %5 is not in window @7", True), (None, False)])
def test_restart_refuses_when_the_agent_cannot_be_stopped(board, agent, refusal, ok):
    agent["refusal"], agent["ok"] = refusal, ok
    board.items[A_ID] = mk("Spec ready", worker=dict(W))
    with pytest.raises(SystemExit, match="could not be stopped"):
        commands.restart(A_ID[:8])
    assert board.writes == [] and ("unlock", A_ID) in agent["locked"]


def test_restart_says_so_when_there_is_no_agent_of_that_stage(board, agent):
    board.items[A_ID] = mk("Spec ready", worker=dict(W))
    assert "no run agent" in commands.restart(A_ID[:8], stage="run")[0]
    board.items[A_ID] = mk("Spec ready")
    assert "no agent" in commands.restart(A_ID[:8])[0]
    assert board.writes == []


def test_pl_restart_prints_the_lines(board, agent):
    board.items[A_ID] = mk("Spec ready", worker=dict(W))
    assert "stopped its plan agent" in run_cli(commands.cmd_restart, id=A_ID[:8], stage=None)


# ---------- hold / unhold ----------

def test_hold_adds_the_parked_tag_keeps_the_others_and_records_why(board):
    board.items[A_ID] = mk("Approved", tags=("api", "frontend"))
    lines = commands.hold(A_ID[:8], reason="waiting on legal")
    c = board.items[A_ID]
    assert c["tags"] == ["api", "frontend", "parked"]
    assert c["metadata"]["hold_reason"] == "waiting on legal" and c["metadata"]["held_by"] == "me@example.test"
    assert "held" in lines[0]
    assert agents.hold_reason(c) == "held: waiting on legal"
    assert agents.hold_reason(mk("Approved", tags=("parked",))) == "held"
    with pytest.raises(SystemExit, match="already held"):
        commands.hold(A_ID[:8])


def test_unhold_removes_only_the_parked_tag(board):
    board.items[A_ID] = mk("Approved", tags=("api", "parked"), hold_reason="x", held_at="t", held_by="me")
    lines = commands.unhold(A_ID[:8])
    c = board.items[A_ID]
    assert c["tags"] == ["api"] and c["metadata"]["hold_reason"] is None
    assert "the dispatcher takes it again" in lines[0]
    with pytest.raises(SystemExit, match="is not held"):
        commands.unhold(A_ID[:8])


def test_pl_hold_and_unhold_print_the_lines(board):
    board.items[A_ID] = mk("Approved")
    assert "held" in run_cli(commands.cmd_hold, id=A_ID[:8], reason=None)
    assert "unheld" in run_cli(commands.cmd_unhold, id=A_ID[:8])


# ---------- adopt ----------

def test_adopt_returns_the_lines_pl_adopt_prints(board, monkeypatch):
    monkeypatch.setattr(commands, "next_profile", lambda cs: "acme")
    c = mk("Spec ready")
    c["metadata"] = {}
    board.items[A_ID] = c
    lines = commands.adopt(A_ID[:8])
    assert lines == [f"adopted {A_ID[:8]}  card one  (Spec ready); the dispatcher takes it from here"]
    assert board.items[A_ID]["metadata"]["pipeline_mode"] == "auto"
    assert run_cli(commands.cmd_adopt, id=A_ID[:8], account=None) == f"{A_ID[:8]} is already a funnel card (profile acme)\n"


# ---------- the CLI ----------

@pytest.mark.parametrize("cmd, want", [("restart", "--stage"), ("hold", "--reason"), ("unhold", "id")])
def test_the_new_commands_have_help(tmp_path, cmd, want):
    prof = tmp_path / ".pl-help"
    prof.mkdir()
    (prof / "config.toml").write_text("")
    r = subprocess.run([sys.executable, "-m", "pl", cmd, "--help"], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof), "PL_NO_UPDATE_CHECK": "1"})
    assert r.returncode == 0 and want in r.stdout


def test_the_assistant_asks_before_the_new_commands():
    from pl import assistant, cli
    for cmd in ("restart", "hold", "unhold"):
        assert cmd in assistant.PL_WRITES and cmd in cli.ASSISTANT_ACTIONS
        assert f"`pl {cmd}" in (Path(assistant.__file__).parent / "assistant.md").read_text()
