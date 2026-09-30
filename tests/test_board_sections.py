import pytest

from pl import board

WITH_MARKERS = """Pulled from somewhere.
Preamble line two.

# PIPELINE: INPUT
the idea

# PIPELINE: SPEC
## Goals
- one

# PIPELINE: PLAN
## WP1
do it
"""


def test_round_trip_with_preamble_and_sections():
    parts = board.sections(WITH_MARKERS)
    assert list(parts) == ["", "INPUT", "SPEC", "PLAN"]
    assert parts["INPUT"] == "the idea"
    assert board.render(parts) == WITH_MARKERS


def test_round_trip_without_markers():
    desc = "just a plain description\nwith two lines\n"
    assert board.sections(desc) == {"": desc}
    assert board.render(board.sections(desc)) == desc


def test_empty_description_has_no_sections():
    assert board.sections(None) == {}
    assert board.sections("  \n") == {}


def test_compat_names_are_callable():
    for name in ("card", "sections", "update", "col_id"):
        assert callable(getattr(board, name))


@pytest.fixture
def fake_tracker(monkeypatch):
    sent = []

    class Fake:
        def update(self, item_id, **fields):
            sent.append((item_id, fields))
            if "dropped" in (fields.get("metadata") or {}):
                raise SystemExit("pl: metadata.dropped did not persist")
            return {"id": item_id, **fields}

    monkeypatch.setattr(board, "_t", Fake)
    return sent


def test_update_goes_through_the_configured_tracker(fake_tracker):
    got = board.update("a" * 36, metadata={"replace": "new"})
    assert fake_tracker == [("a" * 36, {"metadata": {"replace": "new"}})]
    assert got["metadata"] == {"replace": "new"}


def test_update_raises_when_a_key_does_not_persist(fake_tracker):
    with pytest.raises(SystemExit, match="metadata.dropped did not persist"):
        board.update("a" * 36, metadata={"dropped": 1})
