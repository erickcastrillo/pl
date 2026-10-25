"""The board copy engine: same columns and cards on another board, resumable, with a dry run."""
import itertools

import pytest

from pl import migrate


class Board:
    """An in-memory tracker that speaks the pl protocol."""

    def __init__(self, name="a"):
        self.name = name
        self._cols = {}
        self._cards = {}
        self._n = itertools.count(1)
        self.clock = "2026-01-01T00:00:00+00:00"

    def columns(self):
        return dict(self._cols)

    def cards(self, query=None):
        return [dict(c) for c in self._cards.values()]

    def card(self, item_id):
        return dict(self._cards[item_id])

    def ensure_column(self, name, *, description=""):
        return self._cols.setdefault(name, f"col-{len(self._cols)}")

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        cid = f"{self.name}-{next(self._n)}"
        self._cards[cid] = {"id": cid, "title": title, "description": description, "tags": list(tags),
                            "metadata": dict(metadata or {}), "list_id": self._cols[column],
                            "updated_at": self.clock, "assigned_to": assigned_to}
        return dict(self._cards[cid])

    def update(self, item_id, *, verify=True, **fields):
        card = self._cards[item_id]
        for k, v in fields.items():
            card[k] = {**card["metadata"], **v} if k == "metadata" else v
        card["updated_at"] = self.clock
        return dict(card)

    def delete(self, item_id):
        del self._cards[item_id]


def seeded(n=50):
    src = Board("src")
    for title in ("Idea", "Approved", "Done"):
        src.ensure_column(title)
    for i in range(n):
        src.create(("Idea", "Approved", "Done")[i % 3], title=f"card {i}", description=f"body {i}",
                   tags=["t", f"n{i}"], metadata={"k": i, "nested": {"a": [i]}}, assigned_to="me@x" if i % 2 else None)
    return src


def run(src, dst, tmp_path, **kw):
    return migrate.copy(src, dst, state_path=tmp_path / "map.json", dry=kw.pop("dry", False),
                        progress=kw.pop("progress", lambda line: None), **kw)


def test_copy_makes_the_same_columns_and_50_cards_with_identical_metadata(tmp_path):
    src, dst = seeded(), Board("dst")
    run(src, dst, tmp_path)
    assert list(dst.columns()) == list(src.columns())
    assert len(dst.cards()) == 50
    mapping = {c["metadata"]["migrated_from"]["id"]: c["id"] for c in dst.cards()}
    assert migrate.verify(src, dst, mapping) == []
    for c in dst.cards():
        assert c["metadata"]["migrated_from"]["updated_at"] == src.clock


def test_a_copy_that_fails_halfway_resumes_without_duplicates(tmp_path):
    src, dst = seeded(), Board("dst")
    real = dst.create
    seen = itertools.count(1)

    def flaky(*a, **kw):
        if next(seen) == 23:
            raise RuntimeError("board went away")
        return real(*a, **kw)
    dst.create = flaky
    with pytest.raises(RuntimeError):
        run(src, dst, tmp_path)
    assert len(dst.cards()) == 22
    first = next(iter(src._cards))
    src.clock = "2026-02-01T00:00:00+00:00"
    src.update(first, title="edited")
    run(src, dst, tmp_path)
    assert len(dst.cards()) == 50
    assert len({c["metadata"]["migrated_from"]["id"] for c in dst.cards()}) == 50
    assert [c["title"] for c in dst.cards() if c["metadata"]["migrated_from"]["id"] == first] == ["edited"]


def test_resume_works_without_the_map_file(tmp_path):
    src, dst = seeded(10), Board("dst")
    run(src, dst, tmp_path)
    (tmp_path / "map.json").unlink()
    run(src, dst, tmp_path)
    assert len(dst.cards()) == 10


def test_dry_run_prints_the_plan_and_calls_no_write(tmp_path):
    src, dst = seeded(4), Board("dst")

    def no_write(*a, **kw):
        raise AssertionError("a dry run wrote to the target")
    dst.create = dst.update = dst.delete = dst.ensure_column = no_write
    lines = []
    plan = run(src, dst, tmp_path, dry=True, progress=lines.append)
    assert sum(x.startswith("would create column") for x in lines) == 3
    assert sum(x.startswith("would copy card") for x in lines) == 4
    assert len(plan["cards"]) == 4
    assert not (tmp_path / "map.json").exists()


def test_verify_names_a_dropped_metadata_key(tmp_path):
    src, dst = seeded(3), Board("dst")
    run(src, dst, tmp_path)
    victim = next(iter(dst._cards.values()))
    del victim["metadata"]["k"]
    mapping = {c["metadata"]["migrated_from"]["id"]: c["id"] for c in dst.cards()}
    out = migrate.verify(src, dst, mapping)
    assert len(out) == 1 and victim["id"] in out[0] and "metadata.k" in out[0]
