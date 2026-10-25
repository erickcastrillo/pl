"""Agent memory store: SQLite with full-text search in the profile's state folder."""
import hashlib
import sqlite3
import stat

import pytest

from pl import config as C
from pl import recall


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _has_fts5():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


pytestmark = pytest.mark.skipif(not _has_fts5(), reason="this Python's SQLite has no FTS5")


def test_the_store_is_created_0600_with_version_1():
    recall.add("hello", "fact", "test")
    p = C.STATE_DIR / "memory.db"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    con = sqlite3.connect(p)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    con.close()


def test_add_then_get_returns_every_field():
    i = recall.add("split into 3 PRs", "rejection", "run:abc", card="42", pipeline="p", pod="eng",
                   client="acme", contact="c1", author="ana")
    assert i.startswith("m-") and len(i) == 10
    e = recall.get(i)
    assert (e["kind"], e["text"], e["source"], e["card"], e["pipeline"], e["pod"], e["client"],
            e["contact"], e["author"]) == ("rejection", "split into 3 PRs", "run:abc", "42", "p", "eng",
                                           "acme", "c1", "ana")
    assert e["created_at"] and e["replaced_by"] is None
    assert recall.get("m-00000000") is None


def test_defaults_for_pipeline_and_pod_and_kind_check():
    e = recall.get(recall.add("x", "run", "s"))
    assert e["pipeline"] == "t" and e["pod"] == "t"
    with pytest.raises(ValueError):
        recall.add("x", "gossip", "s")


def test_search_finds_words_and_ranks_by_bm25():
    weak = recall.add("the plan mentions billing once among many other unrelated words here", "fact", "s")
    strong = recall.add("billing billing billing", "fact", "s")
    recall.add("nothing relevant", "fact", "s")
    assert [e["id"] for e in recall.search("billing")] == [strong, weak]
    assert recall.search("billing", limit=1)[0]["id"] == strong


def test_search_can_be_scoped_to_a_pod():
    a = recall.add("invoice rules", "fact", "s", pod="billing")
    recall.add("invoice code", "fact", "s", pod="eng")
    assert [e["id"] for e in recall.search("invoice", pod="billing")] == [a]


def test_a_replaced_entry_is_hidden_from_search_but_shown_by_get():
    old = recall.add("use two PRs", "decision", "s")
    new = recall.add("use three PRs", "decision", "s", replaces=old)
    assert [e["id"] for e in recall.search("PRs")] == [new]
    assert recall.get(old)["replaced_by"] == new


def test_fts_syntax_in_a_query_is_treated_as_words():
    recall.add("alpha beta", "fact", "s")
    for q in ['alpha OR', 'alpha" NOT', "alpha*", "col:alpha", "(alpha", "NEAR(alpha beta)", '"', ""]:
        recall.search(q)
    assert recall.search("alpha OR zzz") == []
    assert len(recall.search("alpha beta")) == 1


def _forget_args(**kw):
    import argparse
    return argparse.Namespace(memory_cmd="forget", contact="c1", yes=True, dry_run=False, json=False, **kw)


def test_forget_contact_deletes_every_linked_entry_and_prints_the_count(capsys):
    old = recall.add("old line", "fact", "s", contact="c1")
    recall.add("new line", "fact", "s", contact="c1", replaces=old)
    older = recall.add("history of a kept entry", "fact", "s")
    mine = recall.add("replaces history", "fact", "s", contact="c1", replaces=older)
    con = recall._db()
    con.execute("INSERT INTO vectors VALUES (?, 'm', 1, x'00')", (mine,))
    con.commit()
    con.close()
    assert recall.forget_contact("c1") == 4
    assert recall.get(old) is None and recall.get(older) is None
    assert recall.search("line") == [] and recall.search("history") == []
    con = recall._db()
    assert con.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    con.close()
    recall.add("again", "fact", "s", contact="c1")
    recall.cmd_memory(_forget_args())
    assert capsys.readouterr().out.strip() == "deleted 1 entries"


def test_forget_leaves_other_contacts_entries():
    keep = recall.add("keep me", "fact", "s", contact="c2")
    none = recall.add("no contact", "fact", "s")
    recall.add("drop me", "fact", "s", contact="c1")
    recall.forget_contact("c1")
    assert recall.get(keep) and recall.get(none)
    assert [e["id"] for e in recall.search("keep")] == [keep]


def test_forget_without_a_terminal_needs_yes(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    i = recall.add("stays", "fact", "s", contact="c1")
    a = _forget_args()
    a.yes = False
    with pytest.raises(SystemExit) as e:
        recall.cmd_memory(a)
    assert e.value.code == 2 and recall.get(i)


def test_forget_dry_run_counts_and_deletes_nothing(capsys):
    i = recall.add("stays", "fact", "s", contact="c1")
    a = _forget_args()
    a.dry_run = True
    recall.cmd_memory(a)
    assert capsys.readouterr().out.strip() == "would delete 1 entries" and recall.get(i)


def test_forget_records_an_event_without_the_contact_id(monkeypatch):
    from pl import events
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, **d: seen.append((kind, d)))
    recall.add("x", "fact", "s", contact="c1")
    recall.cmd_memory(_forget_args())
    assert seen == [("memory_forgot", {"contact_hash": hashlib.sha256(b"c1").hexdigest()[:12], "count": 1})]
