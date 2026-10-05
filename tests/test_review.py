"""WP10: the spec gate, spec approve / send back, and the review screen for specs and plans."""
import argparse
import copy
import io
import json
import os
import subprocess
from contextlib import redirect_stdout

import pytest
from rich.console import Console
from textual.widgets import TextArea

from pl import commands, dispatch, trackers
from pl import config as C
from pl.board import sections
from pl.tui.app import PlApp
from pl.tui.review import (ConfirmScreen, ReviewScreen, open_questions, plan_checks, review_file, review_text, safe_link,
                           spec_checks, with_answers)

A_ID = "aaaaaaaa-1111-4111-8111-111111111111"
B_ID = "bbbbbbbb-2222-4222-8222-222222222222"
OLD = "2020-01-01T00:00:00+00:00"

SPEC = """## Acceptance criteria
- AC-1 Given a lead, when it replies, then the reply is stored.
- AC-2 Negative: Given a viewer, when it posts, then 403.
- AC-3 Regression: Given the old payload, it still parses.

## Open questions
(none)
"""

PLAN = """# Plan: add a thing

## Minimum change
budget: 2 non-test / 1 test files / ~40 lines

| WP | tier | what |
|----|------|------|
| WP1 | lite | model |
| WP2 | full | api, see [docs](https://example.test/doc) |

## Deliberately not doing
- anything else
"""


def body(**parts):
    return "".join(f"# PIPELINE: {k}\n{v.rstrip()}\n\n" for k, v in parts.items())


class FakeTracker:
    def __init__(self, items):
        self.items = {c["id"]: c for c in items}

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        c = self.items[item_id]
        if "column" in fields:
            fields["list_id"] = self.columns()[fields.pop("column")]
        if "metadata" in fields:
            c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
        c.update(fields)
        c["updated_at"] = "2030-01-01T00:00:00+00:00"
        return copy.deepcopy(c)

    def url(self, item_id):
        return f"https://example.test/{item_id}"


def mk(cid, title, col, desc="", tags=("api",), **meta):
    return {"id": cid, "title": title, "description": desc, "tags": list(tags), "list_id": f"col-{col}",
            "updated_at": OLD, "assigned_to": None, "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


def _accounts(home):
    """The two-account setup these tests were written against (no profile carries defaults since WP14)."""
    C.PROFILES = {"acme": home / ".claude-acme", "acme2": home / ".claude-acme2"}
    C.ACCOUNTS = {n: {"harness": "claude", "config_dir": str(d)} for n, d in C.PROFILES.items()}
    C.PROMPTS = {"spec": "/spec-writer {id}", "design": "/ui-designer {id}",
                 "plan": "/plan-writer {id}", "run": "/loop 5m /run-plan {id}"}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    _accounts(tmp_path)
    monkeypatch.setattr(commands, "notify", lambda *a: None)
    monkeypatch.setattr(dispatch, "notify", lambda *a: None)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def board(monkeypatch):
    fake = FakeTracker([])
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


@pytest.fixture
def started(monkeypatch):
    got = []
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: got.append((c["id"], stage)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cards: want or "acme")
    return got


def events_kinds():
    p = C.STATE_DIR / "events.jsonl"
    return [json.loads(line)["kind"] for line in p.read_text().splitlines()] if p.exists() else []


def args(kw):
    """The command's own arguments, without the screen's feedback hooks."""
    return {k: v for k, v in kw.items() if k not in ("says", "failed", "done")}


def run_cli(fn, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(argparse.Namespace(**kw))
    return out.getvalue()


# ---------- the gate ----------

def test_spec_gate_holds_a_finished_spec_until_approved(board, started, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC))
    dispatch.dispatch_once(1, dry=True, pull=False)
    assert started == []
    run_cli(commands.cmd_approve, id=A_ID, force=False)
    m = board.items[A_ID]["metadata"]
    assert m["spec_approved_at"] and m["spec_approved_by"] == C.USER_EMAIL
    assert board.items[A_ID]["list_id"] == "col-Spec ready"      # approval does not move the card
    assert "spec_approved" in events_kinds()
    dispatch.dispatch_once(1, dry=True, pull=False)
    assert started == [(A_ID, "plan")]


def test_spec_gate_off_starts_the_plan_at_once(board, started):
    assert not C.GATES.get("spec")
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC))
    dispatch.dispatch_once(1, dry=True, pull=False)
    assert started == [(A_ID, "plan")]


def approved_card(cid=A_ID, tags=("api",), **meta):
    return mk(cid, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC), tags=tags,
              spec_approved_at="2026-09-29T10:00:00+00:00", **meta)


def test_a_second_spec_approve_does_not_write(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = approved_card()
    writes = []
    monkeypatch.setattr(commands, "update", lambda *a, **k: writes.append((a, k)))
    out = run_cli(commands.cmd_approve, id=A_ID, force=False)
    assert writes == [] and "already approved at" in out and "the planner starts on its own" in out


def test_approved_label_says_designing_or_planning_and_agent_state(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    live = {"stage": "plan", "session_id": "s1"}
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", "s1"))
    plain = approved_card()
    assert dispatch.approved_label(plain, "Spec ready", {}) == "spec approved \u2014 planning (waiting for a planner slot)"
    busy = approved_card(worker=live)
    assert dispatch.approved_label(busy, "Spec ready", {}) == "spec approved \u2014 planning (agent running)"
    ui = approved_card(tags=("frontend",))
    assert dispatch.approved_label(ui, "Spec ready", {}).startswith("spec approved \u2014 designing")
    unapproved = mk(A_ID, "x", "Spec ready", body(SPEC=SPEC))
    assert dispatch.approved_label(unapproved, "Spec ready", {}) is None
    assert dispatch.approved_label(approved_card(), "Plan for review", {}) is None


def test_pl_list_shows_the_approved_label(board, monkeypatch, capsys):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = approved_card()
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "share", lambda *a: None)
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: [])
    monkeypatch.setattr(commands, "paused", lambda: None)
    commands.cmd_list(argparse.Namespace(product=False, all=False))
    assert "spec approved \u2014 planning (waiting for a planner slot)" in capsys.readouterr().out


def test_an_approved_spec_leaves_the_needs_you_spec_group(monkeypatch):
    from pl.tui.needs import needs_groups
    monkeypatch.setattr(C, "GATES", {"spec": True})
    waiting = row(mk(B_ID, "Other", "Spec ready", body(SPEC=SPEC)), col="Spec ready")
    approved = row(approved_card(), col="Spec ready")
    g = needs_groups([waiting, approved])
    assert [r["card"]["id"] for r in g["specs"]] == [B_ID]


def test_spec_reject_appends_notes_to_input_and_sends_back_to_inbox(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="the idea", SPEC=SPEC),
                           spec_approved_at="2026-09-01T00:00:00+00:00", worker={"stage": "plan"})
    run_cli(commands.cmd_reject, id=A_ID, notes="add a negative AC for 404")
    c = board.items[A_ID]
    parts = sections(c["description"])
    assert parts["INPUT"].startswith("the idea\n\n## Spec review notes ")
    assert parts["INPUT"].endswith(" round 1\nadd a negative AC for 404")
    assert "REVIEW NOTES" not in parts
    assert c["list_id"] == "col-Inbox"
    assert c["metadata"]["spec_round"] == 1 and c["metadata"]["worker"] is None and c["metadata"]["spec_approved_at"] is None
    assert "spec_rejected" in events_kinds()
    c["list_id"] = "col-Spec ready"
    run_cli(commands.cmd_reject, id=A_ID, notes="second pass")
    c = board.items[A_ID]
    assert c["metadata"]["spec_round"] == 2
    assert "add a negative AC for 404" in sections(c["description"])["INPUT"]
    assert sections(c["description"])["INPUT"].endswith(" round 2\nsecond pass")


def test_plan_approve_and_reject_cli_output_unchanged(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    assert run_cli(commands.cmd_reject, id=A_ID, notes="split WP2") == (
        f"rejected {A_ID[:8]}  Add a thing\n  back in Spec ready with your notes; the planner re-plans on the next pass\n")
    assert "split WP2" in sections(board.items[A_ID]["description"])["REVIEW NOTES"]
    board.items[A_ID]["list_id"] = "col-Plan for review"
    assert run_cli(commands.cmd_approve, id=A_ID, force=False) == (
        f"approved {A_ID[:8]}  Add a thing\n  the dispatcher starts the run on its next pass\n")
    assert board.items[A_ID]["list_id"] == "col-Approved"


def test_approving_a_split_part_does_not_upload_the_shared_full_plan(board):
    full = PLAN + "\n## WP3\n" + "the other part's work\n" * 50
    part1 = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN), spec_slug="add-a-thing")
    part2 = mk(B_ID, "Add a thing (part 2)", "Plan for review", body(INPUT="i", PLAN="## WP3\nthe other part"),
               spec_slug="add-a-thing", plan_path=str(C.PLANS / "add-a-thing.md"))
    board.items.update({A_ID: part1, B_ID: part2})
    C.PLANS.mkdir(parents=True, exist_ok=True)
    (C.PLANS / "add-a-thing.md").write_text(full)   # the planner's full original plan; pl never pulled it
    out = run_cli(commands.cmd_approve, id=B_ID, force=False)
    assert "synced" not in out
    assert sections(board.items[B_ID]["description"])["PLAN"] == "## WP3\nthe other part"
    assert board.items[B_ID]["list_id"] == "col-Approved"
    commands.pull_plan(board.items[A_ID])   # reviewing part 1 rewrites the shared file with part 1's plan
    board.items[B_ID]["list_id"] = "col-Plan for review"
    run_cli(commands.cmd_approve, id=B_ID, force=False)
    assert sections(board.items[B_ID]["description"])["PLAN"] == "## WP3\nthe other part"


def test_approve_syncs_a_plan_you_edited_after_pl_pulled_it(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    path = commands.pull_plan(board.items[A_ID])
    path.write_text(PLAN + "\n- my review edit\n")
    out = run_cli(commands.cmd_approve, id=A_ID, force=False)
    assert f"synced your edits from {path} to the card" in out
    assert "my review edit" in sections(board.items[A_ID]["description"])["PLAN"]
    assert board.items[A_ID]["list_id"] == "col-Approved"


def test_an_edited_plan_over_the_card_limit_is_refused_naming_the_file(board):
    C.TRACKER = {**(C.TRACKER or {}), "max_card_chars": 2000}
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    before = copy.deepcopy(board.items[A_ID])
    path = commands.pull_plan(board.items[A_ID])
    path.write_text(PLAN + "x" * 3000)
    with pytest.raises(SystemExit) as e:
        run_cli(commands.cmd_approve, id=A_ID, force=False)
    assert str(path) in str(e.value) and "bigger than this card's part" in str(e.value)
    assert board.items[A_ID] == before


# ---------- text checks and files ----------

def test_spec_and_plan_checks():
    s = {k: (v, ok) for k, v, ok in spec_checks(SPEC, ["api", "p1"])}
    assert s["acceptance criteria"] == ("3", True)
    assert s["negative AC"][1] and s["regression AC"][1]
    assert s["open questions"] == ("none open", True)
    assert "api" in s["repos"][0]
    bad = {k: (v, ok) for k, v, ok in spec_checks("- AC-1 happy\n## Open questions\n- which table?\n", [])}
    assert not bad["negative AC"][1] and not bad["regression AC"][1] and not bad["open questions"][1]
    p = {k: (v, ok) for k, v, ok in plan_checks(PLAN)}
    assert p["budget"] == ("2 non-test / 1 test files / ~40 lines", True)
    assert p["work packages"] == ("2 (1 lite, 1 full)", True)
    assert p["deliberately not doing"] == ("present", True)
    assert not {k: ok for k, _, ok in plan_checks("# Plan\nno budget")}["budget"]


def test_local_plan_file_used_only_inside_the_plans_folder(tmp_path):
    outside = tmp_path / "evil.md"
    outside.write_text("EVIL")
    c = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN), plan_path=str(outside))
    assert review_text("plan", c).strip() == PLAN.strip()
    C.PLANS.mkdir(parents=True)
    inside = C.PLANS / "add-a-thing.md"
    inside.write_text("MY LOCAL EDITS")
    c["metadata"]["plan_path"] = str(inside)
    assert review_text("plan", c) == "MY LOCAL EDITS"
    os.utime(inside, (0, 0))    # older than the card: the card wins
    assert review_text("plan", c).strip() == PLAN.strip()


def test_spec_file_is_written_once_and_refuses_a_bad_slug():
    c = mk(A_ID, "Add a thing", "Spec ready", body(SPEC=SPEC), spec_slug="add-thing")
    p = review_file("spec", c)
    assert p == C.PLANS.parent / "specs" / "spec-add-thing.md"
    assert p.stat().st_mode & 0o777 == 0o600 and p.read_text().strip() == SPEC.strip()
    p.write_text("mine")
    assert review_file("spec", c).read_text() == "mine"
    c["metadata"]["spec_slug"] = "../../etc/x"
    with pytest.raises(SystemExit, match="slug"):
        review_file("spec", c)


def test_links_open_only_over_http():
    assert safe_link("https://example.test/doc") and safe_link("http://x.test")
    assert not safe_link("file:///etc/passwd") and not safe_link("javascript:alert(1)") and not safe_link("")


# ---------- the screen ----------

def app_data(rows):
    snap = {"rows": rows, "prof": "", "parked": False, "at": "", "summary": "", "summary2": "", "needs": {}, "disp": "running"}
    m = {"specs_written": 0, "plans_written": 0, "approvals": 0, "errors_last": None}
    return {"snapshot": snap, "metrics_by_window": {k: m for k in ("1h", "24h", "7d")},
            "daily": {"specs": [0] * 14, "plans": [0] * 14}, "pr_activity": {"opened": [], "merged": []}}


def row(c, col="Plan for review"):
    return {"kind": "review", "card": c, "worker": {}, "win": "", "col": col, "profile": "acme", "text": ""}


class Provider:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self.data


def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def test_plan_approve_through_the_screen_moves_the_card(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(ReviewScreen("plan", A_ID))
        await settle(pilot)
        text = screen_text(app)
        for s in ("Plan: add a thing", "2 non-test / 1 test files", "2 (1 lite, 1 full)", "deliberately not doing"):
            assert s in text, s
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and A_ID[:8] in app.screen.message
        await pilot.press("y")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Approved"
        assert board.items[A_ID]["metadata"]["approved_by"] == C.USER_EMAIL


async def test_send_back_needs_notes(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("plan", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await pilot.press("x")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Plan for review"
        assert "REVIEW NOTES" not in sections(board.items[A_ID]["description"])
        assert any("notes" in str(n.message) for n in app._notifications)
        screen.query_one("#review-notes", TextArea).load_text("split WP2 in two")
        await pilot.press("x")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Spec ready"
        assert "split WP2 in two" in sections(board.items[A_ID]["description"])["REVIEW NOTES"]


async def test_review_screen_link_click_opens_only_http(board, monkeypatch):
    from textual.widgets import Markdown
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    opened = []
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        monkeypatch.setattr(app, "open_url", lambda url, **k: opened.append(url))
        screen = ReviewScreen("plan", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        md = screen.query_one("#review-md", Markdown)
        assert md._open_links is False      # otherwise Textual opens file: and javascript: links itself
        for href in ("file:///etc/passwd", "javascript:alert(1)", "https://example.test/doc"):
            md.post_message(Markdown.LinkClicked(md, href))
            await settle(pilot)
        assert opened == ["https://example.test/doc"]


async def test_pipeline_keeps_the_selected_card_after_rows_reorder(board):
    a = mk(A_ID, "First plan", "Plan for review", body(PLAN=PLAN))
    b = mk(B_ID, "Second plan", "Plan for review", body(PLAN=PLAN))
    board.items.update({A_ID: a, B_ID: b})
    prov = Provider(app_data([row(a), row(b)]))
    app = PlApp(snapshot_provider=prov, interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index(B_ID))
        await pilot.pause()
        prov.data = app_data([row(b), row(a)])      # B moves up; row position 2 is now A
        app.refresh_data()
        await settle(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and B_ID[:8] in app.screen.message
        await pilot.press("y")
        await settle(pilot)
        assert board.items[B_ID]["list_id"] == "col-Approved"
        assert board.items[A_ID]["list_id"] == "col-Plan for review"


# ---------- fix round 1 ----------

def test_plan_path_outside_the_plans_folder_is_neither_read_nor_written(board, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    c = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN), plan_path=str(secret))
    board.items[A_ID] = c
    assert commands.plan_path(c) == C.PLANS / "add-a-thing.md" or commands.plan_path(c).parent == C.PLANS
    commands.pull_plan(c)
    assert secret.read_text() == "TOP SECRET"
    run_cli(commands.cmd_approve, id=A_ID, force=False)
    assert "TOP SECRET" not in board.items[A_ID]["description"]
    victim = C.PLANS.parent.parent / "victim.md"      # a spec_slug that climbs out of the plans folder
    victim.write_text("PRIVATE LOCAL FILE")
    c = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="i", PLAN=PLAN), spec_slug="../../victim")
    board.items[A_ID] = c
    with pytest.raises(SystemExit):
        commands.plan_path(c)
    with pytest.raises(SystemExit):
        run_cli(commands.cmd_approve, id=A_ID, force=False)
    assert "PRIVATE LOCAL FILE" not in board.items[A_ID]["description"]
    assert victim.read_text() == "PRIVATE LOCAL FILE"


async def test_odd_card_data_shows_an_error_and_the_app_keeps_running(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="i", PLAN=PLAN), review_round="x")
    board.items[B_ID] = mk(B_ID, "t", "Plan for review", body(INPUT="i", PLAN=PLAN))
    board.items[B_ID]["metadata"] = ["not", "a", "dict"]
    board.items[B_ID]["title"] = None
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        s = ReviewScreen("plan", A_ID)
        await app.push_screen(s)
        await settle(pilot)
        s.query_one(TextArea).load_text("n")
        await pilot.press("x")                    # review_round="x": int() raises ValueError in the worker
        await settle(pilot)
        assert app.is_running and app._exception is None
        await app.push_screen(ReviewScreen("plan", B_ID))   # metadata is a list: the load worker raises
        await settle(pilot)
        assert app.is_running and app._exception is None
        s.card = board.items[B_ID]                # a None title and no slug: `e` must not raise on the UI thread
        s.action_edit()
        assert app.is_running and app._exception is None


async def test_screen_refuses_when_the_card_is_no_longer_in_its_kind_column(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="i", SPEC=SPEC, PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Plan for review"      # the plan was not approved by a spec press
        screen.query_one("#review-notes", TextArea).load_text("notes")
        await pilot.press("x")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Plan for review"
        assert "REVIEW NOTES" not in sections(board.items[A_ID]["description"])


def test_gated_spec_send_back_refused_while_an_agent_is_working(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="i", SPEC=SPEC), worker={"stage": "plan"})
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("alive", "sid"))
    with pytest.raises(SystemExit, match="an agent is working on this card"):
        run_cli(commands.cmd_reject, id=A_ID, notes="n")
    assert board.items[A_ID]["list_id"] == "col-Spec ready"
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("dead", None))
    run_cli(commands.cmd_reject, id=A_ID, notes="n")
    assert board.items[A_ID]["list_id"] == "col-Inbox"


async def test_edit_survives_a_dangling_symlink(board, tmp_path, monkeypatch):
    d = C.PLANS.parent / "specs"
    d.mkdir(parents=True)
    (d / "spec-add-thing.md").symlink_to(tmp_path / "victim")
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="i", SPEC=SPEC), spec_slug="add-thing")
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID], "Spec ready")])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(ReviewScreen("spec", A_ID))
        await settle(pilot)
        await pilot.press("E")      # WP25: e edits in the app; E opens $EDITOR
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert not (tmp_path / "victim").exists()


# ---------- WP23: read and decide in the Pipeline pane ----------

def lean(c):
    """The snapshot copy of a card: no description, so the pane must read the card itself."""
    return {**copy.deepcopy(c), "description": ""}


class CountingTracker(FakeTracker):
    def __init__(self, items):
        super().__init__(items)
        self.reads = []

    def card(self, item_id):
        self.reads.append(item_id)
        return super().card(item_id)


@pytest.fixture
def counting(monkeypatch):
    fake = CountingTracker([])
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


async def open_pipeline(pilot):
    await settle(pilot)
    await pilot.press("2")   # the Pipeline tab: the first card is selected
    await settle(pilot)


async def test_pipeline_pane_shows_the_plan_under_the_metadata(counting):
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        lines = screen_text(app).splitlines()
        at = {s: next((i for i, line in enumerate(lines) if s in line), None)
              for s in ("column   Plan for review", "Plan: add a thing", "Deliberately not doing")}
        assert None not in at.values(), at
        assert at["column   Plan for review"] < at["Plan: add a thing"] < at["Deliberately not doing"]


async def test_pipeline_pane_reads_each_card_once_per_refresh(counting):
    a = mk(A_ID, "First plan", "Plan for review", body(PLAN=PLAN))
    b = mk(B_ID, "Second plan", "Plan for review", body(PLAN=PLAN))
    counting.items.update({A_ID: a, B_ID: b})
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(a)), row(lean(b))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        t = app.query_one("#cards-table")
        for cid in (B_ID, A_ID, B_ID, A_ID):
            t.move_cursor(row=t.get_row_index(cid))
            await settle(pilot)
        assert sorted(counting.reads) == [A_ID, B_ID]
        app.refresh_data()
        await settle(pilot)
        assert counting.reads.count(A_ID) == 1        # WP24: an unchanged card is not read again
        counting.items[A_ID]["updated_at"] = "2030-01-01T00:00:00+00:00"
        app.snapshot_provider.data = app_data([row(lean(counting.items[A_ID])), row(lean(b))])
        app.refresh_data()
        await settle(pilot)
        assert counting.reads.count(A_ID) == 2        # a changed card is


async def test_pipeline_pane_renders_card_markup_literally(counting):
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN + "\nnote [bold]x[/bold] [red]y[/red]\n"))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        assert "[bold]x[/bold] [red]y[/red]" in screen_text(app)


async def test_pipeline_pane_shows_the_rate_limit_message(counting, monkeypatch):
    from pl.trackers.github import RateLimited
    err = RateLimited(2_000_000_000)
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN))
    monkeypatch.setattr(counting, "card", lambda item_id: (_ for _ in ()).throw(err))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        assert err.note in screen_text(app) and app.is_running


async def test_pipeline_a_and_x_use_the_review_screen_paths(counting, monkeypatch):
    from pl.tui import review
    calls = []
    real = review.run_command
    monkeypatch.setattr(review, "run_command", lambda app, fn, after=None, kind=None, **kw: (
        calls.append((fn, kind, args(kw))), real(app, fn, after, kind=kind, **kw)))
    a = mk(A_ID, "First plan", "Plan for review", body(INPUT="i", PLAN=PLAN))
    b = mk(B_ID, "Second plan", "Plan for review", body(INPUT="i", PLAN=PLAN))
    counting.items.update({A_ID: a, B_ID: b})
    prov = Provider(app_data([row(lean(a)), row(lean(b))]))
    app = PlApp(snapshot_provider=prov, interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(pilot)
        assert calls[0] == (commands.cmd_approve, "plan", {"id": A_ID, "force": False})
        assert counting.items[A_ID]["list_id"] == "col-Approved"
        prov.data = app_data([row(lean(b))])          # the next refresh drops the approved card
        app.refresh_data()
        await settle(pilot)
        assert "Second plan" in str(app.query_one("#cards-detail").render())
        await pilot.press("x")
        await settle(pilot)
        assert isinstance(app.screen, ReviewScreen) and app.screen.card_id == B_ID
        app.screen.query_one("#review-notes", TextArea).load_text("split WP2")
        await pilot.press("escape", "x")              # the notes box has focus; esc leaves it, x sends back
        await settle(pilot)
        assert calls[-1] == (commands.cmd_reject, "plan", {"id": B_ID, "notes": "split WP2"})
        assert counting.items[B_ID]["list_id"] == "col-Spec ready"


# ---------- WP24: a refresh does not redraw what has not changed ----------

LONG = PLAN + "".join(f"- item {i}\n" for i in range(120))


class CopyProvider(Provider):
    def __call__(self):
        return copy.deepcopy(self.data)          # every pass is a fresh fetch: same data, new objects


def spy(monkeypatch, widget, name="update"):
    calls, real = [], getattr(widget, name)
    monkeypatch.setattr(widget, name, lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    return calls


async def test_pipeline_pane_is_left_alone_when_nothing_changed(counting, monkeypatch):
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=LONG))
    app = PlApp(snapshot_provider=CopyProvider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        scroller = app.query_one("#cards-body")
        scroller.scroll_to(y=10, animate=False)
        await settle(pilot)
        assert scroller.scroll_y == 10
        md = spy(monkeypatch, app.query_one("#cards-md"))
        det = spy(monkeypatch, app.query_one("#cards-detail"))
        clears = spy(monkeypatch, app.query_one("#cards-table"), "clear")
        for _ in range(2):
            app.refresh_data()
            await settle(pilot)
        assert (len(md), len(det), len(clears)) == (0, 0, 0)
        assert scroller.scroll_y == 10
        assert counting.reads == [A_ID]


async def test_pipeline_list_keeps_the_selected_card_when_a_row_is_inserted_above(counting):
    a = mk(A_ID, "First plan", "Plan for review", body(PLAN=PLAN))
    b = mk(B_ID, "Second plan", "Plan for review", body(PLAN=PLAN))
    n_id = "cccccccc-3333-4333-8333-333333333333"
    n = mk(n_id, "New plan", "Plan for review", body(PLAN=PLAN))
    counting.items.update({A_ID: a, B_ID: b, n_id: n})
    prov = CopyProvider(app_data([row(lean(a)), row(lean(b))]))
    app = PlApp(snapshot_provider=prov, interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index(B_ID))
        await settle(pilot)
        prov.data = app_data([row(lean(n)), row(lean(a)), row(lean(b))])
        app.refresh_data()
        await settle(pilot)
        assert t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value == B_ID
        assert "Second plan" in str(app.query_one("#cards-detail").render())


# ---------- WP25: answer a spec's open questions, edit the spec ----------

SPEC_OQ = """## 1. Summary
Add a thing.

## 7. Acceptance criteria
- AC-1 Given a lead, when it replies, then the reply is stored.

## 8. Open Questions & Decided Tradeoffs
1. Should the reply be stored when the lead is archived?
2. Which role may edit it?
   Owners only for now.

**Decided by default**
- Retention is 90 days.
- Export uses CSV.

## 9. Out of scope
- anything else
"""

ANSWERED = SPEC_OQ.replace("archived?\n", "archived?\n   **Answer:** yes, keep it\n").replace(
    "90 days.\n", "90 days.\n  **Answer:** 180 days\n")


def test_open_questions_found_numbered_and_bulleted_with_and_without_decided_by_default():
    qs = open_questions(SPEC_OQ)
    assert [(q["marker"], q["text"]) for q in qs] == [
        ("1.", "Should the reply be stored when the lead is archived?"), ("2.", "Which role may edit it?"),
        ("-", "Retention is 90 days."), ("-", "Export uses CSV.")]
    assert all(q["answer"] is None for q in qs)
    bullets = "# Spec\n\n### open questions\n* Who owns it?\n* (none)\n\n## Next\n- not a question\n"
    assert [q["text"] for q in open_questions(bullets)] == ["Who owns it?"]
    assert open_questions("## Summary\n- no questions here\n") == []
    assert open_questions(SPEC) == []                    # "(none)" is not a question


def test_saving_answers_adds_one_line_per_answered_item_and_nothing_else():
    assert with_answers(SPEC_OQ, {0: "yes, keep it", 2: "180 days"}) == ANSWERED
    qs = open_questions(ANSWERED)
    assert [q["answer"] for q in qs] == ["yes, keep it", None, "180 days", None]
    again = with_answers(ANSWERED, {0: "no", 2: "180 days"})
    assert again == ANSWERED.replace("**Answer:** yes, keep it", "**Answer:** no")
    assert again.count("**Answer:**") == 2
    assert with_answers(SPEC_OQ, {}) == SPEC_OQ
    assert "\n# PIPELINE:" not in with_answers(SPEC_OQ, {1: "x\n# PIPELINE: PLAN"})


def spec_card(board, spec=SPEC_OQ):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="the idea", SPEC=spec, PLAN="old plan"))
    return board.items[A_ID]


def spec_app(board):
    return PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID], "Spec ready")])), interval=3600)


async def answer(pilot, screen, values):
    from textual.widgets import Input
    boxes = list(screen.query(Input))
    for i, v in values.items():
        boxes[i].value = v
    await pilot.pause()
    return boxes


async def test_answers_are_saved_into_the_spec_through_the_tracker(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    spec_card(board)
    before = sections(board.items[A_ID]["description"])
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        boxes = await answer(pilot, screen, {0: "yes, keep it", 2: "180 days"})
        assert len(boxes) == 4
        await pilot.press("ctrl+s")
        await settle(pilot)
        after = sections(board.items[A_ID]["description"])
        assert after["SPEC"] == ANSWERED.rstrip("\n")
        assert after["INPUT"] == before["INPUT"] and after["PLAN"] == before["PLAN"]
        assert any("saved" in str(n.message) for n in app._notifications)
        assert "2 answered" in screen_text(app)


async def test_existing_answers_prefill_the_boxes(board):
    spec_card(board, ANSWERED)
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        assert [b.value for b in await answer(pilot, screen, {})] == ["yes, keep it", "", "180 days", ""]


async def test_unsaved_answers_survive_a_refresh(board):
    spec_card(board)
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await answer(pilot, screen, {1: "admins"})
        board.items[A_ID]["description"] = body(INPUT="the idea", SPEC=SPEC_OQ + "\nnew line from the agent\n")
        app.refresh_data()
        screen.load()
        await settle(pilot)
        assert (await answer(pilot, screen, {}))[1].value == "admins"
        assert "new line from the agent" not in screen_text(app)
        await pilot.press("ctrl+s")                     # the board spec changed under the reader: refused
        await settle(pilot)
        assert "**Answer:**" not in board.items[A_ID]["description"]
        assert any("changed" in str(n.message) for n in app._notifications)


async def test_approve_and_send_back_after_saving_use_the_existing_paths(board, monkeypatch):
    from pl.tui import review
    monkeypatch.setattr(C, "GATES", {"spec": True})
    calls, real = [], review.run_command
    monkeypatch.setattr(review, "run_command", lambda app, fn, after=None, kind=None, **kw: (
        calls.append((fn, kind, args(kw))), real(app, fn, after, kind=kind, **kw)))
    spec_card(board)
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await answer(pilot, screen, {0: "yes, keep it", 2: "180 days"})
        screen.query_one("#review-doc").focus()
        await pilot.press("a")                          # unsaved answers: refused, no dialog
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen) and calls == []
        await pilot.press("ctrl+s")
        await settle(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(pilot)
        assert calls[-1] == (commands.cmd_approve, "spec", {"id": A_ID, "force": False})
        assert board.items[A_ID]["metadata"]["spec_approved_at"]
    board.items[A_ID]["metadata"]["spec_approved_at"] = None
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await answer(pilot, screen, {1: "admins"})
        await pilot.press("ctrl+s")
        await settle(pilot)
        screen.query_one("#review-doc").focus()
        await pilot.press("x")                          # no notes needed: the answers are the note
        await settle(pilot)
        fn, kind, kw = calls[-1]
        assert (fn, kind) == (commands.cmd_reject, "spec")
        assert kw["notes"].startswith("answered 1 open question") and "Which role may edit it?" in kw["notes"]
        assert "admins" in kw["notes"]
        assert board.items[A_ID]["list_id"] == "col-Inbox"
        assert "admins" in sections(board.items[A_ID]["description"])["INPUT"]


async def test_in_app_editor_saves_the_edited_spec_and_esc_writes_nothing(board):
    spec_card(board)
    desc = board.items[A_ID]["description"]
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        screen.query_one("#review-doc").focus()
        await pilot.press("e")
        await pilot.pause()
        ed = screen.query_one("#review-editor", TextArea)
        assert ed.display and ed.has_focus and ed.text.rstrip("\n") == SPEC_OQ.rstrip("\n")
        ed.load_text("## Summary\nrewritten\n")
        await pilot.press("escape")
        await settle(pilot)
        assert board.items[A_ID]["description"] == desc and not ed.display
        await pilot.press("e")
        await pilot.pause()
        assert ed.text.rstrip("\n") == SPEC_OQ.rstrip("\n")       # esc threw the edit away
        ed.load_text("## Summary\nrewritten\n# PIPELINE: PLAN\nsmuggled\n")
        await pilot.press("ctrl+s")
        await settle(pilot)
        parts = sections(board.items[A_ID]["description"])
        assert parts["SPEC"] == "## Summary\nrewritten\n # PIPELINE: PLAN\nsmuggled"      # the marker is neutralised
        assert parts["PLAN"] == "old plan" and parts["INPUT"] == "the idea"
        assert not ed.display and "rewritten" in screen_text(app)


async def test_saving_refuses_a_card_that_moved_on(board):
    spec_card(board)
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        await answer(pilot, screen, {0: "yes"})
        board.items[A_ID]["list_id"] = "col-Plan for review"
        desc = board.items[A_ID]["description"]
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert board.items[A_ID]["description"] == desc
        assert any("now in 'Plan for review'" in str(n.message) for n in app._notifications)


def fake_editor(tmp_path, monkeypatch, app, new_text=None):
    """$EDITOR = a tiny script that rewrites the file (or leaves it alone); the headless app cannot suspend."""
    import contextlib
    import sys
    script = tmp_path / "ed.py"
    script.write_text("import sys\n" + (f"open(sys.argv[1], 'w').write({new_text!r})\n" if new_text is not None else ""))
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)


async def external_edit(board, tmp_path, monkeypatch, new_text, before_edit=None):
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        fake_editor(tmp_path, monkeypatch, app, new_text)
        screen = ReviewScreen("spec", A_ID)
        await app.push_screen(screen)
        await settle(pilot)
        if before_edit:
            before_edit()
        screen.query_one("#review-doc").focus()
        await pilot.press("E")
        await settle(pilot)
        return app, [str(n.message) for n in app._notifications]


async def test_external_editor_edit_is_written_to_the_card(board, tmp_path, monkeypatch):
    spec_card(board)
    app, said = await external_edit(board, tmp_path, monkeypatch, "## Summary\nedited in vim\n")
    parts = sections(board.items[A_ID]["description"])
    assert parts["SPEC"] == "## Summary\nedited in vim"
    assert parts["INPUT"] == "the idea" and parts["PLAN"] == "old plan"
    assert any("saved" in m for m in said)


async def test_external_editor_without_changes_writes_nothing(board, tmp_path, monkeypatch):
    spec_card(board)
    desc, stamp = board.items[A_ID]["description"], board.items[A_ID]["updated_at"]
    app, said = await external_edit(board, tmp_path, monkeypatch, None)
    assert board.items[A_ID]["description"] == desc and board.items[A_ID]["updated_at"] == stamp
    assert not any("saved" in m for m in said)


async def test_external_editor_refuses_a_card_that_moved_and_keeps_the_file(board, tmp_path, monkeypatch):
    spec_card(board)
    desc = board.items[A_ID]["description"]

    def move():
        board.items[A_ID]["list_id"] = "col-Plan for review"
    app, said = await external_edit(board, tmp_path, monkeypatch, "## Summary\nkept locally\n", move)
    path = C.PLANS.parent / "specs" / "spec-add-a-thing.md"
    assert board.items[A_ID]["description"] == desc
    assert path.read_text() == "## Summary\nkept locally\n"
    assert any("now in 'Plan for review'" in m and str(path) in m for m in said)


async def pipeline_edit(board, tmp_path, monkeypatch, new_text, before_edit=None):
    """e on the Pipeline's first card with a fake $EDITOR; returns the notices."""
    app = spec_app(board)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        fake_editor(tmp_path, monkeypatch, app, new_text)
        if before_edit:
            before_edit()
        await pilot.press("e")
        await settle(pilot)
        return [str(n.message) for n in app._notifications]


INPUT_FILE = ("inputs", "input-add-a-thing.md")


async def test_pipeline_e_edits_the_input_in_the_editor_and_writes_it_back(board, tmp_path, monkeypatch):
    spec_card(board)
    modes = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, flags, mode=0o777, **kw: modes.append((str(p), mode)) or real(p, flags, mode, **kw))
    said = await pipeline_edit(board, tmp_path, monkeypatch, "the idea, made clearer\n# PIPELINE: SPEC\nnot a section\n")
    parts = sections(board.items[A_ID]["description"])
    assert parts["INPUT"] == "the idea, made clearer\n # PIPELINE: SPEC\nnot a section"   # a marker line is neutralised
    assert parts["SPEC"] == SPEC_OQ.strip() and parts["PLAN"] == "old plan"
    path = C.PLANS.parent.joinpath(*INPUT_FILE)
    assert (str(path), 0o600) in modes and not path.exists()   # written 0600; removed once saved
    assert any("saved INPUT" in m for m in said)


async def test_pipeline_e_without_changes_writes_nothing(board, tmp_path, monkeypatch):
    spec_card(board)
    desc = board.items[A_ID]["description"]
    said = await pipeline_edit(board, tmp_path, monkeypatch, None)
    assert board.items[A_ID]["description"] == desc and not any("saved" in m for m in said)


async def test_pipeline_e_refuses_an_input_changed_meanwhile_and_keeps_the_file(board, tmp_path, monkeypatch):
    spec_card(board)

    def change():   # someone sends the spec back meanwhile: their notes land in INPUT
        board.items[A_ID]["description"] = body(INPUT="the idea\n\nnotes", SPEC=SPEC_OQ, PLAN="old plan")
    import pl.tui.review as review_mod
    real = review_mod._run

    def editor(argv):
        change()
        return real(argv)
    monkeypatch.setattr(review_mod, "_run", editor)
    said = await pipeline_edit(board, tmp_path, monkeypatch, "my edit\n")
    path = C.PLANS.parent.joinpath(*INPUT_FILE)
    assert sections(board.items[A_ID]["description"])["INPUT"] == "the idea\n\nnotes"
    assert path.read_text() == "my edit\n" and any("changed on the board" in m and str(path) in m for m in said)


def test_watch_keeps_done_cards_apart_with_one_board_read(board, monkeypatch):
    from pl import watch
    board.items[A_ID] = mk(A_ID, "Shipped", "Done", done_at=OLD)
    board.items[B_ID] = mk(B_ID, "Open", "Inbox")
    reads = []
    real = watch.cards
    monkeypatch.setattr(watch, "cards", lambda: reads.append(1) or real())
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    monkeypatch.setattr(watch, "pane_tail", lambda *a: [])
    snap = watch.watch_snapshot()
    assert reads == [1]
    assert [r["card"]["id"] for r in snap["rows"] if r.get("card")] == [B_ID]   # every other view sees no Done card
    assert [(r["card"]["id"], r["col"]) for r in snap["done"]] == [(A_ID, "Done")]
    assert "Shipped" not in watch.render_watch(snap) and "Done" not in snap["summary2"]


async def test_pipeline_o_opens_the_spec_with_the_first_answer_box_focused(counting, monkeypatch):
    from textual.widgets import Input
    monkeypatch.setattr(C, "GATES", {"spec": True})
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC_OQ))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]), "Spec ready")])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        await pilot.press("o")
        await settle(pilot)
        assert isinstance(app.screen, ReviewScreen) and app.screen.kind == "spec"
        assert isinstance(app.screen.focused, Input)


# ---------- WP26: cards stay under the board's size limit ----------

from pl import board as B, ideas, product  # noqa: E402
from pl.tui.review import size_text, write_spec  # noqa: E402

LIMIT_MSG = "over the limit of 2,000 (the board server cannot return larger cards); split the feature into smaller cards"


class SpyTracker(FakeTracker):
    """Records every write; a refused write must leave this empty."""
    def __init__(self, items):
        super().__init__(items)
        self.writes = []

    def columns(self):
        return {t: f"col-{t}" for t in [*C.COLUMNS, "Triage"]}

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        self.writes.append(("create", description))
        cid = f"new-{len(self.writes)}"
        self.items[cid] = {"id": cid, "title": title, "description": description, "tags": list(tags),
                           "metadata": metadata or {}, "list_id": f"col-{column}", "updated_at": OLD}
        return copy.deepcopy(self.items[cid])

    def update(self, item_id, *, verify=True, **fields):
        self.writes.append(("update", fields.get("description")))
        return super().update(item_id, verify=verify, **fields)


@pytest.fixture
def writes_spy(monkeypatch):
    fake = SpyTracker([])
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    C.TRACKER = {**(C.TRACKER or {}), "max_card_chars": 2000}
    return fake


def _spec_reject(t, text, mp):
    C.GATES = {"spec": True}
    t.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC))
    run_cli(commands.cmd_reject, id=A_ID, notes=text)


def _plan_reject(t, text, mp):
    t.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    run_cli(commands.cmd_reject, id=A_ID, notes=text)


def _plan_approve_sync(t, text, mp):
    t.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    path = commands.pull_plan(t.items[A_ID])   # pl pulled the plan, then you edited it
    path.write_text(PLAN + text)
    run_cli(commands.cmd_approve, id=A_ID, force=False)


def _write_spec(t, text, mp):
    t.items[A_ID] = mk(A_ID, "Add a thing", "Spec ready", body(INPUT="idea", SPEC=SPEC))
    write_spec(A_ID, sections(t.items[A_ID]["description"])["SPEC"], SPEC + text)


def _idea(t, text, mp):
    C.INTAKE = {}
    run_cli(commands.cmd_idea, text="an idea\n" + text, title=None, doc=[], account=None, repo=[], start=False)


def _done_note(t, text, mp):
    C.INTAKE = {"type": "mcp"}
    t.items[B_ID] = mk(B_ID, "Product ask", "Triage")
    C.USER_EMAIL = "me@example.test"
    t.items[B_ID]["assigned_to"] = C.USER_EMAIL
    mp.setattr(commands, "cards", lambda: [])   # the pipeline board has no card with this id
    run_cli(commands.cmd_done, id=B_ID, note=text)


def _idea_approve(t, text, mp):
    ideas._find_or_create({"id": "i1", "title": "An idea", "brief": {**{k: [] for k in ideas.FIELDS}, "problem": "p " + text}})


def _pull(t, text, mp):
    C.INTAKE = {"type": "mcp"}
    pc = mk(B_ID, "Product ask", "Triage", desc=text)
    t.items[B_ID] = pc
    with redirect_stdout(io.StringIO()):
        product.pull_one(pc, [])


WRITE_PATHS = [_spec_reject, _plan_reject, _plan_approve_sync, _write_spec, _idea, _done_note, _idea_approve, _pull]


@pytest.mark.parametrize("path", WRITE_PATHS, ids=lambda f: f.__name__)
def test_every_description_write_refuses_an_oversized_card_before_any_tracker_write(writes_spy, path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        path(writes_spy, "x" * 3000, monkeypatch)
    assert "this card would be " in str(e.value) and LIMIT_MSG in str(e.value)
    assert writes_spy.writes == []


@pytest.mark.parametrize("path", WRITE_PATHS, ids=lambda f: f.__name__)
def test_every_description_write_passes_under_the_limit(writes_spy, path, monkeypatch):
    path(writes_spy, "short", monkeypatch)
    assert any(d is not None for _, d in writes_spy.writes)
    assert all(B.card_size(d) <= 2000 for _, d in writes_spy.writes if d is not None)


def test_card_size_is_the_json_string_length():
    for s in ("", 'a "quoted" word', "two\nlines\tand a tab", "ünï"):
        assert B.card_size(s) == len(json.dumps(s))
    assert B.card_size('a"b\nc') == 9
    assert B.card_size(None) == 2


def test_the_limit_defaults_to_40000_and_tracker_max_card_chars_overrides():
    C.TRACKER = {}
    assert B.max_card_chars() == 40_000
    C.TRACKER = {"max_card_chars": 5000}
    assert B.max_card_chars() == 5000
    with pytest.raises(SystemExit, match="over the limit of 5,000"):
        B.check_size("x" * 5000)
    assert B.check_size("x" * 4998) == "x" * 4998


def test_size_line_is_dim_then_warning_at_80_percent_then_error_when_over():
    C.TRACKER = {"max_card_chars": 1000}
    for n, want in ((100, "dim"), (799, "dim"), (800, "yellow"), (1000, "yellow"), (1001, "red")):
        t = size_text("x" * (n - 2))
        assert t.plain == f"size {n:,} / 1,000"
        assert str(t.style) == want, (n, t.style)
    C.TRACKER = {}
    assert size_text("x" * 31_198).plain == "size 31,200 / 40,000"


async def test_review_screen_shows_the_card_size(counting):
    desc = body(INPUT="idea", PLAN=PLAN)
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", desc)
    want = f"size {B.card_size(desc):,} / 40,000"
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(ReviewScreen("plan", A_ID))
        await settle(pilot)
        assert want in screen_text(app)


async def test_pipeline_pane_shows_the_cut_message_for_a_card_the_board_cut(monkeypatch, tmp_path):
    from test_tracker_mcp import SERVER, stdio_cfg, tools
    from pl.trackers import mcp as M
    script = tmp_path / "fake_board_server.py"
    script.write_text(SERVER)
    C.TRACKER = stdio_cfg(script, tools=tools(card={"tool": "boards", "args": {"action": "cut", "board_id": "{board_id}"},
                                                    "result": "item"}))
    trackers.reset()
    c = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(c))])), interval=3600)
    try:
        async with app.run_test(size=(176, 48)) as pilot:
            await open_pipeline(pilot)
            text = screen_text(app)
            assert "the board server cut this card off at 48,000 characters" in text, text
            assert "non-JSON" not in text
    finally:
        M.close_all()
        trackers.reset()


# ---------- WP32: the full card from the Pipeline tab ----------

async def test_enter_on_a_pipeline_card_opens_the_whole_card(board, monkeypatch):
    import threading

    from pl.tui import cards as P
    desc = "intro line\n" + body(INPUT="the idea", SPEC=SPEC, DESIGN="the design", PLAN=PLAN,
                                 **{"REVIEW NOTES": "fix the budget", "RUN LEDGER": "WP1 done"})
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", desc, tags=("frontend", "api"))
    board.items[A_ID]["assigned_to_display"] = "Sam"
    reads = []
    real = board.card
    monkeypatch.setattr(board, "card", lambda cid: reads.append(threading.current_thread()) or real(cid))
    copied, opened = [], []
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    async with app.run_test(size=(176, 80)) as pilot:
        await open_pipeline(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, P.CardScreen)
        assert reads and reads[-1] is not threading.main_thread()
        text = screen_text(app)
        for s in ("Add a thing", "Plan for review", "frontend, api", "Sam", "https://example.test/" + A_ID,
                  "pipeline_mode=auto", "the idea", "AC-2 Negative", "the design", "Plan: add a thing",
                  "fix the budget", "WP1 done"):
            assert s in text, s
        order = [text.index(s) for s in ("INPUT", "SPEC", "DESIGN", "PLAN", "REVIEW NOTES", "RUN LEDGER")]
        assert order == sorted(order)
        await pilot.press("y")
        await pilot.press("O")
        await settle(pilot)
        assert copied == [desc]
        assert opened == ["https://example.test/" + A_ID]
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, P.CardScreen)
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, P.CardScreen) and app.is_running


async def test_a_card_that_cannot_be_read_shows_the_trackers_error(board, monkeypatch):
    from pl.tui import cards as P
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(PLAN=PLAN))

    def boom(cid):
        raise SystemExit("pl: the board server cut this card off at 48,000 characters [bold]x[/bold]")
    monkeypatch.setattr(board, "card", boom)
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(P.CardScreen(board.items[A_ID]["id"], "Plan for review"))
        await settle(pilot)
        text = screen_text(app)
        assert "cut this card off at 48,000 characters [bold]x[/bold]" in text, text
        assert "loading" not in text


# ---------- WP35: ctrl+x sends back from inside the notes box, with visible feedback ----------

def notes_of(app):
    return [str(n.message) for n in app._notifications]


async def test_ctrl_x_in_the_notes_box_sends_back_and_says_where_it_went(counting):
    counting.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(lean(counting.items[A_ID]))])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        await pilot.press("ctrl+x")                   # Pipeline pane: opens the review screen on the notes box
        await settle(pilot)
        assert isinstance(app.screen, ReviewScreen)
        assert isinstance(app.screen.focused, TextArea) and app.screen.focused.id == "review-notes"
        await pilot.press(*"split WP2", "enter", *"drop WP3")
        await pilot.press("ctrl+x")                   # still typing in the box: sends back, does not cut a line
        await settle(pilot)
        assert counting.items[A_ID]["list_id"] == "col-Spec ready"
        assert "split WP2\ndrop WP3" in sections(counting.items[A_ID]["description"])["REVIEW NOTES"]
        assert "Sent back to Spec ready: Add a thing — 2 notes" in notes_of(app)
        assert not isinstance(app.screen, ReviewScreen)
        assert "rejected" in events_kinds()


async def test_a_refused_send_back_says_why_and_keeps_the_notes(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("plan", A_ID, notes_first=True)
        await app.push_screen(screen)
        await settle(pilot)
        await pilot.press(*"split WP2")
        board.items[A_ID]["list_id"] = "col-Approved"   # someone approved it meanwhile
        await pilot.press("ctrl+x")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Approved"
        assert any(m.startswith("Not sent back: the card is now in 'Approved'") for m in notes_of(app))
        assert app.screen is screen and screen.query_one("#review-notes", TextArea).text == "split WP2"
        assert "sending back" not in str(screen.query_one("#review-notes").border_title)


async def test_a_second_ctrl_x_while_sending_does_nothing(board, monkeypatch):
    import threading
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    gate, calls, real = threading.Event(), [], commands.cmd_reject

    def slow(a):
        calls.append(a.id)
        gate.wait(5)
        real(a)
    monkeypatch.setattr(commands, "cmd_reject", slow)
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        screen = ReviewScreen("plan", A_ID, notes_first=True)
        await app.push_screen(screen)
        await settle(pilot)
        await pilot.press(*"split WP2", "ctrl+x")
        await pilot.pause()
        assert "sending back" in str(screen.query_one("#review-notes").border_title)
        await pilot.press("ctrl+x", "x")
        await pilot.pause()
        gate.set()
        await settle(pilot)
        assert calls == [A_ID]
        assert sum(m.startswith("Sent back") for m in notes_of(app)) == 1


async def test_ctrl_x_with_empty_notes_is_refused(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(ReviewScreen("plan", A_ID, notes_first=True))
        await settle(pilot)
        await pilot.press(" ", "ctrl+x")
        await settle(pilot)
        assert board.items[A_ID]["list_id"] == "col-Plan for review"
        assert any("write what to change first" in m for m in notes_of(app))


async def test_approve_says_where_the_card_went(board):
    board.items[A_ID] = mk(A_ID, "Add a thing", "Plan for review", body(INPUT="idea", PLAN=PLAN))
    app = PlApp(snapshot_provider=Provider(app_data([row(board.items[A_ID])])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(ReviewScreen("plan", A_ID))
        await settle(pilot)
        await pilot.press("a", "y")
        await settle(pilot)
        assert "Approved: Add a thing → Approved" in notes_of(app)


async def test_pipeline_card_and_card_screen_show_the_approved_label(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    r = row(approved_card(), col="Spec ready")
    r["approved"] = "spec approved \u2014 planning (agent running)"
    app = PlApp(snapshot_provider=Provider(app_data([r])), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await open_pipeline(pilot)
        t = app.query_one("#cards-table")
        assert "spec approved \u2014 planning (agent running)" in t.get_row(r["card"]["id"])[2].plain
    from pl.tui.cards import card_head
    assert "spec approved" in card_head(approved_card(), "Spec ready", None, r["approved"]).plain


def test_watch_rows_carry_the_approved_label(board, monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    board.items[A_ID] = approved_card()
    from pl import watch
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    snap = watch.watch_snapshot()
    got = [r for r in snap["rows"] if r.get("card") and r["card"]["id"] == A_ID]
    assert got and got[0]["approved"].startswith("spec approved \u2014 planning")


def test_watch_rows_mark_only_an_agent_dead_at_the_attempt_limit_as_failed(board, monkeypatch):
    from pl import watch
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    monkeypatch.setattr(watch, "worker_status", lambda w, reg: ("dead", None))
    failed = {}
    for attempts in (3, 2):
        c = approved_card()
        c["metadata"] = {**c["metadata"], "worker": {"stage": "plan", "attempts": attempts, "window": "@1", "session_id": "s"}}
        board.items[A_ID] = c
        got = [r for r in watch.watch_snapshot()["rows"] if r.get("card") and r["card"]["id"] == A_ID]
        failed[attempts] = got[0].get("failed")
    assert failed == {3: True, 2: False}
