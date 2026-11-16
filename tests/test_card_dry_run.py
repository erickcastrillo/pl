"""--dry-run: a card command prints what it would do, never a line saying it was done, and writes nothing."""
import argparse
import copy
from contextlib import redirect_stdout
import io

import pytest

from pl import audit, commands, events, move_agent, notifications, outbound, pipeline, recall, trackers
from pl import config as C

CID = "aaaa1111-0000-0000-0000-000000000001"
PR = "https://github.com/o/r/pull/1"
WORKER = {"stage": "run", "session_id": "s1", "pane": "%5", "window": "@7", "profile": "acme", "attempts": 2,
          "started_at": "2026-09-30T10:00:00+00:00"}


class FakeTracker:
    def __init__(self, card):
        self.items = {card["id"]: card}
        self.writes = []

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def url(self, item_id):
        return f"https://example.test/{item_id}"

    def _write(self, name, *a, **kw):
        self.writes.append(name)

    def update(self, item_id, **fields):
        self._write("update")

    def create(self, *a, **kw):
        self._write("create")

    def delete(self, *a, **kw):
        self._write("delete")

    def move(self, ref, column):
        self._write("move")
        return ref

    def adopt(self, *a, **kw):
        self._write("adopt")


def mk(col, tags=(), **meta):
    return {"id": CID, "title": "a card", "description": "# PIPELINE: INPUT\nidea\n", "tags": list(tags),
            "list_id": f"col-{col}", "updated_at": "", "metadata": {"profile": "acme", **meta}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".pl-t").mkdir()
    (tmp_path / ".pl-t" / "config.toml").write_text("")
    C.load("t")
    C.USER_EMAIL = "me@example.test"
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(audit, "append", lambda *a, **k: None)
    monkeypatch.setattr(recall, "note", lambda *a, **k: None)
    monkeypatch.setattr(notifications, "send", lambda *a, **k: None)
    monkeypatch.setattr(commands.pods, "learn", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "is_config", lambda: False)
    monkeypatch.setattr(outbound, "has_reply", lambda c: False)
    yield
    trackers.set_dry_run(False)
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def board_of(monkeypatch):
    def install(card, dry=True):
        fake = FakeTracker(card)
        monkeypatch.setattr(trackers, "_build", lambda kind: fake)
        trackers.reset()
        trackers.set_dry_run(dry)
        return fake
    return install


def run(fn, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        got = fn(*args)
    return out.getvalue().splitlines(), got


def ns(**kw):
    return argparse.Namespace(**kw)


def test_move_dry_run_prints_would_not_moved(board_of):
    fake = board_of(mk("PR open"))
    lines, _ = run(commands.cmd_move, ns(id=CID, column="In progress", pr=None))
    assert any(l.startswith("would ") for l in lines)
    assert not any(l.startswith("moved ") for l in lines)
    assert fake.writes == []


def test_move_dry_run_with_pr_records_nothing(board_of):
    fake = board_of(mk("PR open"))
    lines, _ = run(commands.cmd_move, ns(id=CID, column="In progress", pr=PR))
    assert not any(l.startswith(("recorded ", "moved ")) for l in lines)
    assert fake.writes == [] and "pr_urls" not in fake.items[CID]["metadata"]


def test_move_without_dry_run_still_moves(board_of):
    board_of(mk("PR open"), dry=False)
    lines, _ = run(commands.cmd_move, ns(id=CID, column="In progress", pr=None))
    assert f"moved {CID} to In progress" in lines


def test_move_to_done_dry_run_writes_no_done_by(board_of):
    fake = board_of(mk("PR open"))
    lines, _ = run(commands.cmd_move, ns(id=CID, column="Done", pr=None))
    assert not any(l.startswith("moved ") for l in lines)
    assert "done_by" not in fake.items[CID]["metadata"] and fake.writes == []


def _dead(monkeypatch):
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("dead", ""))


CASES = {
    "drop": ("dropped", lambda: mk("Plan for review"), lambda a: commands.cmd_drop(ns(id=CID, reason=None))),
    "undrop": ("undropped", lambda: mk("Done", dropped_at="2026-01-01T00:00:00+00:00", dropped_from="Approved"),
               lambda a: commands.cmd_undrop(ns(id=CID))),
    "hold": ("held", lambda: mk("Approved"), lambda a: commands.cmd_hold(ns(id=CID, reason=None))),
    "unhold": ("unheld", lambda: mk("Approved", tags=["parked"]), lambda a: commands.cmd_unhold(ns(id=CID))),
    "approve": ("approved", lambda: mk("Plan for review"),
                lambda a: commands.cmd_approve(ns(id=CID, force=False, code=None, confirm=False))),
    "approve-spec": ("spec approved", lambda: mk("Spec ready"),
                     lambda a: commands.cmd_approve(ns(id=CID, force=False, code=None, confirm=False))),
    "reject": ("rejected", lambda: mk("Plan for review"), lambda a: commands.cmd_reject(ns(id=CID, notes="no"))),
    "adopt": ("adopted", lambda: {**mk("Approved"), "metadata": {}}, lambda a: commands.cmd_adopt(ns(id=CID, account="acme"))),
    "done": ("done ", lambda: mk("PR open"),
             lambda a: commands.cmd_done(ns(id=CID, note=None, drop_slices=None))),
    "retry": ("reset the", lambda: mk("Approved", worker=WORKER), lambda a: commands.cmd_retry(ns(id=CID, stage=None))),
    "restart": ("the dispatcher starts the", lambda: mk("Approved", worker=WORKER),
                lambda a: commands.cmd_restart(ns(id=CID, stage=None))),
    "card-delete": ("deleted", lambda: mk("Approved"),
                    lambda a: commands.cmd_card(ns(id=CID, delete=True, fresh=False, json=False))),
    "section-from": ("wrote", lambda: mk("Inbox"), lambda a: commands.cmd_section(
        ns(id=CID, name="SPEC", from_=str(a), force=False, fresh=False, json=False))),
}


@pytest.mark.parametrize("name", CASES)
def test_card_commands_dry_run_print_no_done_form(name, board_of, monkeypatch, tmp_path):
    prefix, make, call = CASES[name]
    _dead(monkeypatch)
    monkeypatch.setattr(commands.pods, "draft_path", lambda cid, name: tmp_path / "none")
    monkeypatch.setattr(commands, "edited_plan", lambda c, p: None)
    monkeypatch.setattr(commands, "spec_gated", lambda col: col == "Spec ready")
    src = tmp_path / "text.md"
    src.write_text("new text")
    card = make()
    fake = board_of(card)
    before = copy.deepcopy(fake.items[CID])
    lines, _ = run(call, src)
    assert not any(l.lstrip().startswith(prefix) for l in lines), lines
    assert fake.writes == []
    assert fake.items[CID] == before


def test_drop_dry_run_only_says_it_would_stop_a_live_agent(board_of, monkeypatch):
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("alive", ""))
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(move_agent, "_run", lambda argv, **k: pytest.fail("a dry run touched the pane"))
    board_of(mk("Approved", worker=WORKER))
    lines, _ = run(commands.cmd_drop, ns(id=CID, reason=None))
    assert any(l.startswith("would stop") for l in lines)
    assert not any("stopped its" in l or l.startswith("dropped") for l in lines)
