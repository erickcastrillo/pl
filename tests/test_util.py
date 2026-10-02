"""Small shared helpers."""
from pl.util import short_id


def test_short_id_keeps_the_repo_and_number_of_a_github_id():
    assert short_id("your-org/your-repo#43") == "your-repo#43"
    assert short_id("your-org/your-repo#430") != short_id("your-org/your-repo#43")
    assert short_id("your-repo#7") == "your-repo#7"


def test_short_id_cuts_a_board_id_to_8_characters():
    assert short_id("4a000001aaaa") == "4a000001"
    assert short_id("abc") == "abc"
    assert short_id(None) == "None"
