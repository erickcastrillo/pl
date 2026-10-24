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


def test_ago(monkeypatch):
    import time
    from pl.util import ago
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)
    assert ago("2023-11-14T22:08:20+00:00") == "5m ago"
    assert ago("2023-11-14T19:13:20+00:00") == "3h ago"
    assert ago("2023-11-12T22:13:20+00:00") == "2d ago"
    assert ago(None) == "-"
    assert ago("") == "-"
    assert ago("invalid") == "-"

