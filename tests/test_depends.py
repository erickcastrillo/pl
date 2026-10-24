import argparse
import copy
import io
from contextlib import redirect_stdout

import pytest

from pl import agents, commands, config as C, dispatch, trackers
from pl.agents import deps_waiting, slice_order
from pl.dispatch import approved_label


def _card(cid, title="A card", col="Inbox", desc="", **meta):
    return {"id": cid, "title": title, "list_id": col, "description": desc, "metadata": {"pipeline_mode": "auto", **meta}}


@pytest.fixture(autouse=True)
def fake_col_name(monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)


def test_deps_waiting_names_every_open_dependency():
    dep1 = _card("o/r#1", col="Approved")
    dep2 = _card("o/r#2", col="PR open")
    c = _card("o/r#3", col="Approved", depends_on=["o/r#1", "o/r#2"])
    assert deps_waiting(c, [c, dep1, dep2]) == "waiting: needs r#1, r#2 merged"


def test_a_dependency_in_done_is_met():
    dep1 = _card("o/r#1", col="Done")
    c = _card("o/r#2", col="Approved", depends_on=["o/r#1"])
    assert deps_waiting(c, [c, dep1]) is None


def test_a_dropped_or_missing_dependency_is_never_met():
    dep1 = _card("o/r#1", col="Done", dropped_at="2026-10-23T12:00:00Z")
    c = _card("o/r#2", col="Approved", depends_on=["o/r#1", "o/r#99"])
    assert deps_waiting(c, [c, dep1]) == "waiting: needs r#1, r#99 merged"


def test_slice_order_reads_plan_and_slice_numbers():
    assert slice_order({"title": "Plan 3 slice 2: x"}) == (0, 3, 2)
    assert slice_order({"title": "Plan 10 slice 1:"}) > slice_order({"title": "Plan 3 slice 9:"})
    assert slice_order({"title": "Card 1"}) == (1, 0, 0)


# ---------- WP2: dispatcher wait and slice order ----------


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}}
    C.STAGES = {"run": {"account": "claude"}}
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}"}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _pass_dispatch(monkeypatch, cs, max_runs=1):
    started = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append(c))
    for name in ("mirror_to_product", "ensure_services", "sweep_untracked"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(max_runs, False, pull=False)
    return started


def test_a_dependent_approved_card_waits_and_its_dependency_starts(monkeypatch, capsys):
    r36 = _card("o/r#36", title="Plan 22 slice 1: x", col="Approved", profile="claude")
    r37 = _card("o/r#37", title="Plan 22 slice 2: y", col="Approved", profile="claude", depends_on=["o/r#36"])
    started = _pass_dispatch(monkeypatch, [r36, r37], max_runs=1)
    assert [c["id"] for c in started] == ["o/r#36"]
    assert "r#37  run waiting: needs r#36 merged" in capsys.readouterr().out


def test_the_dependent_starts_once_its_dependency_is_done(monkeypatch):
    r36 = _card("o/r#36", title="Plan 22 slice 1: x", col="Done", profile="claude")
    r37 = _card("o/r#37", title="Plan 22 slice 2: y", col="Approved", profile="claude", depends_on=["o/r#36"])
    started = _pass_dispatch(monkeypatch, [r36, r37], max_runs=1)
    assert [c["id"] for c in started] == ["o/r#37"]


def test_startable_cards_start_in_plan_then_slice_order(monkeypatch):
    monkeypatch.setattr(dispatch, "LIVE_RUN_CAP", 10)
    c_p2s1 = _card("o/r#1", title="Plan 2 slice 1: b", col="Approved", updated_at="2026-10-04", profile="claude")
    c_p1s2 = _card("o/r#2", title="Plan 1 slice 2: a2", col="Approved", updated_at="2026-10-03", profile="claude")
    c_p1s1 = _card("o/r#3", title="Plan 1 slice 1: a1", col="Approved", updated_at="2026-10-02", profile="claude")
    c_other = _card("o/r#4", title="Other", col="Approved", updated_at="2026-10-01", profile="claude")
    started = _pass_dispatch(monkeypatch, [c_p2s1, c_p1s2, c_p1s1, c_other], max_runs=10)
    assert [c["id"] for c in started] == ["o/r#3", "o/r#2", "o/r#1", "o/r#4"]


def test_pl_list_shows_the_dependency_wait():
    r36 = _card("o/r#36", col="Approved")
    r37 = _card("o/r#37", col="Approved", depends_on=["o/r#36"])
    assert approved_label(r37, "Approved", {}, [r36, r37]) == "waiting: needs r#36 merged"


# ---------- WP3: pl depends, cycle checks and pl card ----------

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


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


def run_cli(fn, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(argparse.Namespace(**kw))
    return out.getvalue()


def test_depends_records_full_ids_and_pl_card_shows_them(board):
    board.items["o/r#36"] = _card("o/r#36", title="card 36", col="Approved")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", col="Approved")
    lines = commands.depends("r#37", ["r#36"], clear=False)
    assert board.items["o/r#37"]["metadata"]["depends_on"] == ["o/r#36"]
    assert "depends on r#36" in lines[0]
    out = run_cli(commands.cmd_card, id="r#37", delete=False)
    assert "depends on r#36\n" in out


def test_depends_adds_without_duplicates(board):
    board.items["o/r#36"] = _card("o/r#36", title="card 36")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", depends_on=["o/r#36"])
    board.items["o/r#38"] = _card("o/r#38", title="card 38")
    commands.depends("r#37", ["r#36", "r#38"], clear=False)
    assert board.items["o/r#37"]["metadata"]["depends_on"] == ["o/r#36", "o/r#38"]


def test_depends_refuses_a_cycle_with_exit_2_and_names_it(board, capsys):
    board.items["o/r#36"] = _card("o/r#36", title="card 36", depends_on=["o/r#37"])
    board.items["o/r#37"] = _card("o/r#37", title="card 37")
    with pytest.raises(SystemExit) as exc:
        commands.depends("r#37", ["r#36"], clear=False)
    assert exc.value.code == 2
    assert "pl depends: refused: that makes a cycle: r#37 -> r#36 -> r#37" in capsys.readouterr().err
    assert board.writes == []

    with pytest.raises(SystemExit) as exc2:
        commands.depends("r#37", ["r#37"], clear=False)
    assert exc2.value.code == 2
    assert "pl depends: refused: that makes a cycle: r#37 -> r#37" in capsys.readouterr().err
    assert board.writes == []


def test_depends_clear_removes_them(board):
    board.items["o/r#37"] = _card("o/r#37", title="card 37", depends_on=["o/r#36"])
    lines = commands.depends("r#37", [], clear=True)
    assert board.items["o/r#37"]["metadata"]["depends_on"] == []
    assert "no dependencies; it starts like any Approved card" in lines[0]


def test_depends_without_on_or_clear_is_an_error(board):
    board.items["o/r#37"] = _card("o/r#37", title="card 37")
    with pytest.raises(SystemExit, match="pl depends: give --on <id>... or --clear"):
        commands.depends("r#37", [], clear=False)


# ---------- WP2: pl depends --from-plans ----------

def run_cli_exit(fn, **kw):
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw))
    return exc.value.code, out.getvalue()


def test_from_plans_dry_run_lists_and_changes_nothing(board):
    board.items["o/r#36"] = _card("o/r#36", title="card 36")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", desc="# PIPELINE: PLAN\nDepends on: r#36\n")
    out = run_cli(commands.cmd_depends, id=None, from_plans=True, dry_run=True, on=[], clear=False)
    assert "r#37: would depend on r#36\n" in out
    assert "would update 1 card\n" in out
    assert board.writes == []


def test_from_plans_records_full_ids_and_card_shows_them(board):
    board.items["o/r#36"] = _card("o/r#36", title="card 36")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", desc="# PIPELINE: PLAN\nDepends on: r#36\n")
    out = run_cli(commands.cmd_depends, id=None, from_plans=True, dry_run=False, on=[], clear=False)
    assert "r#37: depends on r#36\n" in out
    assert "updated 1 card\n" in out
    assert board.items["o/r#37"]["metadata"]["depends_on"] == ["o/r#36"]
    card_out = run_cli(commands.cmd_card, id="r#37", delete=False)
    assert "depends on r#36\n" in card_out


def test_from_plans_keeps_existing_dependencies(board):
    board.items["o/r#5"] = _card("o/r#5", title="card 5")
    board.items["o/r#36"] = _card("o/r#36", title="card 36")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", desc="# PIPELINE: PLAN\nDepends on: r#36\n", depends_on=["o/r#5"])
    out = run_cli(commands.cmd_depends, id=None, from_plans=True, dry_run=False, on=[], clear=False)
    assert board.items["o/r#37"]["metadata"]["depends_on"] == ["o/r#5"]
    assert "updated 0 cards\n" in out
    assert board.writes == []


def test_from_plans_skips_no_plan_no_line_and_none(board):
    board.items["o/r#1"] = _card("o/r#1", title="no plan", desc="")
    board.items["o/r#2"] = _card("o/r#2", title="plan no depends", desc="# PIPELINE: PLAN\n## Minimum change\n")
    board.items["o/r#3"] = _card("o/r#3", title="none plain", desc="# PIPELINE: PLAN\nDepends on: none\n")
    board.items["o/r#4"] = _card("o/r#4", title="none bold", desc="# PIPELINE: PLAN\n**Depends on:** none\n")
    board.items["o/r#5"] = _card("o/r#5", title="none list", desc="# PIPELINE: PLAN\n- Depends on: none\n")
    out = run_cli(commands.cmd_depends, id=None, from_plans=True, dry_run=False, on=[], clear=False)
    assert "updated 0 cards\n" in out
    assert board.writes == []


def test_from_plans_skips_unknown_ids_and_cycles_and_exits_1(board):
    board.items["o/r#36"] = _card("o/r#36", title="card 36")
    board.items["o/r#37"] = _card("o/r#37", title="card 37", desc="# PIPELINE: PLAN\nDepends on: r#36\n")
    board.items["o/r#38"] = _card("o/r#38", title="card 38", desc="# PIPELINE: PLAN\nDepends on: r#99\n")
    board.items["o/r#40"] = _card("o/r#40", title="card 40", desc="# PIPELINE: PLAN\nDepends on: r#41\n")
    board.items["o/r#41"] = _card("o/r#41", title="card 41", desc="# PIPELINE: PLAN\nDepends on: r#40\n")
    code, out = run_cli_exit(commands.cmd_depends, id=None, from_plans=True, dry_run=False, on=[], clear=False)
    assert code == 1
    assert board.items["o/r#37"]["metadata"]["depends_on"] == ["o/r#36"]
    assert "r#37: depends on r#36\n" in out
    assert "r#38: skipped: r#99 is not a card on the board\n" in out
    assert "r#41: skipped: that makes a cycle: r#41 -> r#40 -> r#41\n" in out
    assert "updated 2 cards\n" in out


@pytest.mark.parametrize("kw", [
    {"id": "r#37", "on": [], "clear": False},
    {"id": None, "on": ["r#36"], "clear": False},
    {"id": None, "on": [], "clear": True},
])
def test_from_plans_refuses_an_id_on_or_clear(board, kw):
    with pytest.raises(SystemExit, match=r"pl depends: --from-plans takes no card id, --on or --clear"):
        commands.cmd_depends(argparse.Namespace(from_plans=True, dry_run=False, **kw))


def test_depends_without_id_or_from_plans_refuses(board):
    with pytest.raises(SystemExit, match=r"pl depends: give a card id, or --from-plans"):
        commands.cmd_depends(argparse.Namespace(id=None, from_plans=False, dry_run=False, on=[], clear=False))



