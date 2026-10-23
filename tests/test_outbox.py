import pytest
import tomlkit

from pl import config as C
from pl import outbox


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_sync_settings_load(fake_home):
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    C.load("t")
    assert C.SYNC.get("github") == "your-org/projects/5"


def test_sync_github_bad_value_is_an_error():
    doc = tomlkit.parse('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/5"\n')
    errs = C.validate(doc)
    assert "sync.github must look like owner/projects/N" in errs


def test_sync_github_needs_local_tracker():
    doc = tomlkit.parse('[tracker]\ntype = "github-project"\nowner = "your-org"\nnumber = "5"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    errs = C.validate(doc)
    assert 'sync.github needs [tracker] type = "local"' in errs


def test_sync_git_validation():
    for bad in ('git = ""\n', 'git = 123\n'):
        doc = tomlkit.parse(f"[sync]\n{bad}")
        assert "sync.git must be a folder path" in C.validate(doc)
    ok = tomlkit.parse('[sync]\ngit = "~/pl-backup"\n')
    assert "sync.git must be a folder path" not in C.validate(ok)


def test_missing_outbox_is_empty():
    assert outbox.entries() == []
    assert outbox.count() == 0


def test_append_writes_one_line_with_seq_and_time():
    outbox.append(1, "create", {"title": "card 1"})
    entries = outbox.entries()
    assert len(entries) == 1
    assert entries[0]["seq"] == 1
    assert entries[0]["card"] == "1"
    assert entries[0]["op"] == "create"
    assert entries[0]["fields"] == {"title": "card 1"}
    assert "T" in entries[0]["at"]
    assert outbox.count() == 1

    outbox.append(1, "update", {"title": "card 1 renamed"})
    entries = outbox.entries()
    assert len(entries) == 2
    assert entries[1]["seq"] == 2
    assert entries[1]["op"] == "update"
    assert outbox.count() == 2


def test_entries_survive_reload():
    outbox.append("card-42", "create", {"title": "survive"})
    C.load("t")
    entries = outbox.entries()
    assert len(entries) == 1
    assert entries[0]["card"] == "card-42"
    assert entries[0]["seq"] == 1


def test_outbox_file_mode_0600():
    outbox.append("card-1", "create", {})
    p = outbox.path()
    assert p.is_file()
    assert (p.stat().st_mode & 0o777) == 0o600


def test_torn_last_line_is_skipped():
    outbox.append("c1", "create", {"a": 1})
    p = outbox.path()
    with open(p, "a") as f:
        f.write('{"seq": 2, "card": "broken"\n')
    entries = outbox.entries()
    assert len(entries) == 1
    assert entries[0]["card"] == "c1"


def test_concurrent_appends_serialise_seq():
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(outbox.append, "c1", "create", {"n": 1})
        f2 = ex.submit(outbox.append, "c2", "create", {"n": 2})
        r1, r2 = f1.result(), f2.result()

    seqs = {r1["seq"], r2["seq"]}
    assert seqs == {1, 2}
    entries = outbox.entries()
    assert [e["seq"] for e in entries] == [1, 2]

