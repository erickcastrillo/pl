import pytest
import tomlkit

from pl import config as C
from pl import cli, ghquota, outbox, trackers

import sys
sys.modules["tests.test_outbox"] = sys.modules[__name__]


class FakeLocal:
    def __init__(self, cfg):
        self.cfg = cfg
        self.items = {}
        self.next_id = 1
        self.fail_update = False

    def create(self, column: str, *, title, description="", tags=(), metadata=None, assigned_to=None):
        cid = str(self.next_id)
        self.next_id += 1
        item = {
            "id": cid,
            "list_id": column,
            "title": title,
            "description": description,
            "tags": list(tags),
            "metadata": metadata or {},
            "assigned_to": assigned_to,
        }
        self.items[cid] = item
        return dict(item)

    def update(self, item_id, *, verify=True, **fields):
        if self.fail_update:
            raise SystemExit("fake update failure")
        cid = str(item_id)
        if cid not in self.items:
            raise KeyError(cid)
        item = self.items[cid]
        if "column" in fields:
            item["list_id"] = fields["column"]
        for k, v in fields.items():
            if k != "column":
                item[k] = v
        return dict(item)

    def delete(self, item_id):
        self.items.pop(str(item_id), None)



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


def test_create_move_edit_each_add_one_entry(fake_home, monkeypatch):
    monkeypatch.setitem(trackers.TYPES, "local", "tests.test_outbox:FakeLocal")
    trackers.reset()
    C.TRACKER = {"type": "local"}
    C.SYNC = {"github": "your-org/projects/5"}
    tr = trackers.get("tracker")
    c = tr.create("Inbox", title="Initial title", description="desc", tags=["t1"])
    cid = c["id"]
    tr.update(cid, column="Approved")
    tr.update(cid, title="x")

    entries = outbox.entries()
    assert len(entries) == 3
    assert entries[0]["op"] == "create"
    assert entries[0]["card"] == cid
    assert entries[0]["fields"]["column"] == "Inbox"
    assert entries[0]["fields"]["title"] == "Initial title"
    assert entries[1]["op"] == "update"
    assert entries[1]["card"] == cid
    assert entries[1]["fields"] == {"column": "Approved"}
    assert entries[2]["op"] == "update"
    assert entries[2]["card"] == cid
    assert entries[2]["fields"] == {"title": "x"}

    assert tr.items[cid]["list_id"] == "Approved"
    assert tr.items[cid]["title"] == "x"


def test_no_sync_setting_no_outbox(fake_home, monkeypatch):
    monkeypatch.setitem(trackers.TYPES, "local", "tests.test_outbox:FakeLocal")
    trackers.reset()
    C.TRACKER = {"type": "local"}
    C.SYNC = {}
    tr = trackers.get("tracker")
    c = tr.create("Inbox", title="No sync")
    tr.update(c["id"], column="Approved")
    assert outbox.path() is None or not outbox.path().exists()


def test_github_tracker_never_queues(fake_home, monkeypatch):
    from pl.trackers import github
    trackers.reset()
    C.TRACKER = {"type": "github-project", "owner": "your-org", "number": 1, "repo": "your-org/repo"}
    C.SYNC = {"github": "your-org/projects/5"}
    monkeypatch.setattr(github, "_gh", lambda args, **kw: "")
    monkeypatch.setattr(github.GitHubProject, "columns", lambda self: {"Inbox": "c1"})
    monkeypatch.setattr(github.GitHubProject, "create", lambda self, col, **kw: {"id": "your-org/repo#1", "list_id": col, "title": kw.get("title")})
    tr = trackers.get("tracker")
    tr.create("Inbox", title="gh item")
    assert outbox.path() is None or not outbox.path().exists()


def test_failed_write_queues_nothing(fake_home, monkeypatch):
    monkeypatch.setitem(trackers.TYPES, "local", "tests.test_outbox:FakeLocal")
    trackers.reset()
    C.TRACKER = {"type": "local"}
    C.SYNC = {"github": "your-org/projects/5"}
    tr = trackers.get("tracker")
    c = tr.create("Inbox", title="Will fail update")
    if outbox.path() and outbox.path().exists():
        outbox.path().unlink()
    tr.fail_update = True
    with pytest.raises(SystemExit):
        tr.update(c["id"], column="Approved")
    assert outbox.entries() == []


def test_delete_adds_one_entry(fake_home, monkeypatch):
    monkeypatch.setitem(trackers.TYPES, "local", "tests.test_outbox:FakeLocal")
    trackers.reset()
    C.TRACKER = {"type": "local"}
    C.SYNC = {"github": "your-org/projects/5"}
    tr = trackers.get("tracker")
    c = tr.create("Inbox", title="Will delete")
    tr.delete(c["id"])
    entries = outbox.entries()
    assert len(entries) == 2
    assert entries[1]["op"] == "delete"
    assert entries[1]["card"] == c["id"]
    assert entries[1]["fields"] == {}


def test_append_oserror_raises_systemexit(fake_home, monkeypatch):
    monkeypatch.setitem(trackers.TYPES, "local", "tests.test_outbox:FakeLocal")
    trackers.reset()
    C.TRACKER = {"type": "local"}
    C.SYNC = {"github": "your-org/projects/5"}
    tr = trackers.get("tracker")
    def fail_append(*a, **kw):
        raise OSError("disk error")
    monkeypatch.setattr(outbox, "append", fail_append)
    with pytest.raises(SystemExit, match=r"pl: outbox: disk error"):
        tr.create("Inbox", title="Fail append")


def test_sync_status_counts_queue(fake_home, monkeypatch, capsys):
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    C.load("t")
    outbox.append("c1", "create", {"title": "c1"})
    outbox.append("c2", "create", {"title": "c2"})

    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "status"])
    cli.main()
    captured = capsys.readouterr().out
    assert "2 changes queued for your-org/projects/5" in captured
    assert "next GitHub window:" in captured


def test_sync_status_json(fake_home, monkeypatch, capsys):
    import json
    import time
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    C.load("t")
    outbox.append("c1", "create", {"title": "c1"})

    monkeypatch.setattr(ghquota, "ledger", lambda *a: {"reset": time.time() + 1800})
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "status", "--json"])
    cli.main()
    data = json.loads(capsys.readouterr().out)
    assert data["queued"] == 1
    assert data["target"] == "your-org/projects/5"
    assert data["next_window"] is not None
    assert "T" in data["next_window"]


def test_sync_status_without_setting_exits_1(fake_home, monkeypatch, capsys):
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n')
    C.load("t")

    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "pl sync: set [sync] github in config.toml" in capsys.readouterr().err


def test_sync_status_spends_no_points(fake_home, monkeypatch, capsys):
    from pl.trackers import github
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    C.load("t")

    def fail_gh(*a, **kw):
        pytest.fail("gh was called!")
    monkeypatch.setattr(github, "_gh", fail_gh)

    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "status"])
    cli.main()
    assert "0 changes queued for your-org/projects/5" in capsys.readouterr().out




