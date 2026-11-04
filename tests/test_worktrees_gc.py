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
            return 0, " M f\n" if name in state["dirty"] else ""
        if argv[2:3] == ["log"]:
            return 0, "abc commit\n" if name in state["unpushed"] else ""
        if argv[2:3] == ["rev-parse"]:
            return 0, str(root / "common.git") + "\n"
        return 0, ""

    monkeypatch.setattr(worktrees, "_git", git)
    monkeypatch.setattr(worktrees, "cards", lambda: state["cards"])
    monkeypatch.setattr(worktrees, "col_name", lambda cid: cid)
    monkeypatch.setattr(worktrees, "registry", lambda: {})
    monkeypatch.setattr(worktrees, "worker_status", lambda w, reg: ("alive" if state["alive"] else "dead", None))
    state["make"] = make
    return state


def by_name(rows):
    return {r["name"]: r for r in rows}


def test_inventory_ignores_foreign_worktrees(env):
    env["make"]("scratch")
    (worktrees.C.WORKTREES_DIR / "plain-file").write_text("x")
    assert worktrees.inventory() == []


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


def test_inventory_marks_clean_done_or_orphaned_for_removal(env):
    env["make"]("r-pl-fin"); env["make"]("r-pl-gone")
    env["cards"] = [card("fin", "Done")]
    rows = by_name(worktrees.inventory())
    assert rows["r-pl-fin"]["action"] == rows["r-pl-gone"]["action"] == "remove"
    assert rows["r-pl-gone"]["card"] is None


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


def test_worktrees_gc_yes_removes_and_reports_space(env, capsys):
    env["make"]("r-pl-fin"); env["make"]("r-pl-busy"); env["make"]("r-pl-bad")
    env["cards"] = [card("fin", "Done"), card("busy", "In progress"), card("bad", "Done")]
    real = worktrees._git

    def flaky(argv):
        if "remove" in argv and argv[-1].endswith("r-pl-bad"):
            return 1, "locked"
        return real(argv)
    worktrees._git = flaky
    try:
        worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    finally:
        worktrees._git = real
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


def test_gc_yes_writes_worktree_cleanup_event(env, tmp_path):
    from pl import events
    C.STATE_DIR = tmp_path / "state"
    env["make"]("r-pl-fin"); env["make"]("r-pl-bad")
    env["cards"] = [card("fin", "Done"), card("bad", "Done")]
    real = worktrees._git
    worktrees._git = lambda argv: (1, "locked") if "remove" in argv and argv[-1].endswith("r-pl-bad") else real(argv)
    try:
        worktrees.cmd_worktrees(SimpleNamespace(action="gc", yes=True, json=False))
    finally:
        worktrees._git = real
    found = [e for e in events._read() if e["kind"] == "worktree_cleanup"]
    assert [(e["card"], e["by"]) for e in found] == [("o/r#fin", "gc")]
    assert found[0]["path"] == str(C.WORKTREES_DIR / "r-pl-fin")
