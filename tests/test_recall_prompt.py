"""Memory entries in a stage prompt: only the card's, its client's and its pod's, capped."""
import pytest

from pl import dispatch, recall
from pl import config as C

pytestmark = pytest.mark.skipif(not __import__("sqlite3").connect(":memory:").execute(
    "select 1 where sqlite_compileoption_used('ENABLE_FTS5')").fetchone(), reason="this Python's SQLite has no FTS5")


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


def _card(cid="42", **meta):
    return {"id": cid, "title": "t", "metadata": meta}


def test_only_entries_for_the_card_its_client_and_its_pod_are_included():
    recall.add("card entry", "run", "run:1", card="42", pod="eng")
    recall.add("client entry", "fact", "chat", card="7", client="acme", pod="eng")
    recall.add("pod entry", "preference", "chat", pod="eng")
    recall.add("other card", "run", "run:2", card="7", pod="eng")
    recall.add("other client", "fact", "chat", card="9", client="globex", pod="eng")
    recall.add("replaced", "fact", "chat", card="42", pod="eng")
    recall.add("new", "fact", "chat", card="42", pod="eng", replaces=recall.search("replaced")[0]["id"])
    out = recall.for_prompt(_card(client="acme"), "eng")
    for kept in ("card entry", "client entry", "pod entry", "new"):
        assert kept in out
    for left in ("other card", "other client", "replaced"):
        assert left not in out
    assert all(line.startswith("- ") for line in out.splitlines())


def test_a_billing_entry_never_reaches_an_engineering_prompt():
    recall.add("invoice terms", "fact", "chat", card="42", client="acme", pod="billing")
    recall.add("shared rule", "preference", "chat", card="42", pod="company")
    out = recall.for_prompt(_card(client="acme"), "eng")
    assert "invoice terms" not in out and "shared rule" in out


def test_the_cap_is_8_entries_or_1500_tokens():
    for i in range(12):
        recall.add(f"short {i}", "run", "run:1", card="42", pod="eng")
    assert len(recall.for_prompt(_card(), "eng").splitlines()) == 8
    for i in range(12):
        recall.add("x" * 2000, "run", "run:1", card="43", pod="eng")
    out = recall.for_prompt(_card("43"), "eng")
    assert 0 < len(out.splitlines()) < 8 and len(out) <= 1500 * 4


def test_the_line_reads_date_kind_text_source():
    recall.add("split into 3 PRs", "rejection", "run:abc", card="42", pod="eng")
    line = recall.for_prompt(_card(), "eng")
    assert line.endswith("rejection: split into 3 PRs (run:abc)") and line[2:12].count("-") == 2


def test_a_stage_on_card_42_gets_a_memory_file_with_only_its_entries():
    recall.add("card entry", "run", "run:1", card="42", pod=C.PROFILE_NAME)
    recall.add("other card", "run", "run:2", card="7", pod=C.PROFILE_NAME)
    arg = dispatch.memory_arg(_card(), "plan", dry=False)
    path = arg.removeprefix(" memory=")
    assert arg.startswith(" memory=") and path.startswith(str(dispatch._memory_dir()))
    text = open(path).read()
    assert "card entry" in text and "other card" not in text
    assert oct(__import__("os").stat(path).st_mode & 0o777) == "0o600"


def test_no_entries_means_no_memory_argument():
    assert dispatch.memory_arg(_card(), "plan", dry=False) == ""
    assert dispatch.memory_arg(_card(), "bogus", dry=False) == ""


def test_a_store_failure_never_stops_the_stage(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no fts")
    monkeypatch.setattr(recall, "for_prompt", boom)
    assert dispatch.memory_arg(_card(), "plan", dry=False) == ""


def test_dry_run_names_the_argument_without_writing(monkeypatch):
    recall.add("card entry", "run", "run:1", card="42", pod=C.PROFILE_NAME)
    assert dispatch.memory_arg(_card(), "plan", dry=True) == " memory=<file>"
    assert not (C.STATE_DIR / "launch").exists()


def test_the_memory_file_lives_in_the_work_folder_agents_can_read(tmp_path):
    # agents start in work_dir; a file under the profile's state folder made Claude ask for a read outside it
    work = tmp_path / "repo"
    (work / ".git" / "info").mkdir(parents=True)
    (C.CONFIG_DIR / "config.toml").write_text(f'[paths]\nwork_dir = "{work}"\n')
    C.load("t")
    recall.add("card entry", "run", "run:1", card="42", pod=C.PROFILE_NAME)
    path = dispatch.memory_arg(_card(), "plan", dry=False).removeprefix(" memory=")
    assert path.startswith(str(work / ".pl" / "launch")) and "card entry" in open(path).read()
    assert ".pl/launch/" in (work / ".git" / "info" / "exclude").read_text().splitlines()
    dispatch.memory_arg(_card(), "plan", dry=False)
    assert (work / ".git" / "info" / "exclude").read_text().splitlines().count(".pl/launch/") == 1
