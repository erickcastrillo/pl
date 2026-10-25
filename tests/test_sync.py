import json
import os
import stat
import time

import pytest

from pl import board, cli, dispatch, events, ghquota, outbox, sync
from pl import config as C
from pl.trackers import github

NOW = 1_000_000.0
RESET = NOW + 1800


class FakeProject:
    """A GitHub Project that records every call; fail_on is the 1-based call number that raises."""

    def __init__(self, fail_on=None, on_call=None):
        self.calls, self.cards, self.fail_on, self.on_call = [], {}, fail_on, on_call

    def _hit(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if self.on_call:
            self.on_call(len(self.calls))
        if self.fail_on == len(self.calls):
            raise SystemExit("fake github error")

    def create(self, column, **kw):
        self._hit("create", column, **kw)
        gid = f"o/r#{len(self.cards) + 100}"
        self.cards[gid] = {"column": column, **kw}
        return {"id": gid}

    def update(self, gid, *, verify=True, **fields):
        self._hit("update", gid, verify=verify, **fields)
        self.cards.setdefault(gid, {}).update(fields)
        return {"id": gid}

    def delete(self, gid):
        self._hit("delete", gid)
        self.cards.pop(gid, None)


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
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
    monkeypatch.setattr(board, "col_name", lambda lid: {"c-todo": "Todo"}.get(lid, "?"))
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake(monkeypatch):
    f = FakeProject()
    monkeypatch.setattr(sync, "target", lambda: f)
    return f


def _ledger(remaining, limit=5000, reset=RESET):
    p = ghquota._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"remaining": remaining, "limit": limit, "reset": reset, "at": NOW}))


def _run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", *argv])
    cli.main()


def _queue_three():
    outbox.append("1", "create", {"column": "Todo", "title": "a", "description": "", "tags": [], "metadata": None,
                                  "assigned_to": None})
    outbox.append("1", "update", {"list_id": "c-todo"})
    outbox.append("1", "update", {"title": "b"})


def test_drop_through_keeps_later_entries():
    for i in range(3):
        outbox.append(str(i), "delete", {})
    assert outbox.drop_through(2) == 1
    assert [e["seq"] for e in outbox.entries()] == [3]
    assert outbox.append("9", "delete", {})["seq"] == 4


def test_drop_through_keeps_mode_0600():
    outbox.append("1", "delete", {})
    outbox.append("2", "delete", {})
    outbox.drop_through(1)
    assert stat.S_IMODE(os.stat(outbox.path()).st_mode) == 0o600


def test_drop_through_past_end_empties():
    outbox.append("1", "delete", {})
    assert outbox.drop_through(99) == 0
    assert outbox.entries() == []


def test_replay_sends_create_move_edit_and_empties_outbox(fake):
    _ledger(4000)
    _queue_three()
    r = sync.replay()
    assert [c[0] for c in fake.calls] == ["create", "update", "update"]
    assert fake.cards["o/r#100"]["column"] == "Todo" and fake.cards["o/r#100"]["title"] == "b"
    assert outbox.count() == 0 and r["sent"] == 3 and r["stopped_because"] == "done"


def test_replay_while_another_replay_runs_sends_nothing(fake):
    _ledger(4000)
    _queue_three()
    with sync._replay_lock():
        r = sync.replay()
    assert fake.calls == [] and outbox.count() == 3
    assert r["sent"] == 0 and r["stopped_because"] == "busy" and r["error"] is None


def test_replay_releases_its_lock_when_done(fake):
    _ledger(4000)
    _queue_three()
    sync.replay()
    assert sync.replay()["stopped_because"] == "done"


def test_replay_keeps_seq_order(fake):
    _ledger(4000)
    _queue_three()
    sync.replay()
    assert [(c[0], c[2].get("title"), c[2].get("column")) for c in fake.calls] == [
        ("create", "a", None), ("update", None, "Todo"), ("update", "b", None)]


def test_error_on_entry_2_keeps_2_and_3(fake):
    _ledger(4000)
    _queue_three()
    fake.fail_on = 2
    r = sync.replay()
    assert [e["seq"] for e in outbox.entries()] == [2, 3]
    assert r["stopped_because"] == "error" and "entry 2" in r["error"] and "card 1" in r["error"]
    fake.fail_on = None
    r = sync.replay()
    assert r["sent"] == 2 and outbox.count() == 0


def test_below_reserve_sends_nothing(fake):
    _ledger(200)
    _queue_three()
    r = sync.replay()
    assert fake.calls == [] and outbox.count() == 3
    assert r["stopped_because"] == "reserve" and r["next_window"]


def test_stops_at_half_budget(fake):
    _ledger(40, limit=100)

    def spend(n):
        _ledger(40 - 8 * n, limit=100)
    fake.on_call = spend
    for i in range(5):
        outbox.append(str(i), "create", {"column": "Todo", "title": str(i)})
    r = sync.replay()
    assert len(fake.calls) == 2 and r["stopped_because"] == "half-budget" and r["remaining"] == 3


def test_crash_after_confirm_never_resends(fake):
    _ledger(4000)
    _queue_three()
    (C.CONFIG_DIR / "sync-ids.json").write_text(json.dumps({"ids": {"1": "o/r#7"}, "last_sent": 2}))
    sync.replay()
    assert [c[0] for c in fake.calls] == ["update"] and fake.calls[0][1][0] == "o/r#7"


def test_create_already_in_ids_sends_nothing(fake):
    _ledger(4000)
    outbox.append("1", "create", {"column": "Todo", "title": "a"})
    (C.CONFIG_DIR / "sync-ids.json").write_text(json.dumps({"ids": {"1": "o/r#7"}, "last_sent": 0}))
    r = sync.replay()
    assert fake.calls == [] and outbox.count() == 0 and r["sent"] == 1


def test_create_stores_github_id_for_later_updates(fake):
    _ledger(4000)
    _queue_three()
    sync.replay()
    assert fake.calls[1][1][0] == fake.calls[2][1][0] == "o/r#100"


def test_update_without_github_id_blocks_queue(fake):
    _ledger(4000)
    outbox.append("9", "update", {"title": "x"})
    outbox.append("9", "update", {"title": "y"})
    r = sync.replay()
    assert fake.calls == [] and outbox.count() == 2 and r["stopped_because"] == "error"
    assert r["error"] == "card 9 has no GitHub item yet: run pl sync init --from-github"


def test_no_reading_sends_one_then_rereads(fake):
    fake.on_call = lambda n: _ledger(100)
    _queue_three()
    r = sync.replay()
    assert len(fake.calls) == 1 and r["stopped_because"] == "reserve" and r["remaining"] == 2


def test_replay_over_budget_counts_as_reserve(fake):
    _ledger(4000)
    _queue_three()
    fake.on_call = lambda n: (_ for _ in ()).throw(github.OverBudget(RESET, "kept for writes"))
    r = sync.replay()
    assert r["stopped_because"] == "reserve" and outbox.count() == 3


def test_create_without_repo_is_an_error(fake):
    _ledger(4000)
    C.SYNC.pop("repo")
    outbox.append("1", "create", {"column": "Todo", "title": "a"})
    r = sync.replay()
    assert r["stopped_because"] == "error" and "[sync] repo" in r["error"] and outbox.count() == 1


def test_sync_ids_mode_0600(fake):
    _ledger(4000)
    _queue_three()
    sync.replay()
    assert stat.S_IMODE(os.stat(C.CONFIG_DIR / "sync-ids.json").st_mode) == 0o600


def test_new_entries_after_a_full_replay_are_not_dropped(fake):
    _ledger(4000)
    _queue_three()
    sync.replay()
    outbox.append("1", "update", {"title": "later"})
    sync.replay()
    assert fake.calls[-1][2]["title"] == "later"


def test_sync_now_prints_counts(fake, capsys, monkeypatch):
    _ledger(4000)
    _queue_three()
    _run(monkeypatch, "sync", "now")
    assert capsys.readouterr().out.strip() == "3 changes sent, 0 remain"


def test_sync_now_json_matches_plain(fake, capsys, monkeypatch):
    _ledger(4000)
    _queue_three()
    _run(monkeypatch, "sync", "now", "--json")
    got = json.loads(capsys.readouterr().out)
    assert got == {"sent": 3, "remaining": 0, "stopped_because": "done", "next_window": got["next_window"]}


def test_sync_now_error_exits_nonzero(fake, capsys, monkeypatch):
    _ledger(4000)
    _queue_three()
    fake.fail_on = 2
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "sync", "now")
    assert e.value.code == 1
    assert "entry 2 (card 1)" in capsys.readouterr().err


def test_sync_now_reserve_prints_window(fake, capsys, monkeypatch):
    _ledger(200)
    _queue_three()
    _run(monkeypatch, "sync", "now")
    out = capsys.readouterr().out
    assert "0 changes sent, 3 remain" in out
    assert "next GitHub window: " + time.strftime("%H:%M", time.localtime(RESET)) in out


def test_sync_now_without_setting_exits_1(fake_home, capsys, monkeypatch):
    (fake_home / ".pl-t" / "config.toml").write_text('[tracker]\ntype = "local"\n')
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "sync", "now")
    assert e.value.code == 1
    assert "pl sync: set [sync] github in config.toml" in capsys.readouterr().err


def _pass(monkeypatch):
    """One dispatcher pass over an empty board."""
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [])
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    for name in ("sweep_untracked", "mirror_to_product", "ensure_services", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)


@pytest.fixture
def pass_env(monkeypatch):
    from pl import accounts, usage
    monkeypatch.setattr(accounts, "check_parked", lambda: None)
    monkeypatch.setattr(usage, "scan", lambda save=True: {})
    monkeypatch.setattr(usage, "summary", lambda st, now=None: {"loops": {}})
    monkeypatch.setattr(dispatch, "paused", lambda: False)
    outbox.append("1", "delete", {})
    calls, evs = [], []
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: evs.append((kind, d)))
    return calls, evs


def test_dispatcher_pass_replays_outbox(monkeypatch, pass_env):
    calls, _ = pass_env
    monkeypatch.setattr(sync, "replay", lambda limit=None: calls.append(limit) or {"stopped_because": "done"})
    _pass(monkeypatch)
    assert calls == [20]


def test_dispatcher_replay_error_logged_pass_goes_on(monkeypatch, pass_env):
    _, evs = pass_env

    def boom(limit=None):
        raise RuntimeError("kaput")
    monkeypatch.setattr(sync, "replay", boom)
    _pass(monkeypatch)
    assert any(k == "error" and "sync: kaput" in d["message"] for k, d in evs)


def test_dispatcher_replay_stop_on_error_is_logged(monkeypatch, pass_env):
    _, evs = pass_env
    monkeypatch.setattr(sync, "replay", lambda limit=None: {"stopped_because": "error", "error": "entry 1 broke"})
    _pass(monkeypatch)
    assert ("error", {"message": "sync: entry 1 broke"}) in evs


def test_paused_dispatcher_does_not_replay(monkeypatch, pass_env):
    calls, _ = pass_env
    monkeypatch.setattr(dispatch, "paused", lambda: {"since": "2026-10-24T00:00:00"})
    monkeypatch.setattr(sync, "replay", lambda limit=None: calls.append(limit))
    _pass(monkeypatch)
    assert calls == []


def test_no_sync_setting_no_replay(monkeypatch, pass_env):
    calls, _ = pass_env
    C.SYNC.pop("github")
    monkeypatch.setattr(sync, "replay", lambda limit=None: calls.append(limit))
    _pass(monkeypatch)
    assert calls == []
