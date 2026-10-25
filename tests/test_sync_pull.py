import json
import os
import stat

import pytest

from pl import accounts, alerts, board, cli, dispatch, events, ghquota, outbox, sync, usage
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
    assert "sync resolve" not in alert["fix"] and "pl sync now" in alert["fix"]
    assert outbox.entries()[0]["fields"] == {"description": "keep"}
    assert r["conflicts"] == [lid] and r["pulled"] == 0


def test_later_local_edit_after_conflict_stays_queued(world):
    local, lid, fake = _conflict(world)
    sync.pull()
    outbox.append(lid, "update", {"title": "mine again"})
    sync.pull()
    assert outbox.entries()[-1]["fields"] == {"title": "mine again"}


def test_matching_github_value_clears_conflict_and_alert(world):
    local, lid, fake = _conflict(world, queued=False)
    sync.pull()
    local.update(lid, title="theirs")
    assert sync.pull()["conflicts"] == []
    assert not (local.card(lid).get("metadata") or {}).get("remote_edit")
    assert alerts.listing() == []


def test_one_field_resolved_other_still_held(world):
    local, lid, fake = _conflict(world, queued=False)
    local.update(lid, description="mine")
    fake.board[:] = [_gcard(title="theirs", description="theirs")]
    sync.pull()
    local.update(lid, title="theirs")
    sync.pull()
    assert list(local.card(lid)["metadata"]["remote_edit"]) == ["description"]
    assert len(alerts.listing()) == 1


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


# ---------- the 5-minute pull window ----------

def _at(monkeypatch, t):
    monkeypatch.setattr(github, "_clock", lambda: t)


def test_pull_due_at_first_then_not_for_5_minutes(world, monkeypatch):
    assert sync.pull_due()
    sync.pull()
    _at(monkeypatch, NOW + sync.PULL_EVERY - 1)
    assert not sync.pull_due()
    _at(monkeypatch, NOW + sync.PULL_EVERY)
    assert sync.pull_due()


def test_reserve_and_error_pulls_count_for_the_window(world):
    _ledger(200)
    sync.pull()
    assert not sync.pull_due()
    (C.STATE_DIR / "sync-pull.json").unlink()
    (ghquota._path()).unlink()
    world[2].fail = "boom"
    assert sync.pull()["stopped_because"] == "error"
    assert not sync.pull_due()


def test_sync_now_resets_the_window(world, monkeypatch):
    _stub_replay(monkeypatch)
    _now(monkeypatch)
    assert not sync.pull_due()


def test_bad_or_future_stamp_is_due(world):
    stamp = C.STATE_DIR / "sync-pull.json"
    stamp.write_text("[1")
    assert sync.pull_due()
    stamp.write_text(json.dumps({"at": NOW + 60}))
    assert sync.pull_due()


# ---------- the dispatcher pulls before it replays ----------

@pytest.fixture
def passes(world, monkeypatch):
    """The dispatcher's sync, with replay recorded; events collected."""
    order, evs = [], []
    real_pull, real_replay = sync.pull, sync.replay
    monkeypatch.setattr(sync, "pull", lambda: order.append("pull") or real_pull())
    monkeypatch.setattr(sync, "replay", lambda limit=None: order.append("replay") or real_replay(limit=limit))
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: evs.append((kind, d)))
    outbox.append(world[1], "update", {"description": "keep"})
    return order, evs


def test_pass_pulls_then_replays(world, passes):
    local, lid, fake = world
    fake.board[:] = [_gcard(title="from github")]
    dispatch._sync_pass()
    assert local.card(lid)["title"] == "from github"
    assert passes[0] == ["pull", "replay"]


def test_second_pass_inside_5_minutes_reads_no_board(world, passes, monkeypatch):
    dispatch._sync_pass()
    _at(monkeypatch, NOW + 60)
    outbox.append(world[1], "update", {"description": "again"})
    dispatch._sync_pass()
    assert world[2].reads == 1 and passes[0].count("replay") == 2


def test_conflict_pass_holds_back_the_local_title(world, passes):
    local, lid, fake = _conflict(world)
    dispatch._sync_pass()
    assert fake.board[0]["title"] == "theirs" and "title" not in fake.updated.get("o/r#1", {})
    assert all("title" not in e["fields"] for e in outbox.entries())
    assert [a["key"] for a in alerts.listing()] == [f"sync-conflict:{lid}"]


def test_pull_read_error_logs_and_skips_replay(world, passes):
    local, lid, fake = world
    fake.fail = "boom"
    before = local.cards()
    dispatch._sync_pass()
    [(kind, d)] = passes[1]
    assert kind == "error" and d["message"].startswith("sync pull:") and "boom" in d["message"]
    assert "replay" not in passes[0] and local.cards() == before


def test_pull_crash_logs_and_skips_replay(world, passes, monkeypatch):
    def crash():
        raise RuntimeError("x")
    monkeypatch.setattr(sync, "pull", crash)
    dispatch._sync_pass()
    assert passes[1] == [("error", {"message": "sync pull: x"})] and "replay" not in passes[0]


def _one_pass(monkeypatch, paused=False, checked=None):
    monkeypatch.setattr(accounts, "check_parked", lambda: (checked if checked is not None else []).append(1))
    monkeypatch.setattr(usage, "scan", lambda save=True: {})
    monkeypatch.setattr(usage, "summary", lambda st, now=None: {"loops": {}})
    monkeypatch.setattr(dispatch, "paused", lambda: {"since": "now"} if paused else None)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [])
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    for name in ("sweep_untracked", "mirror_to_product", "ensure_services", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)


def test_failed_pull_does_not_stop_the_pass(world, passes, monkeypatch):
    checked = []

    def crash():
        raise RuntimeError("x")
    monkeypatch.setattr(sync, "pull", crash)
    _one_pass(monkeypatch, checked=checked)
    assert checked == [1]
    assert passes[1] == [("error", {"message": "sync pull: x"})]


def test_reserve_pull_writes_no_event(world, passes):
    _ledger(200)
    before = world[0].cards()
    dispatch._sync_pass()
    assert passes[1] == [] and world[0].cards() == before and world[2].reads == 0


@pytest.mark.parametrize("case", ["no-sync", "github-tracker", "paused"])
def test_no_pull_when_off(world, passes, monkeypatch, case):
    pulls = []
    monkeypatch.setattr(sync, "pull", lambda: pulls.append(1))
    if case == "no-sync":
        C.SYNC.pop("github")
    elif case == "github-tracker":
        monkeypatch.setattr(C, "TRACKER", {"type": "github-project"})
    if case == "paused":
        _one_pass(monkeypatch, paused=True)
    else:
        dispatch._sync_pass()
    assert pulls == [] and not any("sync pull" in str(e) for e in passes[1])
