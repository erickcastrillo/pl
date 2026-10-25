"""Agent memory store: SQLite with full-text search in the profile's state folder."""
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


def _embedding(monkeypatch, vectors, fail=False):
    """Set [models.local] embed and fake local_jobs.embed: vectors maps a word to a vector; a text takes the first match."""
    monkeypatch.setattr(C, "MODELS", {"local": {"embed": "nomic-embed-text"}})

    def embed(texts):
        if fail:
            raise OSError("ollama down")
        return [next((v for w, v in vectors.items() if w in t), [0.0, 0.0, 1.0]) for t in texts]
    monkeypatch.setattr("pl.local_jobs.embed", embed)


def test_a_query_with_other_words_of_the_same_meaning_finds_the_entry(monkeypatch):
    _embedding(monkeypatch, {"divide": [1.0, 0.1, 0.0], "split": [0.9, 0.2, 0.0]})
    near = recall.add("split the change into 3 PRs", "rejection", "s")
    recall.add("unrelated note about lunch", "fact", "s")
    rows, mode = recall.search_with_mode("divide the work")
    assert mode == "full-text+semantic"
    assert [e["id"] for e in rows] == [near]


def test_without_embeddings_search_is_full_text_and_says_so():
    recall.add("split the change into 3 PRs", "rejection", "s")
    rows, mode = recall.search_with_mode("divide the work")
    assert (rows, mode) == ([], "full-text")
    assert recall.search_with_mode("split")[1] == "full-text"


def test_an_embed_failure_still_saves_the_entry(monkeypatch):
    _embedding(monkeypatch, {}, fail=True)
    i = recall.add("split the change into 3 PRs", "rejection", "s")
    assert recall.get(i)["text"] == "split the change into 3 PRs"
    assert [e["id"] for e in recall.search("split")] == [i]
    assert recall.search_with_mode("split")[1] == "full-text"


def test_reindex_fills_in_missing_vectors(monkeypatch):
    i = recall.add("split the change into 3 PRs", "rejection", "s")
    _embedding(monkeypatch, {"divide": [1.0, 0.0, 0.0], "split": [1.0, 0.1, 0.0]})
    assert recall.reindex() == 1
    assert recall.reindex() == 0
    assert [e["id"] for e in recall.search("divide")] == [i]


def test_forget_removes_the_entrys_vector_too(monkeypatch):
    _embedding(monkeypatch, {"split": [1.0, 0.1, 0.0]})
    i = recall.add("split the change into 3 PRs", "rejection", "s")
    keep = recall.add("lunch note", "fact", "s")
    assert recall.forget(i)
    ids = [r[0] for r in recall._db().execute("SELECT id FROM vectors")]
    assert ids == [keep]


@pytest.mark.parametrize("source", ["email:inbox/1", "connector:gmail", "trigger:t1"])
def test_an_email_or_connector_source_is_refused(source):
    with pytest.raises(recall.Refused, match="no text from emails or connector results"):
        recall.add("the invoice is due", "fact", source)
    assert recall.search("invoice") == []


def test_a_save_under_pl_untrusted_is_refused(monkeypatch):
    monkeypatch.setenv("PL_UNTRUSTED", "1")
    with pytest.raises(recall.Refused, match="no text from emails or connector results"):
        recall.add("the invoice is due", "fact", "owner:ana")
    assert recall.search("invoice") == []


def test_text_holding_a_configured_secret_is_refused(monkeypatch):
    from pl.trackers import mcp
    monkeypatch.setattr(mcp, "_secrets", {"hunter2-secret-value"})
    with pytest.raises(recall.Refused):
        recall.add("the key is hunter2-secret-value", "fact", "owner:ana")
    assert recall.search("hunter2") == []


def test_a_card_number_is_masked_and_a_plain_number_is_kept():
    masked = recall.get(recall.add("paid with 4111 1111 1111 1111 today", "fact", "owner:ana"))["text"]
    assert masked == "paid with ****1111 today"
    plain = recall.get(recall.add("order 1234567890123 shipped", "fact", "owner:ana"))["text"]
    assert plain == "order 1234567890123 shipped"
