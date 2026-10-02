"""Card lookup: a full id, a unique prefix, or for a GitHub card "repo#N" or "#N". An ambiguous short form is refused."""
import shlex

import pytest

from pl import board
from pl.util import cmd_id, short_id

CS = [{"id": "your-org/your-repo#43"}, {"id": "your-org/your-repo#4"}, {"id": "your-org/other-repo#7"},
      {"id": "4a000001aaaa"}, {"id": "4a000002bbbb"}]


@pytest.fixture
def board_of(monkeypatch):
    def use(cs):
        monkeypatch.setattr(board, "cards", lambda: cs)
        monkeypatch.setattr(board, "card", lambda cid: {"id": cid, "read": True})
    return use


def test_a_github_card_by_repo_and_number(board_of):
    board_of(CS)
    assert board.find_card("your-repo#43")["id"] == "your-org/your-repo#43"
    assert board.find_card("your-repo#4")["id"] == "your-org/your-repo#4"   # never #43
    assert board.find_card("Your-Repo#43")["id"] == "your-org/your-repo#43"   # GitHub names ignore case


def test_a_github_card_by_number_alone(board_of):
    board_of(CS)
    assert board.find_card("#7")["id"] == "your-org/other-repo#7"
    assert board.find_card("#4")["id"] == "your-org/your-repo#4"


def test_the_full_github_id_still_works(board_of):
    board_of(CS)
    assert board.find_card("your-org/your-repo#4")["id"] == "your-org/your-repo#4"   # exact, though #43 shares the prefix


def test_an_ambiguous_short_form_is_refused_and_lists_the_matches(board_of):
    board_of(CS + [{"id": "their-org/your-repo#43"}, {"id": "their-org/third#7"}])
    with pytest.raises(SystemExit) as e:
        board.find_card("your-repo#43")
    assert "2 cards match" in str(e.value) and "your-org/your-repo#43" in str(e.value) and "their-org/your-repo#43" in str(e.value)
    with pytest.raises(SystemExit) as e:
        board.find_card("#7")
    assert "your-org/other-repo#7" in str(e.value) and "their-org/third#7" in str(e.value)


def test_no_match_is_refused(board_of):
    board_of(CS)
    for token in ("nope#1", "#99", "#x", "zzzz"):
        with pytest.raises(SystemExit) as e:
            board.find_card(token)
        assert "0 cards match" in str(e.value)


def test_board_id_prefixes_work_as_before(board_of):
    board_of(CS)
    assert board.find_card("4a000001")["id"] == "4a000001aaaa"
    assert board.find_card("4a000002bb")["id"] == "4a000002bbbb"
    with pytest.raises(SystemExit):
        board.find_card("4a00000")   # two cards share it
    long = "f" * 32
    assert board.find_card(long) == {"id": long, "read": True}   # a full board id reads the card directly


def test_cmd_id_is_the_short_id_safe_to_paste_into_a_shell():
    assert cmd_id("your-org/your-repo#43") == "'your-repo#43'"   # unquoted, '#' starts a shell comment
    assert cmd_id("4a000001aaaa") == "4a000001"
    for cid in ("your-org/your-repo#43", "4a000001aaaa"):
        assert shlex.split(f"pl card {cmd_id(cid)}") == ["pl", "card", short_id(cid)]


def test_every_hint_resolves_to_its_card(board_of):
    board_of(CS)
    for c in CS:
        token = shlex.split(f"pl review {cmd_id(c['id'])}")[2]
        assert board.find_card(token)["id"] == c["id"]


def test_pl_review_takes_a_github_short_id_as_a_card(board_of, monkeypatch):
    import types
    from pl import commands
    board_of(CS)
    got = []
    monkeypatch.setattr(commands, "find_card", board.find_card)
    monkeypatch.setattr(commands, "pull_plan", lambda c: got.append(c["id"]) or "plan.md")
    monkeypatch.setattr(commands, "open_in_nvim", lambda p, here: None)
    for token in ("your-repo#43", "#7", "your-org/your-repo#4"):
        commands.cmd_review(types.SimpleNamespace(what=token, here=True))
    assert got == ["your-org/your-repo#43", "your-org/other-repo#7", "your-org/your-repo#4"]
