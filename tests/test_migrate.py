"""The board copy engine: same columns and cards on another board, resumable, with a dry run."""
import argparse
import itertools
import json
import sys

import pytest
import tomlkit

from pl import agents, boardcheck, migrate, trackers
from pl import config as C


class Board:
    """An in-memory tracker that speaks the pl protocol."""

    def __init__(self, name="a"):
        self.name = name
        self._cols = {}
        self._cards = {}
        self._n = itertools.count(1)
        self.clock = "2026-01-01T00:00:00+00:00"

    def columns(self):
        return dict(self._cols)

    def cards(self, query=None):
        return [dict(c) for c in self._cards.values()]

    def card(self, item_id):
        return dict(self._cards[item_id])

    def ensure_column(self, name, *, description=""):
        return self._cols.setdefault(name, f"col-{len(self._cols)}")

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        cid = f"{self.name}-{next(self._n)}"
        self._cards[cid] = {"id": cid, "title": title, "description": description, "tags": list(tags),
                            "metadata": dict(metadata or {}), "list_id": self._cols[column],
                            "updated_at": self.clock, "assigned_to": assigned_to}
        return dict(self._cards[cid])

    def update(self, item_id, *, verify=True, **fields):
        card = self._cards[item_id]
        for k, v in fields.items():
            card[k] = {**card["metadata"], **v} if k == "metadata" else v
        card["updated_at"] = self.clock
        return dict(card)

    def delete(self, item_id):
        del self._cards[item_id]


def seeded(n=50):
    src = Board("src")
    for title in ("Idea", "Approved", "Done"):
        src.ensure_column(title)
    for i in range(n):
        src.create(("Idea", "Approved", "Done")[i % 3], title=f"card {i}", description=f"body {i}",
                   tags=["t", f"n{i}"], metadata={"k": i, "nested": {"a": [i]}}, assigned_to="me@x" if i % 2 else None)
    return src


def run(src, dst, tmp_path, **kw):
    return migrate.copy(src, dst, state_path=tmp_path / "map.json", dry=kw.pop("dry", False),
                        progress=kw.pop("progress", lambda line: None), **kw)


def test_copy_makes_the_same_columns_and_50_cards_with_identical_metadata(tmp_path):
    src, dst = seeded(), Board("dst")
    run(src, dst, tmp_path)
    assert list(dst.columns()) == list(src.columns())
    assert len(dst.cards()) == 50
    mapping = {c["metadata"]["migrated_from"]["id"]: c["id"] for c in dst.cards()}
    assert migrate.verify(src, dst, mapping) == []
    for c in dst.cards():
        assert c["metadata"]["migrated_from"]["updated_at"] == src.clock


def test_a_copy_that_fails_halfway_resumes_without_duplicates(tmp_path):
    src, dst = seeded(), Board("dst")
    real = dst.create
    seen = itertools.count(1)

    def flaky(*a, **kw):
        if next(seen) == 23:
            raise RuntimeError("board went away")
        return real(*a, **kw)
    dst.create = flaky
    with pytest.raises(RuntimeError):
        run(src, dst, tmp_path)
    assert len(dst.cards()) == 22
    first = next(iter(src._cards))
    src.clock = "2026-02-01T00:00:00+00:00"
    src.update(first, title="edited")
    run(src, dst, tmp_path)
    assert len(dst.cards()) == 50
    assert len({c["metadata"]["migrated_from"]["id"] for c in dst.cards()}) == 50
    assert [c["title"] for c in dst.cards() if c["metadata"]["migrated_from"]["id"] == first] == ["edited"]


def test_resume_works_without_the_map_file(tmp_path):
    src, dst = seeded(10), Board("dst")
    run(src, dst, tmp_path)
    (tmp_path / "map.json").unlink()
    run(src, dst, tmp_path)
    assert len(dst.cards()) == 10


def test_dry_run_prints_the_plan_and_calls_no_write(tmp_path):
    src, dst = seeded(4), Board("dst")

    def no_write(*a, **kw):
        raise AssertionError("a dry run wrote to the target")
    dst.create = dst.update = dst.delete = dst.ensure_column = no_write
    lines = []
    plan = run(src, dst, tmp_path, dry=True, progress=lines.append)
    assert sum(x.startswith("would create column") for x in lines) == 3
    assert sum(x.startswith("would copy card") for x in lines) == 4
    assert len(plan["cards"]) == 4
    assert not (tmp_path / "map.json").exists()


def test_verify_names_a_dropped_metadata_key(tmp_path):
    src, dst = seeded(3), Board("dst")
    run(src, dst, tmp_path)
    victim = next(iter(dst._cards.values()))
    del victim["metadata"]["k"]
    mapping = {c["metadata"]["migrated_from"]["id"]: c["id"] for c in dst.cards()}
    out = migrate.verify(src, dst, mapping)
    assert len(out) == 1 and victim["id"] in out[0] and "metadata.k" in out[0]


# --- pl board migrate: target resolution and the switch ---

OLD = '[tracker]\ntype = "local"\n'


@pytest.fixture
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    d = tmp_path / ".pl-mig"
    d.mkdir()
    (d / "config.toml").write_text(OLD)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load(config_dir=str(d))
    yield d
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def boards(profile, monkeypatch):
    src, dst = seeded(50), Board("dst")
    monkeypatch.setattr(trackers, "get", lambda kind: src)
    monkeypatch.setattr(migrate, "_build", lambda cfg: dst)
    monkeypatch.setattr(agents, "registry", lambda: {})
    monkeypatch.setattr(boardcheck, "check", lambda t: [{"id": "R1", "text": "ok", "ok": True, "reason": None}])
    return src, dst


def args(**kw):
    base = dict(action="migrate", to="local", board=None, mcp_config=None, token_env=None, yes=True, dry_run=False, json=False, skip_check=False)
    return argparse.Namespace(**{**base, **kw})


def tracker_table(profile):
    return tomlkit.parse((profile / "config.toml").read_text())["tracker"]


def test_migrate_yes_switches_tracker_and_backs_up_config(boards, profile, capsys):
    src, dst = boards
    migrate.cmd_migrate(args(to="http://127.0.0.1:9000/mcp"))
    assert len(dst.cards()) == 50
    doc = tomlkit.parse((profile / "config.toml").read_text())
    assert doc["tracker"]["type"] == "mcp" and doc["tracker"]["protocol"] == "pl"
    assert doc["tracker_previous"]["type"] == "local"
    (bak,) = profile.glob("config.toml.bak-*")
    assert bak.read_text() == OLD and bak.stat().st_mode & 0o777 == 0o600
    assert "[tracker] now points at http://127.0.0.1:9000/mcp; the old board is unchanged" in capsys.readouterr().out
    assert len(src.cards()) == 50


def test_a_failed_verify_keeps_the_old_tracker(boards, profile, monkeypatch):
    monkeypatch.setattr(migrate, "verify", lambda s, d, m: ["x -> y: title differs"])
    with pytest.raises(SystemExit) as e:
        migrate.cmd_migrate(args())
    assert e.value.code == 1
    assert (profile / "config.toml").read_text() == OLD
    assert not list(profile.glob("config.toml.bak-*"))


def test_migrate_refuses_while_an_agent_is_live(boards, profile, monkeypatch, capsys):
    src, dst = boards
    first = next(iter(src._cards.values()))
    first["metadata"]["worker"] = {"harness": "claude", "stage": "run"}
    monkeypatch.setattr(agents, "worker_status", lambda w, reg: ("alive", None))
    with pytest.raises(SystemExit) as e:
        migrate.cmd_migrate(args())
    assert e.value.code == 1
    assert "wait for these agents or pl restart them" in capsys.readouterr().err
    assert dst.cards() == [] and (profile / "config.toml").read_text() == OLD


def test_migrate_without_yes_and_no_tty_exits_2(boards, profile, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        migrate.cmd_migrate(args(yes=False))
    assert e.value.code == 2 and "--yes" in capsys.readouterr().err
    assert boards[1].cards() == []


def test_migrate_asks_on_a_terminal_and_n_copies_nothing(boards, profile, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    with pytest.raises(SystemExit):
        migrate.cmd_migrate(args(yes=False))
    assert asked == ["Copy 50 cards to local and switch? [y/N]: "]
    assert boards[1].cards() == [] and (profile / "config.toml").read_text() == OLD


def test_migrate_json_shape(boards, profile, capsys):
    migrate.cmd_migrate(args(json=True))
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"columns_created", "cards_copied", "cards_refreshed", "switched", "state_file"}
    assert out["cards_copied"] == 50 and out["columns_created"] == 3 and out["switched"] is True


def test_dry_run_writes_nothing_and_does_not_switch(boards, profile, capsys):
    migrate.cmd_migrate(args(dry_run=True, yes=False))
    assert boards[1].cards() == [] and (profile / "config.toml").read_text() == OLD
    assert "would copy card" in capsys.readouterr().out


def test_target_cfg_forms(profile, tmp_path, monkeypatch):
    assert migrate.target_cfg("local", args()) == {"type": "local"}
    cfg = migrate.target_cfg("https://hub.example/mcp", args(token_env="HUB_TOKEN", board="eng"))
    assert cfg == {"type": "mcp", "protocol": "pl", "board_id": "eng",
                   "server": {"url": "https://hub.example/mcp", "headers": {"Authorization": "Bearer ${HUB_TOKEN}"}}}
    with pytest.raises(SystemExit):
        migrate.target_cfg("https://hub.example/mcp", args())
    assert migrate.target_cfg("http://localhost:1/mcp", args())["server"] == {"url": "http://localhost:1/mcp"}
    named = migrate.target_cfg("hub", args(mcp_config=str(tmp_path / "m.json")))
    assert named == {"type": "mcp", "protocol": "pl", "mcp_config": str(tmp_path / "m.json"), "server": "hub"}
    with pytest.raises(SystemExit):
        migrate.target_cfg("hub", args())


FAILING = [{"id": "R1", "text": "a card round-trips", "ok": True, "reason": None},
           {"id": "R4", "text": "metadata survives", "ok": False, "reason": "dropped k"}]


def test_a_target_that_fails_board_check_copies_nothing(boards, profile, monkeypatch, capsys):
    monkeypatch.setattr(boardcheck, "check", lambda t: FAILING)
    with pytest.raises(SystemExit) as e:
        migrate.cmd_migrate(args())
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "FAIL R4 metadata survives: dropped k" in err and "PASS R1" not in err
    assert boards[1].cards() == [] and boards[1].columns() == {}
    assert (profile / "config.toml").read_text() == OLD


def test_the_lossy_server_from_board_check_stops_the_migration(profile, tmp_path, monkeypatch, capsys):
    from test_tracker_mcp import PROTO_SERVER
    script = tmp_path / "proto_board_server.py"
    script.write_text(PROTO_SERVER)
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"lossy": {
        "command": sys.executable, "args": [str(script)], "env": {"LOSSY": "1"}}}}))
    monkeypatch.setattr(trackers, "get", lambda kind: seeded(3))
    monkeypatch.setattr(agents, "registry", lambda: {})
    from pl.trackers import mcp
    monkeypatch.setattr(mcp, "_secrets", set())   # the server's env values ("1") would mask later tests' output
    with pytest.raises(SystemExit) as e:
        migrate.cmd_migrate(args(to="lossy", mcp_config=str(mcp_config), board="b-1"))
    assert e.value.code == 1
    assert "FAIL R4" in capsys.readouterr().err
    assert (profile / "config.toml").read_text() == OLD


def test_skip_check_copies_without_running_the_rules(boards, profile, monkeypatch):
    def not_called(t):
        raise AssertionError("the check ran")
    monkeypatch.setattr(boardcheck, "check", not_called)
    migrate.cmd_migrate(args(skip_check=True))
    assert len(boards[1].cards()) == 50


def test_dry_run_prints_the_check_results_with_the_plan(boards, profile, monkeypatch, capsys):
    monkeypatch.setattr(boardcheck, "check", lambda t: FAILING)
    migrate.cmd_migrate(args(dry_run=True, yes=False))
    out = capsys.readouterr().out
    assert "PASS R1 a card round-trips" in out and "FAIL R4 metadata survives: dropped k" in out
    assert "would copy card" in out and boards[1].cards() == []
