import os
import stat
import pytest

from pl import config as C
from pl.trackers import localstore


def test_db_is_created_0600_with_the_eight_columns(tmp_path):
    db_path = tmp_path / "board.db"
    conn = localstore.open_db(db_path)
    try:
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600

        proto = localstore.protocol(conn)
        assert proto["version"] == 1
        assert len(proto["boards"]) == 1

        cols = localstore.columns(conn)["columns"]
        assert [c["title"] for c in cols] == list(C.COLUMNS)
        assert len(cols) == 8
    finally:
        conn.close()


def test_metadata_round_trips_exactly(tmp_path):
    conn = localstore.open_db(tmp_path / "board.db")
    try:
        col_id = localstore.columns(conn)["columns"][0]["id"]
        meta = {
            "zebra": 1,
            "alpha": {"nested": ["item1", 42, {"key": "🚀 emoji and ñ accent"}]},
            "charlie": True,
        }
        made = localstore.create(conn, col_id, title="Roundtrip Card", metadata=meta)["card"]
        assert made["metadata"] == meta
        assert list(made["metadata"].keys()) == ["zebra", "alpha", "charlie"]

        fetched = localstore.card(conn, made["id"])["card"]
        assert fetched["metadata"] == meta
        assert list(fetched["metadata"].keys()) == ["zebra", "alpha", "charlie"]
    finally:
        conn.close()


def test_update_merges_fields_and_replaces_metadata_whole(tmp_path):
    conn = localstore.open_db(tmp_path / "board.db")
    try:
        col_id = localstore.columns(conn)["columns"][0]["id"]
        made = localstore.create(
            conn,
            col_id,
            title="Initial Title",
            description="Initial Description",
            tags=["tag1", "tag2"],
            metadata={"m1": "v1", "m2": "v2"},
        )["card"]

        updated = localstore.update(
            conn,
            made["id"],
            fields={"title": "Updated Title", "metadata": {"m3": "v3"}},
        )["card"]

        assert updated["title"] == "Updated Title"
        assert updated["description"] == "Initial Description"
        assert updated["tags"] == ["tag1", "tag2"]
        assert updated["metadata"] == {"m3": "v3"}
    finally:
        conn.close()


def test_cards_page_by_cursor(tmp_path):
    conn = localstore.open_db(tmp_path / "board.db")
    try:
        col_id = localstore.columns(conn)["columns"][0]["id"]
        created_ids = []
        for i in range(5):
            c = localstore.create(conn, col_id, title=f"Card {i}")["card"]
            created_ids.append(c["id"])

        page1 = localstore.cards(conn, limit=2)
        assert len(page1["cards"]) == 2
        assert [c["id"] for c in page1["cards"]] == created_ids[:2]
        assert page1["next_cursor"] is not None

        page2 = localstore.cards(conn, cursor=page1["next_cursor"], limit=2)
        assert len(page2["cards"]) == 2
        assert [c["id"] for c in page2["cards"]] == created_ids[2:4]
        assert page2["next_cursor"] is not None

        page3 = localstore.cards(conn, cursor=page2["next_cursor"], limit=2)
        assert len(page3["cards"]) == 1
        assert [c["id"] for c in page3["cards"]] == [created_ids[4]]
        assert page3["next_cursor"] is None
    finally:
        conn.close()


def test_unknown_card_is_not_found_and_unknown_column_is_invalid(tmp_path):
    conn = localstore.open_db(tmp_path / "board.db")
    try:
        col_id = localstore.columns(conn)["columns"][0]["id"]
        made = localstore.create(conn, col_id, title="Valid Card")["card"]

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.card(conn, "missing-id")
        assert exc_info.value.code == "not_found"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.update(conn, "missing-id", fields={"title": "x"})
        assert exc_info.value.code == "not_found"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.delete(conn, "missing-id")
        assert exc_info.value.code == "not_found"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.move(conn, "missing-id", col_id)
        assert exc_info.value.code == "not_found"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.create(conn, "unknown-col-id", title="Card")
        assert exc_info.value.code == "invalid"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.move(conn, made["id"], "unknown-col-id")
        assert exc_info.value.code == "invalid"

        with pytest.raises(localstore.ProtocolError) as exc_info:
            localstore.update(conn, made["id"], fields={"unknown_field": "val"})
        assert exc_info.value.code == "invalid"
    finally:
        conn.close()


def test_reopening_keeps_cards_and_does_not_reseed_columns(tmp_path):
    db_path = tmp_path / "board.db"
    conn1 = localstore.open_db(db_path)
    col_id = localstore.columns(conn1)["columns"][0]["id"]
    card = localstore.create(conn1, col_id, title="Persistent Card")["card"]
    conn1.close()

    conn2 = localstore.open_db(db_path)
    try:
        cols = localstore.columns(conn2)["columns"]
        assert len(cols) == 8
        found = localstore.card(conn2, card["id"])["card"]
        assert found["title"] == "Persistent Card"
    finally:
        conn2.close()
