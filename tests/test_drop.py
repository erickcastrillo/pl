"""pl drop / pl undrop: a card no longer needed goes to Done marked dropped, its live agent stopped; undrop puts it back."""
import argparse
import copy
import io
import json
from contextlib import redirect_stdout

import pytest

from pl import commands, move_agent, trackers
from pl import config as C

A_ID = "aaaa1111-0000-0000-0000-000000000001"
B_ID = "aaaa2222-0000-0000-0000-000000000002"
PANE_W = {"stage": "run", "session_id": "s1", "pane": "%5", "window": "@7", "profile": "acme",
          "started_at": "2026-09-30T10:00:00+00:00"}


class FakeTracker:
    def __init__(self, items):
        self.items = {c["id"]: c for c in items}
        self.writes = []

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        self.writes.append((item_id, copy.deepcopy(fields)))
        c = self.items[item_id]
        if "column" in fields:
            fields["list_id"] = self.columns()[fields.pop("column")]
        if "metadata" in fields:
            c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
        c.update(fields)
        return copy.deepcopy(c)

    def url(self, item_id):
        return f"https://example.test/{item_id}"


def mk(cid, col, **meta):
    return {"id": cid, "title": f"card {cid[:8]}", "description": "", "tags": [], "list_id": f"col-{col}",
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
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker([])
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


@pytest.fixture
def agent(monkeypatch):
    """A live agent on the card: what stop() was asked to stop, and what it answers."""
    got = {"stopped": [], "ok": True, "refusal": None}
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: got["refusal"])

    def stop(pane, sid):
        got["stopped"].append((pane, sid))
        return got["ok"]
    monkeypatch.setattr(move_agent, "stop", stop)
    return got


def kinds():
    p = C.STATE_DIR / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def run_cli(fn, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(argparse.Namespace(**kw))
    return out.getvalue()


# ---------- done ----------

def test_done_moves_a_pipeline_card_to_done_and_pl_done_prints_the_same_lines(board):
    board.items[A_ID] = mk(A_ID, "PR open", worker={"stage": "run"})
    lines = commands.done(A_ID[:8])
    m = board.items[A_ID]["metadata"]
    assert board.items[A_ID]["list_id"] == "col-Done" and m["done_by"] == "me@example.test" and m["done_at"]
    assert m["worker"] is None and "dropped_at" not in m
    assert lines == [f"done {A_ID[:8]}  card {A_ID[:8]}  (pipeline board, was in PR open)"]
    board.items[B_ID] = mk(B_ID, "Manual")
    assert run_cli(commands.cmd_done, id=B_ID, note=None) == f"done {B_ID[:8]}  card {B_ID[:8]}  (pipeline board, was in Manual)\n"


def test_done_refuses_two_matches(board):
    board.items[A_ID] = mk(A_ID, "Inbox")
    board.items[B_ID] = mk(B_ID, "Inbox")
    with pytest.raises(SystemExit, match="2 cards match"):
        commands.done("aaaa")
    assert board.writes == []


# ---------- drop ----------

def test_drop_moves_to_done_and_records_why_and_from_where(board):
    board.items[A_ID] = mk(A_ID, "Spec ready")
    lines = commands.drop(A_ID[:8], reason="customer cancelled")
    c = board.items[A_ID]
    m = c["metadata"]
    assert c["list_id"] == "col-Done"
    assert m["dropped_by"] == "me@example.test" and m["dropped_at"]
    assert m["drop_reason"] == "customer cancelled" and m["dropped_from"] == "Spec ready"
    assert m["worker"] is None
    assert "done_at" not in m                                  # dropped is not finished
    assert any("dropped" in line and "Spec ready" in line for line in lines)
    ev = [e for e in kinds() if e["kind"] == "dropped"]
    assert ev and ev[0]["card"] == A_ID and ev[0]["from"] == "Spec ready"
    assert "customer" not in json.dumps(ev)                   # the reason is card text: not in the event log


def test_drop_refuses_two_matches(board):
    board.items[A_ID] = mk(A_ID, "Inbox")
    board.items[B_ID] = mk(B_ID, "Inbox")
    with pytest.raises(SystemExit, match="2 cards match"):
        commands.drop("aaaa")
    assert board.writes == []


@pytest.mark.parametrize("meta", [{}, {"dropped_at": "2026-09-30T10:00:00+00:00", "dropped_from": "Inbox"}])
def test_drop_refuses_a_card_already_in_done(board, meta):
    board.items[A_ID] = mk(A_ID, "Done", **meta)
    with pytest.raises(SystemExit, match="already"):
        commands.drop(A_ID)
    assert board.writes == []


def test_drop_moves_the_linked_product_card_to_done(board, monkeypatch):
    intake = FakeTracker([{"id": "p1", "title": "p", "list_id": "pcol-Backlog", "metadata": {}}])
    monkeypatch.setattr(trackers, "get", lambda kind: intake if kind == "intake" else board)
    monkeypatch.setattr(commands, "intake_configured", lambda: True)
    monkeypatch.setattr(commands, "product_lists", lambda: {"Backlog": "pcol-Backlog", "Done": "pcol-Done"})
    board.items[A_ID] = mk(A_ID, "Approved", product_card="p1")
    lines = commands.drop(A_ID)
    assert intake.items["p1"]["list_id"] == "pcol-Done"
    assert any("Product card" in line for line in lines)


def test_drop_stops_a_live_agent_and_leaves_its_window_for_the_dispatcher_to_close(board, agent):
    board.items[A_ID] = mk(A_ID, "In progress", worker=dict(PANE_W))
    lines = commands.drop(A_ID)
    assert agent["stopped"] == [("%5", "s1")]
    m = board.items[A_ID]["metadata"]
    assert board.items[A_ID]["list_id"] == "col-Done" and m["worker"] is None
    assert m["finished_workers"][-1]["window"] == "@7"       # the dispatcher closes finished windows
    assert any("stopped" in line for line in lines)
    assert not move_agent.locked(A_ID)                         # the move lock is released


@pytest.mark.parametrize("how", ["stop fails", "pane refused"])
def test_drop_refuses_when_the_live_agent_cannot_be_stopped(board, agent, how):
    if how == "stop fails":
        agent["ok"] = False
    else:
        agent["refusal"] = "its pane %5 is not in window @7"
    board.items[A_ID] = mk(A_ID, "In progress", worker=dict(PANE_W))
    with pytest.raises(SystemExit, match="not dropped"):
        commands.drop(A_ID)
    assert board.items[A_ID]["list_id"] == "col-In progress" and board.writes == []
    assert agent["stopped"] == ([] if how == "pane refused" else [("%5", "s1")])
    assert not move_agent.locked(A_ID)


def test_drop_refuses_while_an_agent_move_holds_the_card(board, agent):
    board.items[A_ID] = mk(A_ID, "In progress", worker=dict(PANE_W))
    assert move_agent.lock(A_ID)
    try:
        with pytest.raises(SystemExit, match="move"):
            commands.drop(A_ID)
    finally:
        move_agent.unlock(A_ID)
    assert agent["stopped"] == [] and board.writes == []


def test_drop_of_a_dead_agent_sends_no_keys(board, monkeypatch, agent):
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("dead", w.get("session_id")))
    board.items[A_ID] = mk(A_ID, "In progress", worker=dict(PANE_W))
    commands.drop(A_ID)
    assert agent["stopped"] == []
    assert board.items[A_ID]["list_id"] == "col-Done"


def test_cmd_drop_prints_the_lines(board):
    board.items[A_ID] = mk(A_ID, "Inbox")
    out = run_cli(commands.cmd_drop, id=A_ID, reason=None)
    assert "dropped" in out
    assert board.items[A_ID]["metadata"]["drop_reason"] is None


# ---------- undrop ----------

def dropped(col_from="Plan for review"):
    return mk(A_ID, "Done", dropped_at="2026-09-30T10:00:00+00:00", dropped_by="me@example.test",
              drop_reason="no longer needed", **({"dropped_from": col_from} if col_from else {}))


def test_undrop_puts_the_card_back_and_clears_the_drop(board):
    board.items[A_ID] = dropped()
    lines = commands.undrop(A_ID[:8])                          # a Done card is found by prefix
    c = board.items[A_ID]
    assert c["list_id"] == "col-Plan for review"
    assert not any(c["metadata"].get(k) for k in ("dropped_at", "dropped_by", "drop_reason", "dropped_from"))
    assert any("Plan for review" in line for line in lines)
    assert "undropped" in [e["kind"] for e in kinds()]


@pytest.mark.parametrize("col_from", [None, "Nowhere", "Done"])
def test_undrop_falls_back_to_inbox(board, col_from):
    board.items[A_ID] = dropped(col_from)
    commands.undrop(A_ID)
    assert board.items[A_ID]["list_id"] == "col-Inbox"


def test_undrop_refuses_a_card_that_was_not_dropped(board):
    board.items[A_ID] = mk(A_ID, "Done", done_at="2026-09-30T10:00:00+00:00")
    with pytest.raises(SystemExit, match="not dropped"):
        commands.undrop(A_ID)
    assert board.writes == []


def test_undrop_refuses_a_dropped_card_someone_moved_out_of_done(board):
    board.items[A_ID] = {**dropped(), "list_id": "col-Inbox"}
    with pytest.raises(SystemExit, match="not in Done"):
        commands.undrop(A_ID)
    assert board.writes == []


def test_cmd_undrop_prints_the_lines(board):
    board.items[A_ID] = dropped()
    assert "Plan for review" in run_cli(commands.cmd_undrop, id=A_ID)
