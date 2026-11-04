import fcntl
import json
import os
import stat

import pytest

from pl import cli, commands
from pl import config as C
from pl.trackers import localrestore, localstore
from pl.trackers.local import Local


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    C.CONFIG_DIR = tmp_path / "prof"
    C.STATE_DIR = C.CONFIG_DIR / "state"
    C.LOCK_FILE = C.STATE_DIR / "pl-dispatch.lock"
    C.STATE_DIR.mkdir(parents=True)
    C.TRACKER = {"type": "local"}
    C.PROFILE_NAME = "t"
    monkeypatch.setattr(C, "TRACKER_DRY_RUN", False, raising=False)
    yield C.CONFIG_DIR
    for k, v in saved.items():
        setattr(C, k, v)


def _backup(tmp_path, **over):
    d = tmp_path / "bk"
    d.mkdir(exist_ok=True)
    data = {
        "columns": [{"id": "ca", "title": "Alpha"}, {"id": "cb", "title": "Beta"}],
        "cards": [
            {"id": "k1", "column": "Alpha", "title": "one", "description": "# PIPELINE: SPEC\nbody", "tags": ["x"],
             "assigned_to": "me", "metadata": {"pr": 1}, "seq": 40},
            {"id": "k2", "list_id": "cb", "title": "two", "description": "", "tags": [], "assigned_to": None,
             "metadata": {}, "seq": 41},
            {"id": "k3", "column": "Beta", "title": "three", "description": "d", "tags": ["y", "z"],
             "assigned_to": None, "metadata": {"a": {"b": 2}}},
        ],
        **over,
    }
    (d / "board.json").write_text(json.dumps(data))
    return d


def _rows(db=None):
    conn = localstore.open_db(db or localrestore.db_path())
    cols = [tuple(r) for r in conn.execute("SELECT id, title, pos FROM columns ORDER BY pos")]
    cards = [tuple(r) for r in conn.execute("SELECT * FROM cards ORDER BY id")]
    conn.close()
    return cols, cards


def _seed():
    t = Local({})
    t.create("Inbox", title="old", description="old body")
    k = t.create("Inbox", title="doomed", description="x")
    return k


def test_restore_replaces_board_with_backup(tmp_path):
    _seed()
    d = _backup(tmp_path)
    out = localrestore.restore(d, True)
    assert out.startswith("restored 3 cards in 2 columns from")
    conn = localstore.open_db(localrestore.db_path())
    cols = localstore.columns(conn)["columns"]
    assert [(c["id"], c["title"]) for c in cols] == [("ca", "Alpha"), ("cb", "Beta")]
    got = {c["id"]: c for c in localstore.cards(conn)["cards"]}
    assert set(got) == {"k1", "k2", "k3"}
    k1 = got["k1"]
    assert (k1["list_id"], k1["title"], k1["description"], k1["tags"], k1["assigned_to"], k1["metadata"]) == (
        "ca", "one", "# PIPELINE: SPEC\nbody", ["x"], "me", {"pr": 1})
    assert got["k2"]["list_id"] == "cb"
    assert got["k3"]["list_id"] == "cb" and got["k3"]["metadata"] == {"a": {"b": 2}}


def test_new_card_after_restore_gets_higher_seq(tmp_path):
    _seed()
    localrestore.restore(_backup(tmp_path), True)
    conn = localstore.open_db(localrestore.db_path())
    top = max(c["seq"] for c in localstore.cards(conn)["cards"])
    assert top >= 41
    new = localstore.create(conn, "ca", "fresh")["card"]
    assert new["seq"] > top


def test_missing_seq_never_goes_back(tmp_path):
    _seed()
    before = _rows()[1]
    high = max(r[-1] for r in before)
    localrestore.restore(_backup(tmp_path), True)
    seqs = [r[-1] for r in _rows()[1]]
    assert len(set(seqs)) == 3 and min(seqs) > 0
    new = localstore.create(localstore.open_db(localrestore.db_path()), "ca", "n")["card"]
    assert new["seq"] > max(seqs) and new["seq"] > high - 1


def test_without_yes_changes_nothing(tmp_path, capsys):
    _seed()
    before = _rows()
    with pytest.raises(SystemExit) as e:
        localrestore.restore(_backup(tmp_path), False)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "would replace 2 local cards with 3 cards" in out and "--yes" in out
    assert _rows() == before


@pytest.mark.parametrize("kind", ["nofile", "badjson", "nocards", "nocolumns", "badcard", "unknowncol"])
def test_bad_backup_reports_reason(tmp_path, kind):
    _seed()
    before = _rows()
    d = tmp_path / "bk"
    d.mkdir()
    f = d / "board.json"
    if kind == "badjson":
        f.write_text("{nope")
    elif kind == "nocards":
        f.write_text(json.dumps({"columns": ["A"]}))
    elif kind == "nocolumns":
        f.write_text(json.dumps({"cards": []}))
    elif kind == "badcard":
        f.write_text(json.dumps({"columns": ["A"], "cards": [{"column": "A"}]}))
    elif kind == "unknowncol":
        f.write_text(json.dumps({"columns": ["A"], "cards": [{"id": "1", "title": "t", "column": "Z"}]}))
    with pytest.raises(SystemExit) as e:
        localrestore.restore(d, True)
    assert str(e.value).startswith(f"pl board restore: {d}/board.json: ")
    assert _rows() == before


def test_failure_mid_restore_rolls_back(tmp_path, monkeypatch):
    _seed()
    before = _rows()
    real = localstore.open_db

    class Failing:
        def __init__(self, conn):
            self.conn, self.inserts = conn, 0

        def execute(self, sql, *a):
            if sql.lstrip().startswith("INSERT INTO cards"):
                self.inserts += 1
                if self.inserts == 2:
                    import sqlite3
                    raise sqlite3.OperationalError("database or disk is full")
            return self.conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(localstore, "open_db", lambda *a, **k: Failing(real(*a, **k)))
    with pytest.raises(SystemExit) as e:
        localrestore.restore(_backup(tmp_path), True)
    assert "failed, board unchanged" in str(e.value) and "disk is full" in str(e.value)
    monkeypatch.setattr(localstore, "open_db", real)
    assert _rows() == before


def test_refuses_non_local_tracker(tmp_path):
    C.TRACKER = {"type": "github-project"}
    with pytest.raises(SystemExit) as e:
        localrestore.restore(_backup(tmp_path), True)
    assert str(e.value) == 'pl board restore: needs [tracker] type = "local"'
    assert not (C.CONFIG_DIR / "board.db").exists()


def test_refuses_while_dispatcher_holds_lock(tmp_path):
    _seed()
    before = _rows()
    with open(C.LOCK_FILE, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as e:
            localrestore.restore(_backup(tmp_path), True)
    assert str(e.value) == "pl board restore: stop the dispatcher for this profile first"
    assert _rows() == before


def test_no_outbox_entries_and_one_event(tmp_path):
    _seed()
    outbox = C.STATE_DIR / "outbox.jsonl"
    size = outbox.stat().st_size if outbox.exists() else None
    d = _backup(tmp_path)
    localrestore.restore(d, True)
    assert (outbox.stat().st_size if outbox.exists() else None) == size
    lines = [json.loads(x) for x in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]
    got = [x for x in lines if x["kind"] == "board restored"]
    assert len(got) == 1 and got[0]["source"] == str(d) and got[0]["cards"] == 3


def test_safety_copy_is_0600(tmp_path):
    _seed()
    out = localrestore.restore(_backup(tmp_path), True)
    copies = list(C.CONFIG_DIR.glob("board.db.before-restore-*"))
    assert len(copies) == 1 and str(copies[0]) in out
    assert stat.S_IMODE(os.stat(copies[0]).st_mode) == 0o600
    titles = {r[3] for r in _rows(copies[0])[1]}
    assert titles == {"old", "doomed"}


def test_cli_board_restore_runs(tmp_path, capsys):
    _seed()
    d = _backup(tmp_path)
    commands.cmd_board(cli.build_parser().parse_args(["board", "restore", "--from", str(d), "--yes"]))
    assert "restored 3 cards in 2 columns from" in capsys.readouterr().out


def test_board_restore_needs_from():
    with pytest.raises(SystemExit) as e:
        commands.cmd_board(cli.build_parser().parse_args(["board", "restore"]))
    assert "pl board restore: --from DIR is required" in str(e.value)


def test_help_lists_board_restore():
    assert "pl board restore --from DIR [--yes]" in cli.__doc__
