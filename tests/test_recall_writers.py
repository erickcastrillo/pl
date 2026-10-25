"""Run summaries and approval reasons land in agent memory."""
import argparse
import copy
import sqlite3

import pytest

from pl import accounts, commands, dispatch, recall, trackers
from pl import config as C



def _has_fts5():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


pytestmark = pytest.mark.skipif(not _has_fts5(), reason="this Python's SQLite has no FTS5")

WORKER = {"stage": "run", "session_id": "sess-1", "profile": "acme", "harness": "claude", "attempts": 1}
BODY = ("# PIPELINE: SPEC\n### Outcome\nRuns leave a summary.\n\n# PIPELINE: PLAN\n## Summary\nSlice 4 of memory.\n"
        "## Minimum change\nsmall\n")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.USER_EMAIL = "me@example.test"
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(col, **meta):
    return {"id": "o/r#7", "title": "Card 7", "list_id": col, "updated_at": "2026-10-01", "description": BODY,
            "tags": [], "metadata": {"pipeline_mode": "auto", **meta}}


def _pass(monkeypatch, cs):
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, max_prep=2, pull=False)


def _finish(monkeypatch):
    _pass(monkeypatch, [_card("In progress", worker=WORKER)])
    _pass(monkeypatch, [_card("PR open", worker=WORKER, pr_urls=["https://github.com/o/r/pull/9"])])


def test_a_stage_that_finishes_leaves_one_run_entry_with_every_field(monkeypatch):
    _finish(monkeypatch)
    (e,) = recall.search("summary")
    assert (e["kind"], e["source"], e["card"], e["pipeline"], e["pod"]) == ("run", "run:sess-1", "o/r#7", "t", "t")
    assert e["author"] == "run agent (claude/acme)"
    assert e["created_at"]
    assert "Slice 4 of memory." in e["text"] and "https://github.com/o/r/pull/9" in e["text"]


def test_a_second_pass_does_not_write_it_again(monkeypatch):
    _finish(monkeypatch)
    recall.record_run(_card("PR open", worker=WORKER), "run", WORKER)
    _pass(monkeypatch, [_card("PR open", worker=WORKER)])
    assert len(recall.search("summary")) == 1


def test_a_design_stage_is_recorded_when_its_section_appears(monkeypatch):
    c = _card("Spec ready", worker={**WORKER, "stage": "design"})
    c["description"] += "\n# PIPELINE: DESIGN\nscreens\n"
    _pass(monkeypatch, [c])
    assert recall.search("summary")[0]["author"] == "design agent (claude/acme)"


def test_a_store_failure_never_stops_the_pass(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("pl: this Python's SQLite has no FTS5")
    monkeypatch.setattr(recall, "add", boom)
    _finish(monkeypatch)
    assert "memory" in capsys.readouterr().err


class FakeTracker:
    def __init__(self, c):
        self.c = c

    def columns(self):
        return {t: t for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(self.c)]

    def card(self, item_id):
        return copy.deepcopy(self.c)

    def update(self, item_id, *, verify=True, **fields):
        self.c["metadata"] = {**self.c["metadata"], **fields.pop("metadata", {})}
        self.c.update(fields)
        return copy.deepcopy(self.c)


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker(_card("Plan for review"))
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "edited_plan", lambda c, path: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    return fake


def test_a_plan_rejected_with_notes_saves_the_reason_linked_to_the_card(board):
    commands.cmd_reject(argparse.Namespace(id="o/r#7", notes="split into 3 PRs"))
    (e,) = recall.search("split")
    assert (e["kind"], e["card"], e["source"]) == ("rejection", "o/r#7", "rejection:o/r#7#1")


def test_a_spec_rejection_is_saved_too(board, monkeypatch):
    board.c["list_id"] = "Inbox"
    monkeypatch.setattr(commands, "spec_gated", lambda col: True)
    commands.cmd_reject(argparse.Namespace(id="o/r#7", notes="too vague"))
    (e,) = recall.search("vague")
    assert (e["kind"], e["card"], e["source"]) == ("rejection", "o/r#7", "rejection:o/r#7#1")


def test_approve_with_a_reason_saves_it(board):
    commands.cmd_approve(argparse.Namespace(id="o/r#7", force=False, reason="small and safe"))
    (e,) = recall.search("safe")
    assert (e["kind"], e["card"]) == ("approval", "o/r#7")


def test_approve_without_a_reason_saves_approved(board):
    commands.cmd_approve(argparse.Namespace(id="o/r#7", force=False, reason=None))
    assert recall.search("approved")[0]["kind"] == "approval"


def test_a_memory_failure_never_undoes_the_approval(board, monkeypatch, capsys):
    monkeypatch.setattr(recall, "add", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no store")))
    commands.cmd_approve(argparse.Namespace(id="o/r#7", force=False, reason=None))
    assert board.c["list_id"] == "Approved"
    assert "memory" in capsys.readouterr().err
