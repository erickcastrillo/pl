import pytest

from pl import alerts, cli, outbox, sync

import test_sync_pull as P

_gcard, profile, world = P._gcard, P.profile, P.world


def _held(local, lid, fields, title="A"):
    local.update(lid, title=title, metadata={"owner": "me", "remote_edit": {
        f: {"value": v, "at": "2026-10-25T00:00:00+00:00"} for f, v in fields.items()}})
    alerts.open(f"sync-conflict:{lid}", "high", "changed on both", "resolve it")


def _resolve(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "sync", "resolve", *args])
    cli.main()


def _meta(local, lid):
    return local.card(lid).get("metadata") or {}


def test_keep_remote_takes_github_title_no_outbox(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    before = outbox.count()
    assert sync.resolve(lid, "remote") == ["title"]
    assert local.card(lid)["title"] == "B"
    assert _meta(local, lid) == {"owner": "me"}
    assert outbox.count() == before


def test_keep_local_keeps_title_queues_one_entry(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    before = outbox.count()
    sync.resolve(lid, "local")
    assert local.card(lid)["title"] == "A"
    assert _meta(local, lid) == {"owner": "me"}
    got = outbox.entries()[before:]
    assert [(e["card"], e["op"], e["fields"]) for e in got] == [(lid, "update", {"title": "A"})]


@pytest.mark.parametrize("keep", ["remote", "local"])
def test_resolve_closes_alert(world, keep):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    sync.resolve(lid, keep)
    assert alerts.listing() == []


def test_several_fields_remote(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B", "column": "Backlog"})
    sync.resolve(lid, "remote")
    got = local.card(lid)
    assert got["title"] == "B" and got["list_id"] == local.columns()["Backlog"]


def test_several_fields_local(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B", "column": "Backlog"})
    sync.resolve(lid, "local")
    got = local.card(lid)
    assert got["title"] == "A" and got["list_id"] == local.columns()["Todo"]
    assert sorted((e["fields"] for e in outbox.entries()), key=str) == [{"column": "Todo"}, {"title": "A"}]


def test_keep_remote_drops_held_entries(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    outbox.append(lid, "update", {"title": "A2"})
    sync.resolve(lid, "remote")
    assert outbox.entries() == []


def test_keep_local_replaces_held_entry_with_newest(world):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    outbox.append(lid, "update", {"title": "A0"})
    sync.resolve(lid, "local")
    assert [e["fields"] for e in outbox.entries()] == [{"title": "A"}]


@pytest.mark.parametrize("keep", ["remote", "local"])
def test_base_records_github_value(world, keep):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    sync.resolve(lid, keep)
    assert sync._load_base()[lid]["title"] == "B"


def test_no_remote_edit_exits_and_changes_nothing(world):
    local, lid, _ = world
    before = (local.card(lid), outbox.entries(), sync._load_base())
    with pytest.raises(SystemExit, match=f"card {lid} has no remote edit"):
        sync.resolve(lid, "remote")
    assert (local.card(lid), outbox.entries(), sync._load_base()) == before


def test_resolve_then_pull_no_new_conflict(world):
    local, lid, fake = world
    _held(local, lid, {"title": "B"})
    fake.board[:] = [_gcard(title="B")]
    sync.resolve(lid, "local")
    r = sync.pull()
    assert r["conflicts"] == [] and "remote_edit" not in _meta(local, lid)
    assert [e["fields"] for e in outbox.entries()] == [{"title": "A"}]
    assert alerts.listing() == []


def test_cli_keep_remote_exit_0(world, monkeypatch, capsys):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    _resolve(monkeypatch, lid, "--keep", "remote")
    assert "kept remote for title" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "card", lid])
    cli.main()
    out = capsys.readouterr().out
    assert "B" in out and "remote_edit" not in out


def test_cli_keep_local_says_queued(world, monkeypatch, capsys):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    _resolve(monkeypatch, lid, "--keep", "local")
    assert "kept local for title; queued, the next sync sends it" in capsys.readouterr().out


def _fails(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    return e.value.code, capsys.readouterr().err


def test_cli_no_sync_setting_exits_1(world, profile, monkeypatch, capsys):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    cfg = profile / ".pl-t" / "config.toml"
    cfg.write_text(cfg.read_text().split("[sync]")[0])
    want = _fails(monkeypatch, capsys, "sync", "now")
    got = _fails(monkeypatch, capsys, "sync", "resolve", lid, "--keep", "remote")
    assert got == want and got[0] == 1 and "[sync] github" in got[1]
    assert local.card(lid)["title"] == "A"


def test_cli_missing_keep_usage_error(world, monkeypatch, capsys):
    local, lid, _ = world
    _held(local, lid, {"title": "B"})
    code, err = _fails(monkeypatch, capsys, "sync", "resolve", lid)
    assert code != 0 and "remote" in err and "local" in err
    assert local.card(lid)["title"] == "A"


def test_cli_bad_keep_value_usage_error(world, monkeypatch, capsys):
    code, err = _fails(monkeypatch, capsys, "sync", "resolve", "x", "--keep", "both")
    assert code == 2 and "remote" in err and "local" in err


def test_cli_unknown_card_exits_1(world, monkeypatch, capsys):
    want = _fails(monkeypatch, capsys, "card", "nope")
    got = _fails(monkeypatch, capsys, "sync", "resolve", "nope", "--keep", "remote")
    assert got == want and got[0] != 0


def test_cli_no_remote_edit_exits_1(world, monkeypatch, capsys):
    _, lid, _ = world
    code, err = _fails(monkeypatch, capsys, "sync", "resolve", lid, "--keep", "remote")
    assert code == 1 and f"card {lid} has no remote edit" in err
