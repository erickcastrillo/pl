"""pl plan check: slices of one plan that change the same file with no order between them."""
import argparse
import copy
import json

import pytest

from pl import plancheck, trackers
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


def plan_text(*wheres, later=""):
    wps = "\n".join(f"### WP{i}: x\n- What: y.\n- Where: {w}\n- Change: z." for i, w in enumerate(wheres, 1))
    later = f"\n## Later slices\n{later}\n\n## Decisions\n- none" if later else ""
    return f"## Summary\ns\n\n## Work packages\n\n{wps}\n{later}"


def slice_card(n, slice_no, *wheres, later="", col="Plan for review", **meta):
    c = mk(n, f"Plan 58 slice {slice_no}: x", col=col, plan=58, slice=slice_no, **meta)
    c["description"] = f"# PIPELINE: PLAN\n{plan_text(*wheres, later=later)}\n" if wheres else ""
    return c


def ns(n=58, json=False):
    return argparse.Namespace(plan_cmd="check", n=n, json=json)


def run(board, capsys, **kw):
    try:
        plancheck.cmd_plan(ns(**kw))
        code = 0
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_two_slices_sharing_a_file_without_a_link_are_listed_and_exit_1(board, capsys):
    board.items = [slice_card(1, 1, "`src/pl/tui/loops.py`, `tests/a.py`"), slice_card(2, 2, "src/pl/tui/loops.py:draw")]
    code, out = run(board, capsys)
    assert code == 1
    assert "00000001" in out and "00000002" in out and "shared: src/pl/tui/loops.py" in out


@pytest.mark.parametrize("owner, other", [(1, 2), (2, 1)])
def test_a_depends_link_in_either_direction_clears_the_pair(board, capsys, owner, other):
    cards = {1: slice_card(1, 1, "src/pl/a.py"), 2: slice_card(2, 2, "src/pl/a.py")}
    cards[owner]["metadata"]["depends_on"] = [cards[other]["id"]]
    board.items = list(cards.values())
    code, out = run(board, capsys)
    assert code == 0 and "no unordered overlaps" in out


def test_no_overlap_prints_one_line_and_exits_0(board, capsys):
    board.items = [slice_card(1, 1, "src/pl/a.py"), slice_card(2, 2, "src/pl/b.py")]
    code, out = run(board, capsys)
    assert code == 0
    assert out == "plan 58: no unordered overlaps among 2 cards checked\n"


def test_whatsnew_and_changelog_do_not_count(board, capsys):
    where = "src/pl/whatsnew.py, CHANGELOG.md, docs/CHANGELOG.md"
    board.items = [slice_card(1, 1, where), slice_card(2, 2, where)]
    assert run(board, capsys)[0] == 0


def test_a_later_slices_files_list_counts_for_the_card_it_names(board, capsys):
    two = slice_card(2, 2)
    later = f"- Slice 2 (Plan 58, {two['id']}): after 1, more; files: src/pl/a.py, tests/t.py"
    board.items = [slice_card(1, 1, "src/pl/a.py", later=later), two]
    code, out = run(board, capsys)
    assert code == 1 and "shared: src/pl/a.py" in out


def test_json_lists_pairs_and_keeps_the_exit_status(board, capsys):
    a, b = slice_card(1, 1, "src/pl/a.py, src/pl/b.py"), slice_card(2, 2, "src/pl/b.py, src/pl/a.py")
    board.items = [a, b]
    code, out = run(board, capsys, json=True)
    assert code == 1
    assert json.loads(out) == [{"a": a["id"], "b": b["id"], "files": ["src/pl/a.py", "src/pl/b.py"]}]


def test_json_is_an_empty_list_when_clean(board, capsys):
    board.items = [slice_card(1, 1, "src/pl/a.py"), slice_card(2, 2, "src/pl/b.py")]
    code, out = run(board, capsys, json=True)
    assert code == 0 and json.loads(out) == []


def test_a_plan_with_no_cards_is_an_error_naming_the_number(board, capsys):
    board.items = [slice_card(1, 1, "src/pl/a.py", col="Done"), mk(3, "other", plan=7, slice=1)]
    for n in (58, 99):
        code, _ = run(board, capsys, n=n)
        assert code == f"pl plan check: no open cards of plan {n} on the board"


def test_pl_plan_check_is_a_cli_command():
    from pl import cli
    a = cli.build_parser().parse_args(["plan", "check", "58", "--json"])
    assert (a.cmd, a.plan_cmd, a.n, a.json) == ("plan", "check", 58, True)
