"""Plan 54 slice 2: pl applies the writes a finished stage agent claimed but did not land."""
import json
import types
from pathlib import Path

import pytest
from test_run_release import fake  # noqa: F401 - the dispatcher fixture

from pl import agent_alerts, agent_screen, agents, alerts, board, commands, dispatch, events, move_agent, reconcile, worktrees
from pl import config as C
from pl.trackers import github

PR = "https://github.com/o/r/pull/9"
REAL_CHECK = agent_alerts.check   # conftest fakes it for every other test


def _card(cid="o/r#7", col="In progress", worker=True, desc="body", **meta):
    m = {"pipeline_mode": "auto", **meta}
    if worker:
        m["worker"] = {"stage": {"In progress": "run", "Approved": "run", "PR open": "run"}.get(col, "plan"),
                       "pane": "%1", "window": "w", "session_id": "s1"}
    return {"id": cid, "title": "Add widget", "list_id": col, "description": desc, "metadata": m}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.CODE_HOST = {"owner": None, "labels": {"review": "pl:review"}}
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def world(monkeypatch):
    """Fakes for every seam the sweep touches; the knobs are plain attributes."""
    w = types.SimpleNamespace(gh=[], moves=[], updates=[], events=[], sent=[], status="alive", word="working", locked=set(),
                              trees=[], labels=["pl:review"], pr_state="OPEN", commits=[], listed=[PR], edit_error=None,
                              budget=False, now=1_000_000.0)

    def gh(args):
        w.gh.append(args)
        if w.budget:
            raise github.OverBudget(w.now + 600, "test budget")
        if args[:2] == ["pr", "view"]:
            return json.dumps({"state": w.pr_state, "labels": [{"name": n} for n in w.labels], "commits": w.commits})
        if args[:2] == ["pr", "list"]:
            return json.dumps([{"url": u} for u in w.listed])
        if args[:2] == ["pr", "edit"] and w.edit_error:
            raise SystemExit(w.edit_error)
        return ""
    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(commands, "move_to", lambda ref, col, cid=None: w.moves.append((ref, col)))
    monkeypatch.setattr(board, "update", lambda cid, **f: w.updates.append((cid, f)))
    monkeypatch.setattr(board, "col_name", lambda lid: lid)
    monkeypatch.setattr(board, "col_id", lambda name: name)
    monkeypatch.setattr(agents, "worker_status", lambda wk, reg: (w.status, wk.get("session_id")))
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: ("", w.word, ""))
    monkeypatch.setattr(move_agent, "locked", lambda cid: cid in w.locked)
    monkeypatch.setattr(worktrees, "_list_worktrees", lambda d: w.trees)
    monkeypatch.setattr(dispatch, "notify", lambda title, fix="": w.sent.append(title))
    monkeypatch.setattr(events, "emit", lambda kind, cid=None, **d: w.events.append((kind, cid, d)))
    monkeypatch.setattr(alerts, "_clock", lambda: w.now)
    return w


def _edits(w):
    return [a for a in w.gh if a[:2] == ["pr", "edit"]]


def _reconciled(w):
    return [e for e in w.events if e[0] == "reconciled"]


def _keys():
    return sorted(a["key"] for a in alerts.listing())


def test_finished_run_agent_with_pr_moves_card_to_pr_open(world):
    world.word = "finished"
    reconcile.sweep([_card(pr_urls=[PR])], {}, {})
    assert world.moves == [("o/r#7", "PR open")]
    assert _reconciled(world) == [("reconciled", "o/r#7", {"stage": "run", "changes": ["moved to PR open"]})]


def test_closed_window_counts_as_finished(world):
    world.status = "dead"
    reconcile.sweep([_card(pr_urls=[PR])], {}, {})
    assert world.moves == [("o/r#7", "PR open")]


def test_pr_found_by_branch_is_linked(world):
    world.word = "finished"
    world.trees = [{"path": Path("/w/x"), "branch": "pl/add-widget"}]
    reconcile.sweep([_card()], {}, {})
    assert ("o/r#7", {"metadata": {"pr_urls": [PR]}}) in world.updates
    assert _reconciled(world)[0][2]["changes"] == [f"linked PR {PR}", "moved to PR open"]
    assert ["pr", "list", "--repo", "o/r", "--head", "pl/add-widget", "--state", "open", "--json", "url", "--limit", "1"] in world.gh


def test_missing_review_label_is_added_and_rework_removed(world):
    world.word, world.labels = "finished", ["pl:needs-rework"]
    reconcile.sweep([_card(col="PR open", pr_urls=[PR])], {}, {})
    assert _edits(world) == [["pr", "edit", PR, "--add-label", "pl:review", "--remove-label", "pl:needs-rework"]]
    assert _reconciled(world)[0][2]["changes"] == ["added label pl:review", "removed label pl:needs-rework"]


def test_rework_run_without_new_commits_keeps_labels(world):
    world.word, world.labels = "finished", ["pl:needs-rework"]
    world.commits = [{"committedDate": "2026-10-26T10:00:00Z"}]
    reconcile.sweep([_card(col="PR open", pr_urls=[PR], rework={"at": "2026-10-26T12:00:00+00:00"})], {}, {})
    assert _edits(world) == [] and _reconciled(world) == []


def test_plan_section_moves_card_to_plan_for_review(world):
    world.word = "finished"
    reconcile.sweep([_card(col="Spec ready", desc="# PIPELINE: PLAN\nWP1")], {}, {})
    assert world.moves == [("o/r#7", "Plan for review")]
    assert _reconciled(world)[0][2] == {"stage": "plan", "changes": ["moved to Plan for review"]}


def test_no_output_writes_nothing(world):
    world.word, world.listed = "finished", []
    cs = [_card(), _card("o/r#8", col="Spec ready", desc="no section yet")]
    reconcile.sweep(cs, {}, {})
    assert world.moves == [] and world.updates == [] and _edits(world) == []
    assert _reconciled(world) == [] and _keys() == []


def test_failed_label_write_opens_alert_once_then_resolves(world):
    world.word, world.labels, world.edit_error = "finished", [], "gh: boom"
    c = _card(col="PR open", pr_urls=[PR])
    reconcile.sweep([c], {}, {})
    (a,) = alerts.listing()
    assert a["key"] == "reconcile_failed:o/r#7" and f"add label pl:review to {PR}" in a["title"]
    assert len(world.sent) == 1 and _reconciled(world) == []
    world.now += 60
    reconcile.sweep([c], {}, {})
    assert len(world.sent) == 1
    world.labels, world.edit_error = ["pl:review"], None
    reconcile.sweep([c], {}, {})
    assert _keys() == [] and len(world.sent) == 1


def test_failing_github_read_on_one_card_does_not_stop_the_others(world, monkeypatch):
    world.word = "finished"
    real = github._gh

    def gh(args):
        if args[:2] == ["pr", "view"] and args[2] == PR:
            raise SystemExit("gh: not found")
        return real(args)
    monkeypatch.setattr(github, "_gh", gh)
    other = "https://github.com/o/r/pull/10"
    cs = [_card("o/r#1", col="PR open", pr_urls=[PR]), _card("o/r#2", col="In progress", pr_urls=[other])]
    reconcile.sweep(cs, {}, {})
    assert world.moves == [("o/r#2", "PR open")]
    assert _keys() == ["reconcile_failed:o/r#1"] and len(world.sent) == 1


def test_empty_branch_lookup_is_retried_only_after_a_few_minutes(world):
    world.word, world.listed = "finished", []
    world.trees = [{"path": Path("/w/x"), "branch": "pl/add-widget"}]
    st = {}
    c = _card()
    reconcile.sweep([c], {}, st)
    reconcile.sweep([c], {}, st)
    lists = lambda: [a for a in world.gh if a[:2] == ["pr", "list"]]   # noqa: E731
    assert len(lists()) == 1
    world.now += 600
    reconcile.sweep([c], {}, st)
    assert len(lists()) == 2


def test_budget_hold_opens_no_alert(world):
    world.word, world.budget = "finished", True
    st = {}
    reconcile.sweep([_card(col="PR open", pr_urls=[PR])], {}, st)
    assert _keys() == [] and _reconciled(world) == [] and not st.get("reconciled")


def test_second_pass_writes_nothing(world):
    world.word = "finished"
    st = {}
    cs = [_card(pr_urls=[PR])]
    reconcile.sweep(cs, {}, st)
    n = len(world.gh)
    reconcile.sweep(cs, {}, st)
    assert len(_reconciled(world)) == 1 and world.moves == [("o/r#7", "PR open")] and len(world.gh) == n


def test_skips_working_locked_dropped_and_manual_cards(world):
    world.locked = {"o/r#2"}
    cs = [_card("o/r#1", pr_urls=[PR]), _card("o/r#2", pr_urls=[PR]), _card("o/r#3", pr_urls=[PR], dropped_at="x"),
          _card("o/r#4", pr_urls=[PR], pipeline_mode="manual"), _card("o/r#5", pr_urls=[PR], question="which?")]
    reconcile.sweep(cs, {}, {})   # o/r#1 is still working: screen word "working"
    world.word = "finished"
    reconcile.sweep(cs[1:], {}, {})
    assert world.moves == [] and world.updates == [] and world.events == []


def test_reconciled_card_opens_no_unmoved_alert(world, monkeypatch):
    world.word = "finished"
    c = _card(pr_urls=[PR])
    alerts.open("agent_unmoved:o/r#7", "warn", "old", "fix", cards=["o/r#7"])
    monkeypatch.setattr(commands, "move_to", lambda ref, col, cid=None: c.update(list_id=col))
    reconcile.sweep([c], {}, {})
    REAL_CHECK([c], {}, set())
    assert "agent_unmoved:o/r#7" not in _keys()


def test_dispatch_once_calls_reconcile(fake, monkeypatch):  # noqa: F811
    seen = []
    from pl import dispatch
    monkeypatch.setattr(dispatch, "reconcile", reconcile)   # conftest stubs it out of every other dispatcher pass
    monkeypatch.setattr(reconcile, "sweep", lambda *a: seen.append(a) or (_ for _ in ()).throw(RuntimeError("boom")))
    fake.run([])
    assert len(seen) == 1
