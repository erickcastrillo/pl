"""WP9: the Textual console renders every view from one provider message, off the UI thread."""
import argparse
import io
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from pl import cli, trackers, watch
from pl import config as C
from pl.tui import app as tui_app
from pl.tui import pipeline as tui_pipeline
from pl.tui import prs as tui_prs
from pl.tui.app import PlApp

NOW = datetime.now(timezone.utc)


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
    monkeypatch.setattr(tui_prs, "_run", FakeGh(), raising=False)          # the PRs tab never reaches the real gh
    monkeypatch.setattr(tui_prs, "intent_text", lambda url, body: f"# What {url} was meant to do", raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class FakeGh:
    """Stands in for subprocess.run in the PRs tab: records every argv and env; answers `pr view` with view."""

    def __init__(self, view=None, merge=(0, "merged", "")):
        self.calls, self.view, self.merge = [], view, merge

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("env")))
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0 if self.view else 1, json.dumps(self.view or {}), "" if self.view else "no gh here")
        return subprocess.CompletedProcess(argv, self.merge[0], self.merge[1], self.merge[2])

    def merges(self):
        return [a for a, _ in self.calls if a[:3] == ["gh", "pr", "merge"]]


def _view(labels=("pl:merge-ready",), checks=("SUCCESS", "SUCCESS"), mergeable="MERGEABLE", review="APPROVED"):
    return {"title": "feat: merge me first", "author": {"login": "octo"}, "headRefName": "feat/x", "baseRefName": "development",
            "additions": 120, "deletions": 7, "changedFiles": 3, "labels": [{"name": n} for n in labels],
            "statusCheckRollup": [{"conclusion": c, "status": "COMPLETED"} for c in checks],
            "reviewDecision": review, "mergeable": mergeable, "body": "cardId=x"}


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _card(cid, title, col, kind="row", account="acme", auto=True, hours=5):
    c = {"id": cid, "title": title, "tags": ["frontend"], "updated_at": _iso(hours),
         "metadata": {"pipeline_mode": "auto" if auto else "manual", "profile": account}, "list_id": col}
    return {"kind": kind, "card": c, "worker": {}, "win": "", "col": col, "profile": account, "text": f"{cid[:8]}  {title}"}


def _pr(repo, number, title, state):
    pr = {"repo": repo, "number": number, "title": title, "state": state, "url": f"https://github.com/o/{repo}/pull/{number}"}
    return {"kind": "review", "card": None, "pr": pr, "col": "PRs", "worker": {}, "win": "", "text": f" ? {repo}#{number}  {title}"}


def fake_data(title="Add a salesforce keyword to the palette", pr_title="Keyboard shortcuts: gather stalled PRs"):
    rows = [
        {"kind": "loops_head", "text": "LOOPS   1 of 1 running", "card": None},
        {"kind": "loop_idle", "card": None, "loop": "merge-check", "col": "Loops", "worker": {}, "win": "", "text": " ○ merge-check"},
        _pr("frontend", 1117, pr_title, "decide"),
        _pr("api", 1576, "fix(users): delete routes staff only", "rework"),
        _pr("frontend", 1200, "feat: merge me first", "merge"),
        _pr("frontend", 1201, "feat: merge me second", "merge"),
        _pr("api", 1300, "feat: waiting for the gate", "gate"),
        _card("spec0001aaaa", "pl: track cards in GitHub Projects", "Spec ready", hours=2),
        _card("4a000001aaaa", title, "Plan for review", kind="review", hours=96),
        _card("60000002aaaa", "api: no persistent BCC field", "Plan for review", kind="review", account="acme2", hours=72),
        _card("c0000003aaaa", "Host DB proxy serves a frozen database", "Manual", auto=False, hours=30),
        _card("d0000004aaaa", "objective_outcome recorded succeeded", "PR open", hours=120),
    ]
    snap = {"rows": rows, "prof": "acme: ok (0 running, 0 queued)  acme2: ok (0 running, 0 queued)", "parked": False,
            "at": "14:30:00", "summary": "NEEDS YOU: 2 plans to review", "summary2": "CARDS", "needs": {"review": 2},
            "disp": "running"}
    metrics = {"1h": {"specs_written": 3, "plans_written": 2, "approvals": 0, "errors_last": None},
               "24h": {"specs_written": 12, "plans_written": 11, "approvals": 1, "errors_last": None},
               "7d": {"specs_written": 71, "plans_written": 54, "approvals": 9,
                      "errors_last": {"ts": _iso(1), "kind": "error", "message": "board API answered Rate exceeded"}}}
    prs = {"opened": [_iso(0.5), _iso(3), _iso(30), _iso(50), _iso(60)], "merged": [_iso(10), _iso(100)]}
    return {"snapshot": snap, "metrics_by_window": metrics, "pr_activity": prs,
            "daily": {"specs": [0] * 13 + [12], "plans": [0] * 13 + [11]}}


class Provider:
    """Fake snapshot provider: records the thread of every call; raises when told to."""

    def __init__(self, data=None):
        self.data, self.threads, self.fail = data or fake_data(), [], None

    def __call__(self):
        self.threads.append(threading.current_thread())
        if self.fail:
            raise self.fail
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


def tile(app, name):
    return app.query_one(f"#tile-{name} Digits").value


async def test_dashboard_renders_tiles_and_panels():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert [tile(app, n) for n in ("specs", "plans", "opened", "merged", "ready", "waiting")] == \
            ["12", "11", "2", "1", "2", "5"]
        text = screen_text(app)
        for s in ("SPECS WRITTEN", "READY TO MERGE", "WAITING ON YOU", "Throughput", "Where work waits",
                  "Plan for review", "Decide next", "2 PRs pass the merge check", "Health",
                  "spend", "not tracked yet", "exceeded", "legacy", "dispatcher running"):
            assert s in text, s


async def test_needs_you_groups_and_counts(monkeypatch):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await pilot.pause()
        text = screen_text(app)
        for s in ("PLANS TO REVIEW 2", "PRS STOPPED FOR YOUR CALL 1", "NEEDS REWORK 1", "MANUAL · YOURS TO DO 1",
                  "2 PRs ready to merge", "4a000001", "frontend#1117"):
            assert s in text, s
        assert "SPECS TO REVIEW" not in text          # the spec gate is off in legacy mode
        await pilot.press("a")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen" and "4a000001" in app.screen.message   # WP10: a opens the approve dialog


async def test_needs_you_shows_specs_when_the_gate_is_on(monkeypatch):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await pilot.pause()
        assert "SPECS TO REVIEW 1" in screen_text(app)


async def test_pull_requests_groups():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await pilot.pause()
        text = screen_text(app)
        for s in ("NEED YOUR DECISION 1", "READY TO MERGE 2", "NEED REWORK 1", "AWAITING MERGE CHECK 1",
                  "frontend#1200  feat: merge me first", "api#1300  feat: waiting for the gate"):
            assert s in text, s


async def test_pipeline_columns_and_cards():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        text = screen_text(app)
        for s in ("Inbox (0)", "Spec ready (1)", "Plan for review (2)", "Manual (1)", "PR open (1)",
                  "4a000001", "acme2", "your review", "yours to do"):
            assert s in text, s
        assert "Done (" not in text


async def test_refresh_failure_keeps_numbers_and_shows_the_error():
    prov = Provider()
    app = PlApp(snapshot_provider=prov)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        prov.fail = SystemExit("pl: board API answered Rate exceeded")
        app.refresh_data()
        await settle(pilot)
        assert tile(app, "specs") == "12"
        assert "Rate exceeded" in str(app.query_one("#header").render())
        await pilot.press("w")          # data must still be there: the next window shows its own fake value
        await pilot.pause()
        assert tile(app, "specs") == "71"
        prov.fail = None
        app.refresh_data()
        await settle(pilot)
        assert "Rate exceeded" not in str(app.query_one("#header").render())


async def test_w_cycles_the_window():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        seen = [(tile(app, "specs"), tile(app, "opened"))]
        for _ in range(3):
            await pilot.press("w")
            await pilot.pause()
            seen.append((tile(app, "specs"), tile(app, "opened")))
        assert seen == [("12", "2"), ("71", "5"), ("3", "1"), ("12", "2")]


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
async def test_dashboard_bottom_row_three_equal_columns(size):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        widths = [app.query_one(i).size.width for i in ("#decide", "#health", "#standup-panel")]
        assert min(widths) > 0 and max(widths) - min(widths) <= 1, widths


async def test_small_terminal_every_tab_and_pipeline_scrolls_sideways():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        for key in "12345678":
            await pilot.press(key)
            await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        assert app.query_one("#pipeline-scroll").max_scroll_x > 0


async def test_provider_only_runs_off_the_ui_thread():
    prov = Provider()
    app = PlApp(snapshot_provider=prov)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.refresh_data()
        await settle(pilot)
    assert len(prov.threads) >= 2
    assert all(t is not threading.main_thread() for t in prov.threads)


async def test_card_markup_is_shown_as_plain_text():
    title = "[bold]x[/bold] [link=https://evil.example]y[/link]"
    pr_title = "[red]pr[/red] [link=https://evil.example]z[/link]"
    app = PlApp(snapshot_provider=Provider(fake_data(title=title, pr_title=pr_title)))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await pilot.pause()
        table_line = next(l for l in screen_text(app).splitlines() if "4a000001 " in l)
        assert "[bold]x[/bold]" in table_line.split("││")[0]      # the list, not only the detail pane
        assert "[bold]x[/bold]" in str(app.query_one("#needs-detail").render())        # card detail pane
        app.query_one("#needs-table").move_cursor(row=4)                                 # the PR row
        await pilot.pause()
        assert "[red]pr[/red]" in str(app.query_one("#needs-detail").render())         # PR detail pane
        await pilot.press("5")
        await pilot.pause()
        assert "[red]pr[/red] [link=https://evil.example]z[/link]" in screen_text(app)
        await pilot.press("4")
        await pilot.pause()
        assert "[bold]x[/bold]" in screen_text(app)


async def test_pipeline_keys_jump_and_open_off_the_ui_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(tui_pipeline, "jump_to_window", lambda win: calls.append(("jump", win, threading.current_thread())) or "ok")
    opened = []
    monkeypatch.setattr(C, "TRACKER", {"type": "mcp", "board_id": "b-1", "server": {"command": "none"},
                                       "card_url": "https://boards.example/{board_id}?cardId={item_id}"})
    trackers.reset()
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "4a000001aaaa").focus()
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("c")
        await settle(pilot)
    assert calls and calls[0][2] is not threading.main_thread()
    assert opened == ["https://boards.example/b-1?cardId=4a000001aaaa"]


async def test_c_opens_only_web_links(monkeypatch):
    class T:
        url = staticmethod(lambda cid: urls.pop(0))
    urls = ["file:///etc/passwd", "HTTPS://example.com/card/1"]
    monkeypatch.setattr(tui_pipeline.trackers, "get", lambda kind: T)
    opened = []
    monkeypatch.setattr(C, "TRACKER", {"type": "mcp", "board_id": "b-1", "server": {"command": "none"},
                                       "card_url": "https://boards.example/{board_id}?cardId={item_id}"})
    trackers.reset()
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "4a000001aaaa").focus()
        await pilot.pause()
        await pilot.press("c")
        await settle(pilot)
        assert opened == [] and any("no web link" in str(n.message) for n in app._notifications)
        await pilot.press("c")
        await settle(pilot)
        assert opened == ["HTTPS://example.com/card/1"]


def test_pr_activity_counts_and_survives_gh_failure(monkeypatch):
    seen = []

    def fake_gh(args):
        seen.append(args)
        if "--merged-at" in args:
            return [{"closedAt": "2026-09-27T10:00:00Z"}]
        return [{"createdAt": "2026-09-27T09:00:00Z"}, {"createdAt": "2026-09-20T09:00:00Z"}]
    monkeypatch.setattr(watch, "_gh", fake_gh)
    assert watch.pr_activity(days=14) == {"opened": ["2026-09-27T09:00:00Z", "2026-09-20T09:00:00Z"],
                                          "merged": ["2026-09-27T10:00:00Z"]}
    assert all("--created" in a or "--merged-at" in a for a in seen)

    def boom(args):
        raise subprocess.TimeoutExpired("gh", 30)
    monkeypatch.setattr(watch, "_gh", boom)
    assert watch.pr_activity() is None


def test_bare_pl_opens_the_console_only_on_a_terminal(fake_home, monkeypatch, capsys):
    (fake_home / ".pl-t").mkdir()
    (fake_home / ".pl-t" / "config.toml").write_text("")
    monkeypatch.setenv("PL_CONFIG_DIR", str(fake_home / ".pl-t"))
    ran = []
    monkeypatch.setattr(PlApp, "run", lambda self, *a, **k: ran.append(self.interval))
    monkeypatch.setattr(sys, "argv", ["pl"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    cli.main()
    assert ran == [15]
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    cli.main()
    assert ran == [15] and "usage: pl" in capsys.readouterr().out


def test_pl_watch_launches_the_console(monkeypatch):
    ran = []
    monkeypatch.setattr(PlApp, "run", lambda self, *a, **k: ran.append(self.interval))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    watch.cmd_watch(argparse.Namespace(interval=7, once=False, plain=False))
    assert ran == [7]
    assert not hasattr(watch, "tui_watch")
    assert tui_app.default_provider.__module__ == "pl.tui.app"


async def test_odd_card_ids_none_titles_duplicates_and_render_errors_do_not_kill_the_app(monkeypatch):
    data = fake_data()
    gh = _card("org/repo#12", None, "Plan for review", kind="review")
    data["snapshot"]["rows"] += [gh, dict(gh), _card("org/repo#13", "gh card", "Spec ready")]
    epoch = _card("epoch-card", "epoch card", "Plan for review", kind="review")
    epoch["card"]["updated_at"] = 1790000000
    objtags = _card("objtags-card", "objtags card", "Plan for review", kind="review")
    objtags["card"]["tags"] = [{"name": "bug"}]
    data["snapshot"]["rows"] += [epoch, objtags]
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.is_running and "render failed" not in str(app.query_one("#header").render())
        table = app.query_one("#needs-table")
        for i in range(table.row_count):
            table.move_cursor(row=i)
            await pilot.pause()
        assert app.is_running and "render failed" not in str(app.query_one("#header").render())
        await pilot.press("2")
        await pilot.pause()
        assert "epoch card" in screen_text(app)
        await pilot.press("4")
        await pilot.pause()
        assert "org/repo" in screen_text(app)
        monkeypatch.setattr(type(app.query_one("#tabs").parent.query_one("PrsView")), "show",
                            lambda self, d: (_ for _ in ()).throw(ValueError("boom")))
        app.refresh_data()
        await settle(pilot)
        assert app.is_running
        assert "boom" in str(app.query_one("#header").render())


async def test_empty_sparklines_draw_a_baseline_not_a_solid_block():
    data = fake_data()
    data["daily"] = {"specs": [0], "plans": []}
    data["pr_activity"] = {"opened": [], "merged": []}
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        text = screen_text(app)
        assert "█" not in text
        assert "no data yet" in text


async def test_real_sparkline_series_still_draws_bars():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "█" in screen_text(app)


async def test_other_views_skip_the_redraw_when_their_data_is_unchanged(monkeypatch):
    """WP24: identical data on the next pass leaves the tables and the pipeline columns alone."""
    import copy
    prov = Provider()
    app = PlApp(snapshot_provider=lambda: copy.deepcopy(prov()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        cols = list(app.query_one("#pipeline-scroll").children)
        clears = {tid: [] for tid in ("waits", "loops-table", "activity-table")}
        for tid, calls in clears.items():
            t = app.query_one(f"#{tid}")
            real = t.clear
            monkeypatch.setattr(t, "clear", lambda *a, _r=real, _c=calls, **k: (_c.append(1), _r(*a, **k))[1])
        app.refresh_data()
        await settle(pilot)
        assert {k: len(v) for k, v in clears.items()} == {k: 0 for k in clears}
        assert list(app.query_one("#pipeline-scroll").children) == cols
        prov.data = fake_data(title="A new title")
        app.refresh_data()
        await settle(pilot)
        assert list(app.query_one("#pipeline-scroll").children) != cols


@pytest.mark.parametrize("row,tab,selected", [(0, "prs", None), (1, "needs", "spec0001aaaa"), (2, "needs", "4a000001aaaa"),
                                              (3, "needs", "pr:api#1576"), (4, "needs", "pr:frontend#1117"),
                                              (5, "needs", "c0000003aaaa")])
async def test_decide_next_rows_are_selectable(monkeypatch, row, tab, selected):
    monkeypatch.setattr(C, "GATES", {"spec": True})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#decide").focus()
        await pilot.press(*["down"] * row, "enter")
        await pilot.pause()
        assert app.active_tab == tab
        if selected:
            assert app.query_one("NeedsView")._current()[0] == selected


async def test_decide_next_row_with_zero_count_still_navigates():
    app = PlApp(snapshot_provider=Provider())   # spec gate off: no specs waiting
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#decide").focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.active_tab == "needs"


async def _open_pr(pilot, app, url="https://github.com/o/frontend/pull/1200"):
    await settle(pilot)
    await pilot.press("5")
    await settle(pilot)
    t = app.query_one("#prs-table")
    t.move_cursor(row=t.get_row_index(url))
    await settle(pilot)


async def test_pr_overview_shows_what_the_pr_is_and_whether_to_merge(monkeypatch):
    """WP33: one gh pr view per selection (profile gh env), cached; overview, intent and verdict in the right pane."""
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    monkeypatch.setattr(C, "GH_CONFIG_DIR", C.CONFIG_DIR or __import__("pathlib").Path("/nowhere/gh"))
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        text = screen_text(app)
        for s in ("feat: merge me first", "octo", "feat/x", "development", "+120 -7", "3 files",
                  "pl:merge-ready", "2 passed", "APPROVED", "MERGEABLE", "ready to merge",
                  "What https://github.com/o/frontend/pull/1200 was meant to do"):
            assert s in text, s
        views = [(a, env) for a, env in gh.calls if a[:4] == ["gh", "pr", "view", "https://github.com/o/frontend/pull/1200"]]
        assert len(views) == 1 and not gh.merges()
        assert views[0][1]["GH_CONFIG_DIR"] == str(C.GH_CONFIG_DIR)
        t = app.query_one("#prs-table")
        t.move_cursor(row=t.get_row_index("https://github.com/o/frontend/pull/1201"))
        await settle(pilot)
        t.move_cursor(row=t.get_row_index("https://github.com/o/frontend/pull/1200"))
        await settle(pilot)
        assert sum(1 for a, _ in gh.calls if a[3:4] == ["https://github.com/o/frontend/pull/1200"]) == 1   # cached


async def test_m_merges_only_after_confirmation(monkeypatch):
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("m")
        await settle(pilot)
        assert "development" in screen_text(app) and "squash" in screen_text(app)   # the modal names base and method
        assert gh.merges() == []
        await pilot.press("n")
        await settle(pilot)
        assert gh.merges() == [] and len(app.screen_stack) == 1
        await pilot.press("m")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", "https://github.com/o/frontend/pull/1200", "--squash"]]


async def test_not_ready_pr_is_refused_on_m_and_confirmed_on_M(monkeypatch):
    gh = FakeGh(_view(checks=("SUCCESS", "FAILURE")))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "1 check failed" in screen_text(app)
        await pilot.press("m")
        await settle(pilot)
        assert len(app.screen_stack) == 1 and gh.merges() == []
        await pilot.press("M")
        await settle(pilot)
        assert len(app.screen_stack) == 2 and gh.merges() == []
        await pilot.press("enter")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", "https://github.com/o/frontend/pull/1200", "--squash"]]


async def test_repo_without_squash_offers_a_merge_commit(monkeypatch):
    gh = FakeGh(_view(), merge=(1, "", "GraphQL: Squash merges are not allowed on this repository. (mergePullRequest)"))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("m", "y")
        await settle(pilot)
        url = "https://github.com/o/frontend/pull/1200"
        assert gh.merges() == [["gh", "pr", "merge", url, "--squash"]]   # no automatic --merge retry
        assert any("press m again" in str(n.message) for n in app._notifications)
        gh.merge = (0, "merged", "")
        await pilot.press("m")
        await settle(pilot)
        assert "merge commit" in screen_text(app) and len(app.screen_stack) == 2 and len(gh.merges()) == 1
        await pilot.press("y")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", url, "--squash"], ["gh", "pr", "merge", url, "--merge"]]


def _notes(app):
    return " | ".join(str(n.message) for n in app._notifications)


async def _refused(pilot, app, gh):
    await pilot.press("m")
    await settle(pilot)
    return len(app.screen_stack) == 1 and gh.merges() == []


async def test_changes_requested_is_not_ready(monkeypatch):
    gh = FakeGh(_view(review="CHANGES_REQUESTED"))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: a reviewer asked for changes" in screen_text(app)
        assert await _refused(pilot, app, gh)


async def test_missing_merge_ready_label_is_refused(monkeypatch):
    gh = FakeGh(_view(labels=("pl:ready-for-review",)))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: no merge-ready label" in screen_text(app)
        assert await _refused(pilot, app, gh)


async def test_a_renamed_merge_ready_label_is_the_only_one_that_counts(monkeypatch):
    monkeypatch.setattr(C, "CODE_HOST", {"owner": None, "labels": {"merge_ready": "ship-it"}})
    gh = FakeGh(_view(labels=("pl:merge-ready",)))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: no merge-ready label" in screen_text(app)
        assert await _refused(pilot, app, gh)
    gh = FakeGh(_view(labels=("ship-it",)))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready" not in screen_text(app) and "ready to merge" in screen_text(app)


async def test_a_pr_with_no_checks_is_ready_and_says_so(monkeypatch):
    gh = FakeGh(_view(checks=()))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "ready to merge (no checks ran)" in screen_text(app)
        await pilot.press("m")
        await settle(pilot)
        assert len(app.screen_stack) == 2


async def test_a_pr_link_that_is_not_a_github_pr_url_is_never_passed_to_gh(monkeypatch):
    data = fake_data()
    data["snapshot"]["rows"].append(_pr("frontend", 1400, "feat: sneaky", "merge"))
    data["snapshot"]["rows"][-1]["pr"]["url"] = "--admin"
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app, url="--admin")
        assert "not a GitHub pull request link" in screen_text(app)
        for key in ("m", "M"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1
        assert "not a GitHub pull request link" in _notes(app)
        assert not any("--admin" in a for a, _ in gh.calls)


async def test_a_failed_read_is_not_cached(monkeypatch):
    gh = FakeGh(None)
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "no gh here" in screen_text(app)
        url = "https://github.com/o/frontend/pull/1200"
        assert sum(1 for a, _ in gh.calls if a[3:4] == [url]) == 1   # shown once, not retried in a loop
        gh.view = _view()
        t = app.query_one("#prs-table")
        t.move_cursor(row=t.get_row_index("https://github.com/o/frontend/pull/1201"))
        await settle(pilot)
        t.move_cursor(row=t.get_row_index(url))
        await settle(pilot)
        assert sum(1 for a, _ in gh.calls if a[3:4] == [url]) == 2 and "ready to merge" in screen_text(app)


async def test_a_rate_limit_note_is_not_cached(monkeypatch):
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    lim = type("Lim", (), {"note": "GitHub asked pl to wait"})()
    monkeypatch.setattr(tui_prs.github, "limited", lambda: lim)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "GitHub asked pl to wait" in screen_text(app)
        monkeypatch.setattr(tui_prs.github, "limited", lambda: None)
        t = app.query_one("#prs-table")
        t.move_cursor(row=t.get_row_index("https://github.com/o/frontend/pull/1201"))
        await settle(pilot)
        t.move_cursor(row=t.get_row_index("https://github.com/o/frontend/pull/1200"))
        await settle(pilot)
        assert "ready to merge" in screen_text(app)


async def test_squash_fallback_on_a_forced_merge_says_press_M(monkeypatch):
    gh = FakeGh(_view(checks=("FAILURE",)), merge=(1, "", "GraphQL: Squash merges are not allowed on this repository."))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("M", "enter")
        await settle(pilot)
        assert "press M again" in _notes(app) and "press m again" not in _notes(app)


async def test_t_on_a_failed_card_asks_then_retries_it_off_the_ui_thread(monkeypatch):
    got = []
    monkeypatch.setattr(tui_pipeline, "retry", lambda cid: got.append((cid, threading.current_thread())) or [f"{cid[:8]}: reset"])
    data = fake_data()
    data["snapshot"]["rows"].append({**_card("dead0001aaaa", "A card whose spec agent died", "Inbox", kind="needs"), "failed": True})
    data["snapshot"]["rows"].append(_card("wait0001aaaa", "An agent waiting on a person", "Inbox", kind="needs"))
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "spec0001aaaa").focus()
        await pilot.pause()
        await pilot.press("t")          # not a failed card: nothing to retry, no dialog
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "wait0001aaaa").focus()
        await pilot.pause()
        await pilot.press("t")          # needs you, but waiting on a person: its agent did not fail
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "dead0001aaaa").focus()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert len(app.screen_stack) == 2   # the confirm line
        await pilot.press("n")
        await settle(pilot)
        assert got == []
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == ["dead0001aaaa"] and got[0][1] is not threading.main_thread()


async def test_m_on_a_live_agent_asks_then_moves_it_off_the_ui_thread(monkeypatch):
    from pl import move_agent
    got = []
    monkeypatch.setattr(move_agent, "move_card", lambda cid, target: got.append((cid, target, threading.current_thread())) or "moved")
    data = fake_data()
    live = _card("live0001aaaa", "A card with a live agent", "Inbox", kind="working")
    live["worker"] = {"window": "@3", "pane": "%3", "profile": "acme", "session_id": "s"}
    data["snapshot"]["rows"].append(live)
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await pilot.pause()
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "spec0001aaaa").focus()
        await pilot.pause()
        await pilot.press("m")          # no agent window: nothing to move, no dialog
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        next(b for b in app.query(tui_pipeline.CardBox) if b.row["card"]["id"] == "live0001aaaa").focus()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert len(app.screen_stack) == 2   # the confirm line
        await pilot.press("n")
        await settle(pilot)
        assert got == []
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert [(g[0], g[1]) for g in got] == [("live0001aaaa", None)] and got[0][2] is not threading.main_thread()
    assert not hasattr(move_agent, "other_account")   # the target is picked off the UI thread, by move_card


async def test_dashboard_header_shows_free_memory_and_a_red_warning_when_low():
    data = fake_data()
    data["memory"] = {"free": 12 * 1024 ** 3, "total": 32 * 1024 ** 3, "low": False}
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "memory: 12.0 GB free" in screen_text(app)
        assert "LOW MEMORY" not in screen_text(app)
    data["memory"] = {"free": 2 * 1024 ** 3, "total": 32 * 1024 ** 3, "low": True}
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        text = screen_text(app)
        assert "memory: 2.0 GB free" in text and "LOW MEMORY: new agents held" in text
        bar = app.query_one("#window-bar").render()
        assert any("red" in str(s.style) for s in bar.spans if "LOW" in bar.plain[s.start:s.end])


async def test_needs_you_shows_alerts_first_and_k_acknowledges(monkeypatch):
    from pl import alerts
    from pl.tui import needs as tui_needs
    acked = []
    monkeypatch.setattr(alerts, "ack", lambda key: acked.append(key) or True)
    data = fake_data()
    data["alerts"] = [{"key": "stage_failed:abcd1234:spec", "severity": "high", "title": "Card abcd1234: the spec agent died 3 times",
                       "fix": "pl retry abcd1234", "first_seen": NOW.timestamp() - 7200, "last_seen": NOW.timestamp(), "count": 4}]
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await pilot.pause()
        text = screen_text(app)
        for s in ("ALERTS 1", "high 2h", "pl retry abcd1234", "×4"):
            assert s in text, s
        assert text.index("ALERTS 1") < text.index("PLANS TO REVIEW")
        assert app.query_one(tui_needs.NeedsView)._current()[0] == "alert:stage_failed:abcd1234:spec"
        await pilot.press("k")
        await pilot.pause()
        assert acked == ["stage_failed:abcd1234:spec"] and "acked" in screen_text(app)
        data["alerts"] = []                       # resolved: the dispatcher's next read leaves it out
        app.refresh_data()
        await settle(pilot)
        assert "ALERTS" not in screen_text(app)
