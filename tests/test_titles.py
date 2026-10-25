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


def check(capsys, json_out=False):
    with pytest.raises(SystemExit) as e:
        titles.cmd_titles(argparse.Namespace(titles_cmd="check", json=json_out))
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


def check_ok(capsys, json_out=False):
    titles.cmd_titles(argparse.Namespace(titles_cmd="check", json=json_out))
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
