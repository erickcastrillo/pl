import json
from types import SimpleNamespace

import pytest

from pl import config as C
from pl import worktrees


def card(name, column, worker=None):
    return {"id": f"o/r#{name}", "title": name, "list_id": column, "metadata": {"spec_slug": name, "worker": worker}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    monkeypatch.setattr(C, "WORKTREES_DIR", root)
    state = {"cards": [], "dirty": set(), "unpushed": set(), "calls": [], "alive": False}

    def make(folder):
        (root / folder).mkdir()
        (root / folder / ".git").write_text("gitdir: x")
        (root / folder / "f").write_text("x" * 100)

    def git(argv):
        state["calls"].append(argv)
        d = argv[1] if argv[0] == "-C" else None
        name = d.rsplit("/", 1)[-1] if d else None
        if argv[2:4] == ["status", "--porcelain"]:
            return state.get("status_rc", 0), " M f\n" if name in state["dirty"] else ""
        if argv[2:3] == ["log"]:
            return state.get("log_rc", 0), "abc commit\n" if name in state["unpushed"] else ""
        if argv[2:3] == ["rev-parse"]:
            return 0, str(root / "common.git") + "\n"
        return 0, ""

    def run(argv, cwd=None, **kw):
        rc, out = git(["-C", cwd, *argv[1:]] if argv[1] != "--git-dir" else argv[1:])
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(worktrees, "_run", run)
    monkeypatch.setattr(C, "WORK_DIR", None)
    monkeypatch.setattr(worktrees, "cards", lambda: state["cards"])
    monkeypatch.setattr(worktrees, "col_name", lambda cid: cid)
    monkeypatch.setattr(worktrees, "registry", lambda: {})
    monkeypatch.setattr(worktrees, "worker_status", lambda w, reg: ("alive" if state["alive"] else "dead", None))
    state["make"] = make
    return state


def by_name(rows):
    return {r["name"]: r for r in rows}


def test_inventory_lists_foreign_worktrees_as_no_card(env):
    env["make"]("scratch")
    (worktrees.C.WORKTREES_DIR / "plain-file").write_text("x")
    rows = worktrees.inventory()
    assert [(r["name"], r["action"], r["reason"]) for r in rows] == [("scratch", "keep", "no card on this board")]


def test_inventory_keeps_uncommitted_and_unpushed(env):
    env["make"]("r-pl-dirty"); env["make"]("r-pl-ahead")
    env["dirty"].add("r-pl-dirty"); env["unpushed"].add("r-pl-ahead")
    env["cards"] = [card("dirty", "Done"), card("ahead", "Done")]
    rows = by_name(worktrees.inventory())
    assert (rows["r-pl-dirty"]["action"], rows["r-pl-dirty"]["reason"]) == ("keep", "uncommitted changes")
    assert (rows["r-pl-ahead"]["action"], rows["r-pl-ahead"]["reason"]) == ("keep", "unpushed commits")


def test_inventory_keeps_active_cards_and_live_agents(env):
    env["make"]("r-pl-busy"); env["make"]("r-pl-pr"); env["make"]("r-pl-fin")
    env["cards"] = [card("busy", "In progress"), card("pr", "PR open"), card("fin", "Done", worker={"stage": "run"})]
    env["alive"] = True
    rows = by_name(worktrees.inventory())
    assert [rows[n]["reason"] for n in ("r-pl-busy", "r-pl-pr", "r-pl-fin")] == ["card In progress", "card PR open", "agent running"]
    assert {r["action"] for r in rows.values()} == {"keep"}


def test_inventory_marks_clean_done_for_removal(env):
    env["make"]("r-pl-fin")
    env["cards"] = [card("fin", "Done")]
    assert worktrees.inventory()[0]["action"] == "remove"


def test_no_card_is_kept_by_report_and_gc(env):
    env["make"]("r-pl-gone")
    row = worktrees.inventory()[0]
    assert (row["action"], row["reason"], row["card"]) == ("keep", "no card on this board", None)
    assert not any("status" in c for c in env["calls"])
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    assert not any("remove" in c for c in env["calls"])


def test_status_failure_is_cannot_inspect_never_remove(env):
    env["make"]("r-pl-fin")
    env["cards"] = [card("fin", "Done")]
    env["status_rc"] = 128
    row = worktrees.inventory()[0]
    assert (row["action"], row["reason"]) == ("keep", "cannot inspect")


def test_log_failure_is_cannot_inspect(env):
    env["make"]("r-pl-fin")
    env["cards"] = [card("fin", "Done")]
    env["log_rc"] = 128
    assert worktrees.inventory()[0]["reason"] == "cannot inspect"


def test_stale_folder_with_git_file_is_state_unknown_not_remove(env):
    env["make"]("r-pl-fin.stale-20261101-120000")
    env["cards"] = [card("fin", "Done")]
    row = worktrees.inventory()[0]
    assert (row["action"], row["reason"], row["stale"], row["card"]) == ("keep", "stale, state unknown", True, "o/r#fin")
    assert not any("status" in c for c in env["calls"])


def test_plain_stale_folder_is_listed(env):
    d = C.WORKTREES_DIR / "r-pl-x.stale-20261101-120000-2"
    d.mkdir()
    (d / "f").write_text("x" * 10)
    row = worktrees.inventory()[0]
    assert (row["stale"], row["size"], row["card"]) == (True, 10, None)


def test_symlink_is_not_listed(env, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (C.WORKTREES_DIR / "r-pl-link.stale-20261101-120000").symlink_to(target)
    assert worktrees.inventory() == []


def test_size_survives_a_file_that_vanishes(env, monkeypatch):
    env["make"]("r-pl-fin")
    real = worktrees.os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("/f"):
            raise FileNotFoundError(p)
        return real(p, *a, **k)
    monkeypatch.setattr(worktrees.os, "lstat", lstat)
    assert worktrees._size(C.WORKTREES_DIR / "r-pl-fin") == 9


def test_inventory_without_sizes_does_not_walk(env, monkeypatch):
    env["make"]("r-pl-fin")
    monkeypatch.setattr(worktrees, "_size", lambda p: pytest.fail("walked"))
    assert "size" not in worktrees.inventory(sizes=False)[0]


def test_list_shows_size_and_total_line(env, capsys):
    env["make"]("r-pl-fin"); env["make"]("r-pl-busy")
    env["cards"] = [card("fin", "Done"), card("busy", "In progress")]
    worktrees.cmd_worktrees(SimpleNamespace(action="list", yes=False, json=False))
    out = capsys.readouterr().out
    assert "109 B" in out
    assert "2 folders, 218 B, 109 B can be removed" in out.splitlines()[-1]


def test_list_json_has_rows_and_totals_in_bytes(env, capsys):
    env["make"]("r-pl-fin"); env["make"]("r-pl-busy")
    env["cards"] = [card("fin", "Done"), card("busy", "In progress")]
    worktrees.cmd_worktrees(SimpleNamespace(action="list", yes=False, json=True))
    doc = json.loads(capsys.readouterr().out)
    assert doc["total"] == {"folders": 2, "size": 218, "removable": 109}
    assert {r["name"]: r["size"] for r in doc["rows"]} == {"r-pl-fin": 109, "r-pl-busy": 109}


def test_gc_json_is_still_a_list(env, capsys):
    env["make"]("r-pl-fin")
    env["cards"] = [card("fin", "Done")]
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=False, json=True))
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and rows[0]["size"] == 109


def test_empty_folder_prints_zero_total(env, capsys):
    worktrees.cmd_worktrees(SimpleNamespace(action="list", yes=False, json=False))
    assert capsys.readouterr().out.strip() == "0 folders, 0 B, 0 B can be removed"


def test_bare_worktrees_command_lists():
    from pl import cli
    a = cli.build_parser().parse_args(["worktrees"])
    assert a.action == "list"



def test_worktrees_list_output(env, capsys):
    env["make"]("r-pl-fin"); env["make"]("r-pl-busy")
    env["cards"] = [card("fin", "Done"), card("busy", "In progress")]
    worktrees.cmd_worktrees(SimpleNamespace(action="list", yes=False, json=False))
    out = capsys.readouterr().out
    assert "r-pl-fin" in out and "o/r#fin" in out and "Done" in out and "remove" in out
    assert "keep: card In progress" in out


def test_worktrees_gc_dry_run_and_json(env, capsys):
    env["make"]("r-pl-fin")
    env["cards"] = [card("fin", "Done")]
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=False, json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "r-pl-fin" and rows[0]["action"] == "remove"
    assert not any("remove" in c for c in env["calls"])


def test_worktrees_gc_yes_removes_and_reports_space(env, capsys, monkeypatch):
    env["make"]("r-pl-fin"); env["make"]("r-pl-busy"); env["make"]("r-pl-bad")
    env["cards"] = [card("fin", "Done"), card("busy", "In progress"), card("bad", "Done")]
    real = worktrees._run

    def flaky(argv, **kw):
        if "remove" in argv and argv[-1].endswith("r-pl-bad"):
            return SimpleNamespace(returncode=1, stdout="locked", stderr="")
        return real(argv, **kw)
    monkeypatch.setattr(worktrees, "_run", flaky)
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    removed = [c[-1] for c in env["calls"] if "remove" in c]
    assert removed == [str(C.WORKTREES_DIR / "r-pl-fin")]
    assert any("prune" in c for c in env["calls"])
    out = capsys.readouterr().out
    assert "removed 1" in out and "kept 1" in out and "failed 1" in out and "freed" in out


def test_gc_yes_keeps_live_agent_worktree_in_any_column(env, capsys):
    env["alive"] = True
    for name, column in (("busy", "In progress"), ("fin", "Done")):
        env["make"](f"r-pl-{name}")
    env["cards"] = [card("busy", "In progress", {"pane": "%1"}), card("fin", "Done", {"pane": "%2"})]
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    assert not any("remove" in c for c in env["calls"])
    assert all(r["reason"] == "agent running" for r in worktrees.inventory())
    assert capsys.readouterr().out.count("removed 0, kept 2") == 1


def test_gc_keeps_held_agent_worktree(env):
    env["make"]("r-pl-wait")
    env["cards"] = [card("wait", "In progress", {"trust_wait": "/x"})]
    row = by_name(worktrees.inventory())["r-pl-wait"]
    assert (row["action"], row["reason"]) == ("keep", "agent held")


def test_gc_yes_writes_worktree_cleanup_event(env, tmp_path, monkeypatch):
    from pl import events
    C.STATE_DIR = tmp_path / "state"
    env["make"]("r-pl-fin"); env["make"]("r-pl-bad")
    env["cards"] = [card("fin", "Done"), card("bad", "Done")]
    real = worktrees._run
    monkeypatch.setattr(worktrees, "_run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="locked", stderr="")
                        if "remove" in argv and argv[-1].endswith("r-pl-bad") else real(argv, **kw))
    worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    found = [e for e in events._read() if e["kind"] == "worktree_cleanup"]
    assert [(e["card"], e["by"]) for e in found] == [("o/r#fin", "gc")]
    assert found[0]["path"] == str(C.WORKTREES_DIR / "r-pl-fin")
