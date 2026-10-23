import pytest

from pl import agents
from pl.agents import deps_waiting, slice_order


def _card(cid, title="A card", col="Inbox", **meta):
    return {"id": cid, "title": title, "list_id": col, "metadata": {"pipeline_mode": "auto", **meta}}


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
