import json

import pytest

from pl import cli, dispatch, outbox, sync, trackers
from pl import config as C
from pl.trackers.github import RateLimited
from pl.trackers.local import Local


class FakeGitHub:
    def __init__(self, fail=False):
        self.fail = fail

    def columns(self):
        return {"Inbox": "o1", "Approved": "o2"}

    def cards(self, query=None):
        if self.fail:
            raise RateLimited(0, "rate limit")
        return [
            {"id": "o/r#7", "item_id": "PVTI_7", "title": "seven", "description": "d7", "tags": ["a", "b"],
             "metadata": {"worker": {"pane": "%1"}, "pr_urls": ["https://github.com/o/r/pull/9"], "pipeline_mode": "auto"},
             "list_id": "o2", "assigned_to": "erick"},
            {"id": "o/r#8", "item_id": "PVTI_8", "title": "eight", "description": "", "tags": [], "metadata": {},
             "list_id": "o1", "assigned_to": None},
            {"id": "o/r#9", "item_id": "PVTI_9", "title": "nine", "description": "d9", "tags": ["c"], "metadata": {"x": 1},
             "list_id": "o2", "assigned_to": None},
        ]


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "o/projects/5"\n')
    C.load("t")
    trackers.reset()
    monkeypatch.setattr(sync, "_github", lambda: FakeGitHub())
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: False)
    yield d
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", *argv])
    cli.main()


def local_cards():
    return Local(C.TRACKER).cards()


def init(monkeypatch, *extra):
    run(monkeypatch, "sync", "init", "--from-github", *extra)


def fails(monkeypatch, capsys, message, *argv):
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, *argv)
    assert exc.value.code not in (0, None) or message in str(exc.value)
    assert message in capsys.readouterr().err + str(exc.value)


def test_local_id_resolves_card_id_and_survives_replay_state():
    sync._save({"ids": {"L1": "o/r#1"}, "items": {"L1": "PVTI_1"}, "last_sent": 0})
    assert sync.local_id(" o/r#1 ") == "L1"
    assert sync.local_id("o/r#2") is None
    assert sync._state()["items"] == {"L1": "PVTI_1"}


def test_local_id_unreadable_file_is_none():
    sync._ids_path().write_text("{not json")
    assert sync.local_id("o/r#1") is None


def test_init_copies_cards_columns_and_metadata(monkeypatch):
    init(monkeypatch)
    local = Local(C.TRACKER)
    assert {"Inbox", "Approved"} <= set(local.columns())
    names = {v: k for k, v in local.columns().items()}
    got = {c["title"]: c for c in local.cards()}
    assert set(got) == {"seven", "eight", "nine"}
    seven = got["seven"]
    assert names[seven["list_id"]] == "Approved"
    assert (seven["description"], seven["tags"], seven["assigned_to"]) == ("d7", ["a", "b"], "erick")
    assert seven["metadata"] == FakeGitHub().cards()[0]["metadata"]
    assert names[got["eight"]["list_id"]] == "Inbox"


def test_init_writes_id_map_and_prints_summary(monkeypatch, capsys):
    init(monkeypatch)
    assert "imported 3 cards into 2 columns from o/projects/5" in capsys.readouterr().out
    state = sync._state()
    by_title = {c["title"]: c["id"] for c in local_cards()}
    assert state["ids"][by_title["seven"]] == "o/r#7"
    assert state["items"][by_title["seven"]] == "PVTI_7"
    assert len(state["ids"]) == 3
    assert (sync._ids_path().stat().st_mode & 0o777) == 0o600


def test_init_queues_nothing(monkeypatch, capsys):
    init(monkeypatch)
    capsys.readouterr()
    assert outbox.count() == 0
    run(monkeypatch, "sync", "status")
    assert "0 changes queued" in capsys.readouterr().out


def test_init_refuses_non_empty_board(monkeypatch, capsys):
    Local(C.TRACKER).create("Inbox", title="already here")
    fails(monkeypatch, capsys, "pl sync init: the local board is not empty", "sync", "init", "--from-github")
    assert [c["title"] for c in local_cards()] == ["already here"]
    assert not sync._ids_path().exists()


def test_init_refuses_existing_ids_file(monkeypatch, capsys):
    sync._save({"ids": {}, "last_sent": 0})
    fails(monkeypatch, capsys, "pl sync init: sync-ids.json already exists", "sync", "init", "--from-github")
    assert local_cards() == []


def test_init_needs_sync_github(monkeypatch, capsys, profile):
    (profile / "config.toml").write_text('[tracker]\ntype = "local"\n')
    C.load("t")
    fails(monkeypatch, capsys, "pl sync: set [sync] github in config.toml", "sync", "init", "--from-github")


def test_init_needs_local_tracker(monkeypatch, capsys, profile):
    (profile / "config.toml").write_text('[tracker]\ntype = "github-project"\nowner = "o"\nnumber = "5"\n\n[sync]\ngithub = "o/projects/5"\n')
    C.load("t")
    fails(monkeypatch, capsys, 'pl sync init: needs [tracker] type = "local"', "sync", "init", "--from-github")


def test_init_needs_from_github(monkeypatch, capsys):
    fails(monkeypatch, capsys, "pl sync init: say where from: --from-github", "sync", "init")


def test_init_github_failure_leaves_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sync, "_github", lambda: FakeGitHub(fail=True))
    fails(monkeypatch, capsys, "nothing was imported", "sync", "init", "--from-github")
    assert local_cards() == []
    assert not sync._ids_path().exists()
    monkeypatch.setattr(sync, "_github", lambda: FakeGitHub())
    init(monkeypatch)
    assert len(local_cards()) == 3


def test_init_refuses_while_dispatcher_runs_unpaused(monkeypatch, capsys):
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: True)
    monkeypatch.setattr(dispatch, "paused", lambda: None)
    fails(monkeypatch, capsys, "pl sync init: the dispatcher is running; run pl pause first", "sync", "init", "--from-github")
    assert local_cards() == []


def test_init_allowed_while_dispatcher_paused(monkeypatch):
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: True)
    monkeypatch.setattr(dispatch, "paused", lambda: {"at": "now"})
    init(monkeypatch)
    assert len(local_cards()) == 3


def test_dry_run_writes_nothing(monkeypatch, capsys):
    init(monkeypatch, "--dry-run")
    assert "would import 3 cards into 2 columns from o/projects/5" in capsys.readouterr().out
    assert local_cards() == []
    assert not sync._ids_path().exists()


def test_card_by_github_id_resolves_through_map(monkeypatch, capsys):
    init(monkeypatch)
    capsys.readouterr()
    run(monkeypatch, "card", "o/r#9")
    assert "nine" in capsys.readouterr().out


def test_move_and_section_by_github_id(monkeypatch, capsys):
    init(monkeypatch)
    capsys.readouterr()
    run(monkeypatch, "move", "o/r#8", "Approved")
    got = {c["title"]: c for c in local_cards()}
    assert got["eight"]["list_id"] == Local(C.TRACKER).columns()["Approved"]
    t = Local(C.TRACKER)
    t.update(got["eight"]["id"], description="# PIPELINE: SPEC\nbody text\n")
    capsys.readouterr()
    run(monkeypatch, "section", "o/r#8", "SPEC")
    assert "body text" in capsys.readouterr().out


def test_unknown_github_id_still_errors(monkeypatch, capsys):
    init(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, "card", "o/r#99")
    assert "0 cards match" in str(exc.value) + capsys.readouterr().err


def test_pr_move_after_import_stays_local(monkeypatch):
    init(monkeypatch)
    monkeypatch.setattr(sync, "_github", lambda: pytest.fail("GitHub was called"))
    run(monkeypatch, "move", "o/r#8", "Approved", "--pr", "https://github.com/o/r/pull/1")
    card = next(c for c in local_cards() if c["title"] == "eight")
    assert card["metadata"]["pr_urls"] == ["https://github.com/o/r/pull/1"]
    ops = [(e["op"], sorted(e["fields"])) for e in outbox.entries()]
    assert ops.count(("update", ["column"])) + ops.count(("update", ["metadata"])) >= 1
    assert len([e for e in outbox.entries() if "column" in e["fields"]]) == 1
    json.dumps(ops)
