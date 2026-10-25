"""pl titles check: plan cards whose title prefix and plan/slice metadata disagree."""
import argparse
import copy
import json
import subprocess
import sys

import pytest

from pl import titles, trackers
from pl import config as C


class FakeTracker:
    def __init__(self):
        self.items, self.writes, self.error = [], [], None

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        if self.error:
            raise SystemExit(self.error)
        return copy.deepcopy(self.items)

    def update(self, *a, **k):
        self.writes.append(("update", a, k))

    def move(self, *a, **k):
        self.writes.append(("move", a, k))

    def create(self, *a, **k):
        self.writes.append(("create", a, k))


def mk(n, title, col="In progress", **meta):
    return {"id": f"{n:08d}-0000-0000-0000-000000000000", "title": title, "description": "", "tags": [],
            "list_id": f"col-{col}", "updated_at": "", "metadata": dict(meta)}


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
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


def ns(cmd="check", **kw):
    return argparse.Namespace(titles_cmd=cmd, json=False, fix=False, dry_run=False, **kw)


def check(capsys, json_out=False, **kw):
    with pytest.raises(SystemExit) as e:
        titles.cmd_titles(argparse.Namespace(**{**vars(ns()), "json": json_out, **kw}))
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


def check_ok(capsys, json_out=False, **kw):
    titles.cmd_titles(argparse.Namespace(**{**vars(ns()), "json": json_out, **kw}))
    out = capsys.readouterr()
    return out.out


def test_metadata_without_title_prefix_is_listed(board, capsys):
    board.items = [mk(1, "kill switch, CLI and dispatcher", plan=3, slice=2)]
    code, out, _ = check(capsys)
    assert code == 1
    assert "00000001" in out and "kill switch, CLI and dispatcher" in out
    assert "metadata says Plan 3 slice 2, title has no prefix" in out


def test_title_prefix_without_metadata_is_listed(board, capsys):
    board.items = [mk(1, "Plan 4 slice 1: worktrees gc")]
    code, out, _ = check(capsys)
    assert code == 1 and "title says Plan 4 slice 1, metadata has none" in out


def test_title_and_metadata_that_disagree_name_both_pairs(board, capsys):
    board.items = [mk(1, "Plan 3 slice 2: x", plan=3, slice=1)]
    code, out, _ = check(capsys)
    assert code == 1 and "title says Plan 3 slice 2, metadata says Plan 3 slice 1" in out


def test_two_in_step_cards_with_the_same_pair_are_both_listed(board, capsys):
    board.items = [mk(1, "Plan 3 slice 2: a", plan=3, slice=2), mk(2, "Plan 3 slice 2: b", plan="3", slice="2")]
    code, out, _ = check(capsys)
    assert code == 1
    assert out.count("Plan 3 slice 2 is used by more than one card") == 2
    assert "00000001" in out and "00000002" in out


def test_a_clean_board_prints_one_line_and_exits_0(board, capsys):
    board.items = [mk(1, "Plan 3 slice 2: a", plan=3, slice=2), mk(2, "plan 5 Slice 1: x", plan=5, slice=1),
                   mk(3, "a plain idea"), mk(4, "Slice 2 of X")]
    out = check_ok(capsys)
    assert out.count("\n") == 1 and "no card needs fixing" in out


def test_done_cards_are_not_checked(board, capsys):
    board.items = [mk(1, "Plan 4 slice 1: x", col="Done")]
    assert "no card needs fixing" in check_ok(capsys)


def test_json_lists_each_flagged_card_with_its_fields(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2), mk(2, "Plan 4 slice 1: gc")]
    code, out, _ = check(capsys, json_out=True)
    rows = json.loads(out)
    assert code == 1 and len(rows) == 2
    keys = {"id", "title", "plan", "slice", "title_plan", "title_slice", "reason"}
    assert all(set(r) == keys for r in rows)
    by_title = {r["title"]: r for r in rows}
    assert by_title["kill switch"]["title_plan"] is None and by_title["kill switch"]["plan"] == 3
    assert by_title["Plan 4 slice 1: gc"]["plan"] is None and by_title["Plan 4 slice 1: gc"]["title_slice"] == 1


def test_json_on_a_clean_board_is_an_empty_list(board, capsys):
    assert check_ok(capsys, json_out=True) == "[]\n"


def test_check_changes_nothing(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2), mk(2, "Plan 4 slice 1: gc"),
                   mk(3, "Plan 3 slice 2: a", plan=3, slice=1), mk(4, "Plan 1 slice 1: a", plan=1, slice=1),
                   mk(5, "Plan 1 slice 1: b", plan=1, slice=1)]
    before = copy.deepcopy(board.items)
    check(capsys)
    assert board.writes == [] and board.cards() == before


def test_a_tracker_error_exits_2_with_its_message(board, capsys):
    board.error = "pl: gh timed out after 60s: gh project item-list"
    code, _, err = check(capsys)
    assert code == 2 and "pl: gh timed out after 60s: gh project item-list" in err


def test_help_shows_titles_check(tmp_path):
    prof = tmp_path / ".pl-help"
    prof.mkdir()
    (prof / "config.toml").write_text("")
    r = subprocess.run([sys.executable, "-m", "pl", "--help"], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof), "PL_NO_UPDATE_CHECK": "1"})
    assert "pl titles check [--json]" in r.stdout and "exits 1" in r.stdout


# ---------- --fix ----------

def test_fix_renames_a_card_whose_metadata_has_plan_and_slice(board, capsys):
    board.items = [mk(1, "kill switch, CLI and dispatcher", plan=3, slice=2)]
    out = check_ok(capsys, fix=True)
    new = "Plan 3 slice 2: kill switch, CLI and dispatcher"
    assert board.writes == [("update", (board.items[0]["id"],), {"title": new})]
    assert out.count("\n") == 1 and "renamed" in out and "kill switch, CLI and dispatcher" in out and new in out


def test_fix_writes_metadata_for_a_prefixed_title(board, capsys):
    board.items = [mk(1, "Plan 4 slice 1: worktrees gc")]
    out = check_ok(capsys, fix=True)
    (_, args, kw), = board.writes
    assert kw == {"metadata": {"plan": 4, "slice": 1}}
    assert all(type(v) is int for v in kw["metadata"].values())
    assert out.count("\n") == 1 and "wrote metadata Plan 4 slice 1" in out


def test_fix_skips_disagreeing_and_duplicate_cards_and_exits_1(board, capsys):
    board.items = [mk(1, "Plan 3 slice 2: x", plan=3, slice=1),
                   mk(2, "Plan 5 slice 2: a", plan=5, slice=2), mk(3, "Plan 5 slice 2: b", plan=5, slice=2)]
    code, out, _ = check(capsys, fix=True)
    assert code == 1 and board.writes == []
    assert out.count("fix by hand with pl titles set") == 3
    assert "title says Plan 3 slice 2, metadata says Plan 3 slice 1" in out
    assert "Plan 5 slice 2 is used by more than one card" in out


def test_fix_dry_run_prints_would_and_writes_nothing(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2), mk(2, "Plan 4 slice 1: gc")]
    before = copy.deepcopy(board.items)
    out = check_ok(capsys, fix=True, dry_run=True)
    lines = out.splitlines()
    assert len(lines) == 2 and all(line.startswith("would") for line in lines)
    assert board.writes == [] and board.cards() == before


def test_fix_json_adds_action_and_new_title(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2), mk(2, "Plan 4 slice 1: gc"),
                   mk(3, "Plan 6 slice 1: x", plan=6, slice=2)]
    code, out, _ = check(capsys, json_out=True, fix=True)
    rows = {r["id"][:8]: r for r in json.loads(out)}
    assert code == 1
    assert [rows[k]["action"] for k in ("00000001", "00000002", "00000003")] == ["rename", "write metadata", "skipped"]
    assert rows["00000001"]["new_title"] == "Plan 3 slice 2: kill switch"
    assert rows["00000002"]["new_title"] is None and rows["00000003"]["new_title"] is None
    assert {"id", "title", "plan", "slice", "title_plan", "title_slice", "reason"} <= set(rows["00000001"])


def test_fix_json_exits_0_when_everything_was_fixed(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2)]
    assert json.loads(check_ok(capsys, json_out=True, fix=True))[0]["action"] == "rename"


def test_fix_does_not_rename_into_a_slot_another_card_holds(board, capsys):
    board.items = [mk(1, "kill switch", plan=3, slice=2), mk(2, "Plan 3 slice 2: other", plan=3, slice=2)]
    code, out, _ = check(capsys, fix=True)
    assert code == 1 and board.writes == []
    assert "00000001" in out and "fix by hand" in out


def test_fix_does_not_write_metadata_into_a_slot_a_done_card_holds(board, capsys):
    board.items = [mk(1, "Plan 5 slice 2: old work", col="Done", plan=5, slice=2), mk(2, "Plan 5 slice 2: again")]
    code, out, _ = check(capsys, fix=True)
    assert code == 1 and board.writes == []
    assert "00000002" in out and "fix by hand" in out


def test_fix_rename_keeps_80_chars_and_whole_words(board, capsys):
    words = ("alpha " * 17).split()
    board.items = [mk(1, " ".join(words), plan=3, slice=2)]
    check_ok(capsys, fix=True)
    new = board.writes[0][2]["title"]
    assert len(new) <= 80 and new.startswith("Plan 3 slice 2: ") and new.split()[-1] == "alpha"


def test_dry_run_without_fix_is_refused(board):
    board.items = [mk(1, "kill switch", plan=3, slice=2)]
    with pytest.raises(SystemExit, match="--dry-run goes with --fix"):
        titles.cmd_titles(argparse.Namespace(**{**vars(ns()), "dry_run": True}))
    assert board.writes == []


# ---------- next and set ----------

def run_next(capsys, **kw):
    titles.cmd_titles(argparse.Namespace(**{**vars(ns("next")), **kw}))
    return capsys.readouterr().out


def test_next_is_one_more_than_the_highest_plan_including_done(board, capsys):
    board.items = [mk(1, "x", col="Done", plan=21, slice=1), mk(2, "Plan 20 slice 1: y"), mk(3, "z", plan="abc")]
    assert run_next(capsys) == "22\n"


def test_next_on_a_board_without_plan_cards_is_1(board, capsys):
    board.items = [mk(1, "a plain idea")]
    assert run_next(capsys) == "1\n"


def test_set_titles_and_tags_a_card_with_no_plan(board, capsys):
    board.items = [mk(1, "worktrees list and gc")]
    titles.set_plan("00000001", 22, 1)
    new = "Plan 22 slice 1: worktrees list and gc"
    assert board.writes == [("update", (board.items[0]["id"],), {"title": new, "metadata": {"plan": 22, "slice": 1}})]
    assert new in capsys.readouterr().out


def test_set_replaces_an_old_prefix(board):
    board.items = [mk(1, "Plan 5 slice 3: worktrees gc", plan=5, slice=3)]
    titles.set_plan("00000001", 22, 1)
    assert board.writes[0][2]["title"] == "Plan 22 slice 1: worktrees gc"


def test_set_refuses_a_slot_another_card_holds(board):
    board.items = [mk(1, "gc"), mk(2, "other", col="Done", plan=22, slice=1)]
    with pytest.raises(SystemExit, match="00000002"):
        titles.set_plan("00000001", 22, 1)
    assert board.writes == []


def test_set_refuses_plan_below_1_and_unknown_id(board):
    board.items = [mk(1, "gc")]
    for token, plan, slice_no in (("00000001", 0, 1), ("00000001", 1, 0), ("nope#999", 1, 1)):
        with pytest.raises(SystemExit):
            titles.set_plan(token, plan, slice_no)
    assert board.writes == []


def test_help_lists_the_titles_commands():
    from pl import cli, whatsnew
    for line in ("pl titles check [--json] [--fix [--dry-run]]", "pl titles next",
                 "pl titles set <id> --plan N --slice M"):
        assert line in cli.__doc__
    assert "pl titles set" in whatsnew.text(whatsnew.ENTRIES)


def test_a_tracker_error_on_next_and_fix_exits_neither_0_nor_1(board, capsys):
    board.error = "pl: gh timed out after 60s: gh project item-list"
    for kw in ({"titles_cmd": "next"}, {"fix": True}):
        with pytest.raises(SystemExit) as e:
            titles.cmd_titles(argparse.Namespace(**{**vars(ns()), **kw}))
        assert e.value.code not in (0, 1)
