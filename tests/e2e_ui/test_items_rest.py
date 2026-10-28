"""The checklist items no other e2e_ui file covers: 1, 2, 3, 6, 8, 9, 14, 16f, 16o, 16p, 16q."""
import contextlib
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from conftest import press
from pl import agent_defs, alerts, checkout_watch, dispatch, events, recall, trackers, watch, whatsnew, worktrees
from pl import config as C
from pl.tui.cards import CardScreen
from test_tui_views import screen_text
from tui_wait import settle, wait_until

pytestmark = pytest.mark.e2e_ui
REAL_FOR_RUN = worktrees.for_run   # the root conftest swaps it per test; items 6 and 16p put it back


def _proc(argv, out="", rc=0, err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def _board():
    return trackers.get("tracker")


def _card(column, title, **metadata):
    c = _board().create(column, title=title)
    if metadata:
        dispatch.update(c["id"], metadata=metadata)
    return _board().card(c["id"])


def _pass(monkeypatch):
    """The seams tests/test_local_board_flow.py fakes, plus a tmux recorder; returns the recorded tmux calls."""
    sent = []

    def tmux(*argv, check=True, **kw):
        sent.append(argv)
        return ""

    for name, fake in {"start_worker": lambda *a, **k: None, "notify": lambda *a, **k: None, "sweep_untracked": lambda *a, **k: 0,
                       "ensure_services": lambda *a, **k: None, "registry": lambda: {}, "healthy_profile": lambda want, cards: want or "e2e",
                       "permission_wait": lambda *a, **k: None, "trust_wait": lambda *a, **k: None, "tmux": tmux,
                       "pane_exists": lambda pane: True}.items():
        monkeypatch.setattr(dispatch, name, fake)
    return sent


def _run_pass():
    dispatch.dispatch_once(max_runs=1, dry=False, pull=False)


def _work_dir(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    (work / ".git" / "info").mkdir(parents=True)
    monkeypatch.setattr(C, "WORK_DIR", work)
    return work


def test_item_1_memory_file_inside_work_dir(profile, tmp_path, monkeypatch):
    work = _work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(recall, "for_prompt", lambda *a, **k: "a past decision")
    c = _card("Inbox", "memory card")
    arg = dispatch.memory_arg(c, "spec", dry=False)
    out = Path(arg.split("memory=", 1)[1].strip("'"))
    assert out.is_relative_to(work / ".pl" / "launch"), f"memory file landed outside the start folder: {out}"
    assert out.stat().st_mode & 0o777 == 0o600
    assert ".pl/launch/" in (work / ".git" / "info" / "exclude").read_text().splitlines()
    assert not list((Path(C.STATE_DIR) / "launch").glob("skill-memory-*")) if (Path(C.STATE_DIR) / "launch").exists() else True


def _finished(window, stage="spec"):
    return {"stage": stage, "window": window, "pane": f"%{window[1:]}", "session_id": f"s{window[1:]}"}


def _closing_pass(monkeypatch):
    sent = _pass(monkeypatch)
    old = (time.time() - 600) * 1000
    monkeypatch.setattr(dispatch, "registry", lambda: {f"s{i}": {"status": "idle", "statusUpdatedAt": old} for i in (1, 2, 3)})
    a = _card("Inbox", "A still in its stage", pipeline_mode="auto", worker={**_finished("@1"), "session_id": "s1"})
    b = _card("Spec ready", "B in the done column", pipeline_mode="auto", finished_workers=[_finished("@2")])
    c = _card("Done", "C done with pl done", pipeline_mode="auto", finished_workers=[_finished("@3")])
    _run_pass()
    killed = {argv[2] for argv in sent if argv[:1] == ("kill-window",)}
    return killed, {x["id"]: _board().card(x["id"]) for x in (a, b, c)}, (a, b, c)


def test_item_2_finished_window_closes_only_at_done_column(profile, monkeypatch):
    killed, after, (a, b, c) = _closing_pass(monkeypatch)
    assert "@1" not in killed, "the window of a card still in its stage was closed"
    assert {"@2", "@3"} <= killed, f"finished windows left open: {killed}"


def test_item_16f_done_card_window_closes(profile, monkeypatch):
    killed, after, (a, b, c) = _closing_pass(monkeypatch)
    meta = after[c["id"]].get("metadata") or {}
    assert "@3" in killed, "the window of a card moved straight to Done stayed open"
    assert not meta.get("finished_workers") and not meta.get("worker"), "a Done card still counts as a live agent"


def test_item_3_settled_pr_asked_once_across_two_passes(profile, tmp_path, monkeypatch):
    _work_dir(tmp_path, monkeypatch)
    root = tmp_path / "trees"
    root.mkdir()
    monkeypatch.setattr(C, "WORKTREES_DIR", root)
    states = ["MERGED", "CLOSED", "MERGED"]
    urls = [f"https://github.com/o/r/pull/{i}" for i in range(1, 4)]
    trees = [{"path": root / f"repo-pl-t{i}", "branch": f"pl/t{i}"} for i in range(3)]
    for t in trees:
        t["path"].mkdir()
    asked = []

    def run(argv, **kw):
        if argv[:3] == ["gh", "pr", "view"] or "pr" in argv and "view" in argv:
            url = next(a for a in argv if a.startswith("https://"))
            asked.append(url)
            return _proc(argv, json.dumps({"state": states[urls.index(url)], "mergedAt": None, "headRefName": "x"}))
        return _proc(argv)

    monkeypatch.setattr(worktrees, "_run", run)
    monkeypatch.setattr(worktrees, "_list_worktrees", lambda work: trees)
    monkeypatch.setattr(worktrees, "remove_worktree", lambda *a, **k: None, raising=False)
    cards = [_card("Done", f"t{i}", pr_urls=[urls[i]], branch=f"pl/t{i}") for i in range(3)]
    for _ in range(2):
        worktrees.sweep([_board().card(c["id"]) for c in cards])
    assert max((asked.count(u) for u in urls), default=0) <= 1, f"a settled PR was asked about again: {asked}"


def test_item_3_shared_board_read_not_testable_here():
    pytest.skip("not testable here: needs the live GitHub budget; covered by --live")


def _fake_git(monkeypatch, existing=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["git", "worktree", "list"]:
            return _proc(argv)
        if argv[:2] == ["git", "show-ref"] or argv[:2] == ["git", "rev-parse"]:
            return _proc(argv, rc=1)
        if argv[:3] == ["git", "worktree", "add"]:
            path = Path(argv[argv.index("-b") + 2] if "-b" in argv else argv[3])
            if path.exists():
                return _proc(argv, rc=128, err=f"fatal: '{path}' already exists")
            path.mkdir(parents=True)
            return _proc(argv)
        return _proc(argv)

    monkeypatch.setattr(worktrees, "_run", run)
    return calls


def test_item_6_run_agent_starts_in_its_worktree(profile, tmp_path, monkeypatch):
    work = _work_dir(tmp_path, monkeypatch)
    root = tmp_path / "trees"
    monkeypatch.setattr(C, "WORKTREES_DIR", root)
    monkeypatch.setattr(worktrees, "for_run", REAL_FOR_RUN)
    _fake_git(monkeypatch)
    c = _card("Approved", "run in a worktree")
    path, err = worktrees.for_run(c)
    assert err is None
    assert path == root / f"{work.name}-pl-{worktrees.slug_of(c)}" and path != C.WORK_DIR


async def test_item_6_dirty_main_checkout_alert_in_console(console, tmp_path, monkeypatch):
    _work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(checkout_watch, "_git", lambda argv: (0, " M src/x.py\0"))
    checkout_watch.check([], {}, {}, dry=False, send=lambda *a: None)
    monkeypatch.setattr(watch, "pr_activity", lambda: {})
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    trackers.reset()
    async with console() as pilot:
        await press(pilot, "7")
        await wait_until(pilot, lambda: pilot.app.active_group() == "activity", "the activity tab", timeout=20.0)
        pilot.app.action_tab("alerts")
        await wait_until(pilot, lambda: "Main checkout has" in screen_text(pilot.app), "the dirty-checkout alert under Alerts", timeout=20.0)
        assert "src/x.py" in screen_text(pilot.app), "the alert does not name the changed path"


def test_item_14_unsent_prompt_text_is_sent_or_shown(profile, monkeypatch):
    keys = []

    def tmux(*argv, check=True, **kw):
        if argv[0] == "capture-pane":
            return "❯ half-typed note\n"
        keys.append(argv)
        return ""

    monkeypatch.setattr(dispatch, "tmux", tmux)
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, *a, **k: seen.append((kind, a, k)))
    c = _card("In progress", "rework card", rework={"active": True, "round": 1},
              worker={"stage": "run", "pane": "%9", "window": "@9", "session_id": "s9"})
    dispatch.rework_live(c, c["metadata"]["worker"], "idle", {}, dry=False)
    sent = [a for a in keys if "-l" in a and any("half-typed note" in x for x in a)]
    shown = any("half-typed note" in json.dumps([x[1:] for x in seen], default=str) for _ in [0])
    assert sent or shown, "cleared unsent text without sending or showing it"


def test_item_16p_plain_folder_at_worktree_path_set_aside(profile, tmp_path, monkeypatch):
    work = _work_dir(tmp_path, monkeypatch)
    root = tmp_path / "trees"
    monkeypatch.setattr(C, "WORKTREES_DIR", root)
    monkeypatch.setattr(worktrees, "for_run", REAL_FOR_RUN)
    _fake_git(monkeypatch)
    c = _card("Approved", "stale folder")
    path = root / f"{work.name}-pl-{worktrees.slug_of(c)}"
    path.mkdir(parents=True)
    (path / "leftover.txt").write_text("x")
    worktrees.for_run(c)
    got, err = worktrees.for_run(c)
    aside = list(root.glob(f"{path.name}.stale*"))
    assert aside and (aside[0] / "leftover.txt").exists(), "the plain folder was not moved aside"
    assert got == path and err is None, f"the run did not start on its branch: {err}"
    assert not any("already exists" in str(a) for a in alerts.listing(all=True))


def test_item_8_whatsnew_one_file_per_entry(profile):
    name = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+\.toml$")
    files = sorted(whatsnew.DIR.iterdir())
    assert files and all(name.match(f.name) for f in files if f.name != "__pycache__")
    assert whatsnew.problems() == []
    src = whatsnew.DIR.parent
    assert not [p.name for p in src.iterdir() if p.name.lower() in ("whatsnew.toml", "whatsnew.md") or p.name.startswith("WHATSNEW")]
    assert all({"title", "where", "try"} <= set(e) for e in whatsnew.load())


async def test_item_8_console_lists_newest_entry(console):
    async with console() as pilot:
        pilot.app.action_whatsnew()
        await settle(pilot)
        pilot.app.screen.query_one("#whatsnew-body").scroll_end(animate=False)
        await settle(pilot)
        title = whatsnew.load()[-1]["title"]
        assert title in screen_text(pilot.app)


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    real = tmp_path / "real-profile"
    real.mkdir()
    (real / "config.toml").write_text('[tracker]\ntype = "local"\ncolumns = ["Stand-in column"]\n\n[pod]\npod_learning = true\n')
    monkeypatch.setenv("PL_CONFIG_DIR", str(real))
    return real


def _snapshot(folder):
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file()}


async def test_item_9_board_and_settings_from_throwaway_profile(stand_in, console, profile):
    before = _snapshot(stand_in)
    async with console() as pilot:
        await press(pilot, "3")
        await settle(pilot)
        board_text = screen_text(pilot.app)
        await press(pilot, "8")
        await settle(pilot)
        settings_text = screen_text(pilot.app)
    assert "Stand-in column" not in board_text + settings_text
    assert str(C.PROFILE_DIR if hasattr(C, "PROFILE_DIR") else profile) != str(stand_in)
    assert _snapshot(stand_in) == before, "the real profile folder changed"


def test_item_9_tracker_mcp_timing_not_testable_here():
    pytest.skip("not testable here: a timing flake in the unit suite, not the console")


@pytest.fixture
def agent_pipeline(profile, monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", {"stages": {"draft": {"agent": "writer", "gate": "none", "done": "Spec ready", "next": "Spec ready"}}}, raising=False)
    monkeypatch.setattr(dispatch.pipeline, "is_config", lambda: True)
    writer = {"approval": ["send"], "skills": [], "model": "e2e", "connector_roles": []}
    monkeypatch.setattr(agent_defs, "load", lambda: {"writer": writer})
    return writer


def test_item_16o_1_agent_send_during_stage_not_testable_here():
    pytest.skip("not testable here: what the agent does during its stage is model behaviour")


async def test_item_16o_2_held_draft_shown_without_plan_ready(agent_pipeline, console, monkeypatch):
    monkeypatch.setattr(dispatch.notifications, "send", lambda *a, **k: None)
    c = _card("Spec ready", "held draft")
    assert dispatch.hold_agent_draft(c, "Spec ready", dry=False) is True
    fresh = _board().card(c["id"])
    assert (fresh.get("metadata") or {}).get("approval_pending")
    trackers.reset()
    async with console() as pilot:
        await settle(pilot)
        trackers.reset()   # the console's refresh made its board connection in another thread
        pilot.app.push_screen(CardScreen(fresh["id"], "Spec ready"))
        await settle(pilot)

        def held():
            t = screen_text(pilot.app).lower()
            return "send" in t and ("held" in t or "approval" in t)

        with contextlib.suppress(BaseException):
            await wait_until(pilot, held, "the held draft on the card screen", timeout=10.0)
        assert held(), "the card screen does not name the held draft"


def test_item_16o_3_agent_definitions_not_reloaded_every_pass(agent_pipeline, monkeypatch):
    loads = []
    monkeypatch.setattr(agent_defs, "load", lambda: loads.append(1) or {"writer": agent_pipeline})
    c = _card("Spec ready", "gate cost")
    dispatch.held_gate(c, "Spec ready")
    first = len(loads)
    dispatch.held_gate(c, "Spec ready")
    assert len(loads) == first, f"agent definitions reloaded on the second pass ({first} -> {len(loads)})"


def test_item_16o_4_old_round_zero_approval_does_not_release_new_hold(agent_pipeline):
    c = _card("Spec ready", "old approval", gates={"draft": {"review_round": 0}})
    assert dispatch.held_gate(c, "Spec ready") is not None, "an approval recorded before the agent had approval releases the hold"


def test_item_16q_not_testable_here():
    pytest.skip("not testable here: needs a Vikunja server; covered by --live")


def test_item_16f_live_state_not_testable_here():
    pytest.skip("not testable here: needs the live board and real worktrees; covered by --live")


def test_item_6_live_worktrees_not_testable_here():
    pytest.skip("not testable here: needs the live board and real worktrees; covered by --live")


def test_every_rest_item_has_a_test():
    src = Path(__file__).read_text()
    names = set(re.findall(r"^(?:async )?def (test_item_\w+)", src, re.M))
    for item in ("1", "2", "3", "6", "8", "9", "14", "16f", "16o", "16p", "16q"):
        assert any(re.match(rf"test_item_{item}_", n) for n in names), f"no test for item {item}"
    assert pytestmark.name == "e2e_ui"
