import json
import os
import stat

import pytest

from pl import alerts, board, cli, ghquota, outbox, sync
from pl import config as C
from pl.trackers import github
from pl.trackers.local import Local

NOW = 1_000_000.0
BASE = {"column": "Todo", "title": "t0", "description": "", "tags": [], "assigned_to": None}


class FakeProject:
    def __init__(self, cards=None):
        self.board, self.reads, self.fail = cards or [], 0, None
        self.updated = {}

    def columns(self):
        return {"Backlog": "o1", "Todo": "o2"}

    def cards(self, query=None):
        self.reads += 1
        if self.fail:
            raise SystemExit(self.fail)
        return self.board

    def update(self, gid, *, verify=True, **fields):
        self.updated.setdefault(gid, {}).update(fields)
        for g in self.board:
            if g["id"] == gid:
                g.update({"list_id": self.columns()[fields.pop("column")]} if "column" in fields else {}, **fields)
        return {"id": gid}


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "o/projects/5"\nrepo = "o/r"\n')
    C.load("t")
    monkeypatch.setattr(github, "_clock", lambda: NOW)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _gcard(**kw):
    return {"id": "o/r#1", "title": "t0", "description": "", "tags": [], "metadata": {}, "list_id": "o2",
            "assigned_to": None, **kw}


@pytest.fixture
def world(monkeypatch):
    local = Local(C.TRACKER)
    for name in ("Backlog", "Todo"):
        local.ensure_column(name)
    card = local.create("Todo", title="t0")
    sync._save({"ids": {card["id"]: "o/r#1"}, "items": {}, "last_sent": 0})
    sync._save_base({card["id"]: dict(BASE)})
    fake = FakeProject([_gcard()])
    monkeypatch.setattr(sync, "target", lambda: fake)
    monkeypatch.setattr(board, "col_name", lambda lid: {v: k for k, v in local.columns().items()}.get(lid, "?"))
    return local, card["id"], fake


def _ledger(remaining, limit=5000):
    p = ghquota._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"remaining": remaining, "limit": limit, "reset": NOW + 1800, "at": NOW}))


def test_base_file_mode_0600_and_roundtrip():
    sync._save_base({"1": {"title": "x"}})
    assert sync._load_base() == {"1": {"title": "x"}}
    assert stat.S_IMODE(os.stat(sync._base_path()).st_mode) == 0o600


def test_replay_remembers_sent_fields(world):
    local, lid, fake = world
    _ledger(4000)
    outbox.append(lid, "update", {"title": " new ", "list_id": local.columns()["Backlog"]})
    assert sync.replay()["sent"] == 1
    assert sync._load_base()[lid] == {**BASE, "title": "new", "column": "Backlog"}
    assert "metadata" not in sync._load_base()[lid]


def test_strip_removes_field_keeps_others_and_seq():
    outbox.append("A", "update", {"title": "x", "description": "d"})
    outbox.append("A", "update", {"title": "y"})
    outbox.append("B", "update", {"title": "z"})
    assert outbox.strip("A", {"title"}) == 2
    got = outbox.entries()
    assert [(e["seq"], e["card"], e["fields"]) for e in got] == [(1, "A", {"description": "d"}), (3, "B", {"title": "z"})]
    assert stat.S_IMODE(os.stat(outbox.path()).st_mode) == 0o600


def test_strip_column_removes_list_id():
    outbox.append("A", "update", {"list_id": "c1", "title": "x"})
    outbox.strip("A", {"column"})
    assert outbox.entries()[0]["fields"] == {"title": "x"}


def test_pull_takes_github_column_and_title(world):
    local, lid, fake = world
    fake.board[:] = [_gcard(title="from github", list_id="o1")]
    r = sync.pull()
    got = local.card(lid)
    assert got["title"] == "from github" and got["list_id"] == local.columns()["Backlog"]
    assert r["pulled"] == 1 and r["stopped_because"] == "done" and r["conflicts"] == []


def test_pull_queues_nothing(world):
    local, lid, fake = world
    fake.board[:] = [_gcard(title="from github")]
    before = outbox.count()
    sync.pull()
    assert outbox.count() == before == 0


def _conflict(world, queued=True):
    local, lid, fake = world
    local.update(lid, title="mine")
    if queued:
        outbox.append(lid, "update", {"title": "mine", "description": "keep"})
    fake.board[:] = [_gcard(title="theirs")]
    return local, lid, fake


def test_conflict_keeps_local_saves_remote_edit_and_alerts(world):
    local, lid, fake = _conflict(world)
    r = sync.pull()
    got = local.card(lid)
    edit = got["metadata"]["remote_edit"]["title"]
    assert got["title"] == "mine" and edit["value"] == "theirs" and edit["at"]
    [alert] = alerts.listing()
    assert alert["key"] == f"sync-conflict:{lid}" and "title" in alert["title"]
    assert f"pl sync resolve {lid} --keep remote" in alert["fix"]
    assert outbox.entries()[0]["fields"] == {"description": "keep"}
    assert r["conflicts"] == [lid] and r["pulled"] == 0


def test_conflict_when_local_changed_after_last_sync(world):
    local, lid, fake = _conflict(world, queued=False)
    assert sync.pull()["conflicts"] == [lid]
    assert local.card(lid)["metadata"]["remote_edit"]["title"]["value"] == "theirs"
    assert [a["key"] for a in alerts.listing()] == [f"sync-conflict:{lid}"]


def test_repeat_conflict_one_alert_newest_value(world):
    local, lid, fake = _conflict(world)
    assert sync.pull()["conflicts"] == [lid]
    fake.board[:] = [_gcard(title="theirs again")]
    assert sync.pull()["conflicts"] == [lid]
    assert sync.pull()["conflicts"] == []
    assert len(alerts.listing()) == 1
    assert local.card(lid)["metadata"]["remote_edit"]["title"]["value"] == "theirs again"


def test_own_replay_not_seen_as_person(world):
    local, lid, fake = world
    _ledger(4000)
    outbox.append(lid, "update", {"title": "sent"})
    sync.replay()
    local.update(lid, title="sent")
    local.update(lid, title="edited again")
    outbox.append(lid, "update", {"title": "edited again"})
    r = sync.pull()
    assert r["pulled"] == 0 and r["conflicts"] == []
    assert "remote_edit" not in (local.card(lid).get("metadata") or {}) and alerts.listing() == []


def test_pipeline_metadata_never_pulled(world):
    local, lid, fake = world
    local.update(lid, metadata={"worker": {"pane": "%1"}})
    fake.board[:] = [_gcard(metadata={"worker": {"pane": "%9"}, "pr_urls": ["u"]})]
    sync.pull()
    assert local.card(lid)["metadata"] == {"worker": {"pane": "%1"}}


def test_unmapped_github_card_skipped(world):
    local, lid, fake = world
    fake.board[:] = [_gcard(id="o/r#99", title="stranger")]
    r = sync.pull()
    assert r["pulled"] == 0 and [c["title"] for c in local.cards()] == ["t0"]


def test_below_reserve_reads_nothing(world):
    local, lid, fake = world
    _ledger(200)
    r = sync.pull()
    assert r["stopped_because"] == "reserve" and r["pulled"] == 0 and fake.reads == 0


def test_read_error_changes_nothing(world):
    local, lid, fake = world
    fake.fail = "boom"
    before = (local.cards(), sync._load_base())
    r = sync.pull()
    assert r["stopped_because"] == "error" and "boom" in r["error"]
    assert (local.cards(), sync._load_base()) == before


def _stub_replay(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "replay", lambda: calls.append(1) or {
        "sent": 0, "remaining": 0, "stopped_because": "done", "next_window": None, "error": None})
    return calls


def _now(monkeypatch, *extra):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "now", *extra])
    cli.main()


def test_sync_now_prints_cards_pulled(world, monkeypatch, capsys):
    _stub_replay(monkeypatch)
    world[2].board[:] = [_gcard(title="from github", list_id="o1")]
    _now(monkeypatch)
    assert "1 card pulled" in capsys.readouterr().out


def test_sync_now_json_has_pulled_and_conflicts(world, monkeypatch, capsys):
    _stub_replay(monkeypatch)
    _, lid, _ = _conflict(world)
    _now(monkeypatch, "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["pulled"] == 0 and out["conflicts"] == [lid] and out["sent"] == 0


def test_sync_now_reserve_json(world, monkeypatch, capsys):
    _stub_replay(monkeypatch)
    _ledger(200)
    _now(monkeypatch, "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["pulled"] == 0 and out["stopped_because"] == "reserve"


def test_sync_now_pull_error_exits_1_no_replay(world, monkeypatch, capsys):
    calls = _stub_replay(monkeypatch)
    world[2].fail = "boom"
    with pytest.raises(SystemExit) as e:
        _now(monkeypatch)
    assert e.value.code == 1 and "boom" in capsys.readouterr().err and calls == []
