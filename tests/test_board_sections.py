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


def test_pl_card_prints_unreadable_and_the_text_found(monkeypatch, capsys):
    import argparse
    from pl import commands
    broken_tail = "<!-- pl:metadata -->\n```json\n{broken\n```\n"
    fake_card = {"id": "c-1", "title": "Card with broken block", "list_id": "col-1", "tags": [],
                 "updated_at": "2026-10-01", "unreadable": broken_tail, "description": "Card description"}
    monkeypatch.setattr(commands, "find_card", lambda ref: fake_card)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Inbox")
    commands.cmd_card(argparse.Namespace(id="c-1", delete=False))
    out = capsys.readouterr().out
    assert "metadata: unreadable\n" + broken_tail in out



def test_plan_slice_reads_whole_numbers_from_metadata():
    assert board.plan_slice({"metadata": {"plan": 21, "slice": 2}}) == (21, 2)
    assert board.plan_slice({"metadata": {"plan": "21", "slice": " 5 "}}) == (21, 5)
    assert board.plan_slice({"metadata": {"plan": 21}}) == (21, None)
    assert board.plan_slice({}) == (None, None)
    assert board.plan_slice({"metadata": {"plan": "abc", "slice": "²"}}) == (None, None)
    assert board.plan_slice({"metadata": {"plan": True, "slice": 2.5}}) == (None, None)


def test_plan_order_sorts_by_slice_then_card_id():
    def c(cid, s):
        return {"id": cid, "metadata": {"plan": 21, **({"slice": s} if s is not None else {})}}
    cards = [c("o/r#9", 5), c("o/r#3", 1), c("o/r#12", 2), c("o/r#4", 2), c("o/r#1", None)]
    assert [x["id"] for x in sorted(cards, key=board.plan_order)] == ["o/r#3", "o/r#4", "o/r#12", "o/r#9", "o/r#1"]
