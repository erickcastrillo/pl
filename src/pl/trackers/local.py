"""Local SQLite board tracker.

Cards live in a SQLite database in the profile folder (board.db by default).
Implements the standard pl board protocol in-process through localstore.
"""
from pathlib import Path

from pl import config as C
from pl.trackers import localstore


class Local:
    def __init__(self, cfg=None):
        cfg = cfg or {}
        p = cfg.get("path") or "board.db"
        self.path = (C.CONFIG_DIR / p) if C.CONFIG_DIR else Path(p)
        self._conn = None
        self._columns = None

    def _db(self):
        if self._conn is None:
            self._conn = localstore.open_db(self.path)
        return self._conn

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(self._db(), *args, **kwargs)
        except localstore.ProtocolError as e:
            raise SystemExit(f"pl: local board: {e.code}: {e.message}") from e

    def columns(self) -> dict[str, str]:
        if self._columns is None:
            res = self._call(localstore.columns)
            self._columns = {c["title"]: c["id"] for c in res["columns"]}
        return self._columns

    def _col(self, name: str) -> str:
        if name in self.columns():
            return self.columns()[name]
        if name in self.columns().values():
            return name
        raise SystemExit(f"pl: the board has no column {name!r} (for Inbox run: pl board init)")

    def cards(self, query: dict | None = None) -> list[dict]:
        cards_list = []
        cursor = None
        extra = {}
        if query:
            if "assigned_to" in query:
                extra["assigned_to"] = query["assigned_to"]
            if "updated_since" in query:
                extra["updated_since"] = query["updated_since"]
        while True:
            res = self._call(localstore.cards, cursor=cursor, **extra)
            cards_list.extend(res["cards"])
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return cards_list

    def card(self, item_id: str) -> dict:
        res = self._call(localstore.card, id=item_id)
        return res["card"]

    def create(self, column: str, *, title: str, description: str = "", tags=(), metadata=None, assigned_to=None) -> dict:
        col_id = self._col(column)
        res = self._call(
            localstore.create,
            column_id=col_id,
            title=title,
            description=description,
            tags=list(tags),
            metadata=dict(metadata or {}),
            assigned_to=assigned_to,
        )
        return res["card"]

    def update(self, item_id: str, *, verify: bool = True, **fields) -> dict:
        column = fields.pop("column", None)
        col_id = self._col(column) if column is not None else None
        if column is not None:
            fields["column_id"] = col_id

        if verify and "metadata" in fields:
            fields["metadata"] = {**(self.card(item_id).get("metadata") or {}), **fields["metadata"]}

        if fields:
            res = self._call(localstore.update, id=item_id, fields=fields)
        else:
            res = self._call(localstore.card, id=item_id)

        if not verify:
            return res.get("card", {})

        got = self.card(item_id)
        if col_id is not None:
            fields["list_id"] = col_id
            fields.pop("column_id", None)
        for k, v in fields.items():
            if k == "metadata":
                for mk, mv in v.items():
                    if (got.get("metadata") or {}).get(mk) != mv:
                        raise SystemExit(f"pl: metadata.{mk} did not persist on {str(item_id)[:8]}")
            elif (got.get(k) or "") != (v or ""):
                raise SystemExit(f"pl: field {k} did not persist on {str(item_id)[:8]}")
        return got

    def delete(self, item_id: str) -> None:
        self._call(localstore.delete, id=item_id)

    def ensure_column(self, name: str, *, description: str = "") -> str:
        if name in self.columns():
            return self.columns()[name]
        res = self._call(localstore.ensure_column, title=name, description=description)
        cid = res["column"]["id"]
        self._columns[name] = cid
        return cid

    def url(self, item_id: str) -> str:
        return str(item_id)

    def test(self) -> str:
        cols = len(self.columns())
        crds = len(self.cards())
        return f"ok: local board, {cols} columns, {crds} cards"
