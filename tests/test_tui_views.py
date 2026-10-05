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
from textual.widgets import TabbedContent

from pl import cli, trackers, watch
from pl import config as C
from pl.tui import app as tui_app
from pl.tui import cards as tui_cards
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
                  "tokens", "counting…", "exceeded", "legacy", "dispatcher running"):
            assert s in text, s


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


async def test_small_terminal_every_tab():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        for key in "123456789":
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running and "render failed" not in str(app.query_one("#header").render())


async def test_provider_only_runs_off_the_ui_thread():
    prov = Provider()
    app = PlApp(snapshot_provider=prov)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.refresh_data()
        await settle(pilot)
    assert len(prov.threads) >= 2
    assert all(t is not threading.main_thread() for t in prov.threads)


async def test_card_markup_is_shown_as_plain_text(monkeypatch):
    _fake_card_read(monkeypatch, [])
    title = "[bold]x[/bold] [link=https://evil.example]y[/link]"
    pr_title = "[red]pr[/red] [link=https://evil.example]z[/link]"
    app = PlApp(snapshot_provider=Provider(fake_data(title=title, pr_title=pr_title)))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
        table_line = next(l for l in screen_text(app).splitlines() if "4a000001 " in l)
        assert "[bold]x[/bold]" in table_line.split("││")[0]      # the list, not only the detail pane
        assert "[bold]x[/bold]" in str(app.query_one("#cards-detail").render())        # card detail pane
        await pilot.press("5")
        await pilot.pause()
        assert "[red]pr[/red] [link=https://evil.example]z[/link]" in screen_text(app)


async def test_pipeline_keys_jump_and_open_off_the_ui_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(tui_cards, "jump_to_window", lambda win: calls.append(("jump", win, threading.current_thread())) or "ok")
    _fake_card_read(monkeypatch, [])
    opened = []
    monkeypatch.setattr(C, "TRACKER", {"type": "mcp", "board_id": "b-1", "server": {"command": "none"},
                                       "card_url": "https://boards.example/{board_id}?cardId={item_id}"})
    trackers.reset()
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
        await pilot.press("w")
        await pilot.press("c")
        await settle(pilot)
    assert calls and calls[0][2] is not threading.main_thread()
    assert opened == ["https://boards.example/b-1?cardId=4a000001aaaa"]


async def test_c_opens_only_web_links(monkeypatch):
    class T:
        url = staticmethod(lambda cid: urls.pop(0))
    urls = ["file:///etc/passwd", "HTTPS://example.com/card/1"]
    monkeypatch.setattr(tui_cards.trackers, "get", lambda kind: T)
    _fake_card_read(monkeypatch, [])
    opened = []
    monkeypatch.setattr(C, "TRACKER", {"type": "mcp", "board_id": "b-1", "server": {"command": "none"},
                                       "card_url": "https://boards.example/{board_id}?cardId={item_id}"})
    trackers.reset()
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
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
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.is_running and "render failed" not in str(app.query_one("#header").render())
        await _open_cards(pilot, app)
        table = app.query_one("#cards-table")
        for i in range(table.row_count):
            table.move_cursor(row=i)
            await pilot.pause()
        assert app.is_running and "render failed" not in str(app.query_one("#header").render())
        assert "epoch card" in screen_text(app)
        assert "repo#12" in screen_text(app) and "repo#13" in screen_text(app)   # GitHub ids show as repo#n
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
    """WP24: identical data on the next pass leaves the tables alone."""
    import copy
    prov = Provider()
    app = PlApp(snapshot_provider=lambda: copy.deepcopy(prov()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        clears = {tid: [] for tid in ("waits", "loops-table", "activity-table", "cards-table")}
        for tid, calls in clears.items():
            t = app.query_one(f"#{tid}")
            real = t.clear
            monkeypatch.setattr(t, "clear", lambda *a, _r=real, _c=calls, **k: (_c.append(1), _r(*a, **k))[1])
        app.refresh_data()
        await settle(pilot)
        assert {k: len(v) for k, v in clears.items()} == {k: 0 for k in clears}


@pytest.mark.parametrize("row,tab,selected", [(0, "prs", None), (1, "cards", "spec0001aaaa"), (2, "cards", "4a000001aaaa"),
                                              (3, "prs", None), (4, "prs", None), (5, "cards", "c0000003aaaa")])
async def test_decide_next_rows_are_selectable(monkeypatch, row, tab, selected):
    """Card groups open the Pipeline with only the cards that need you, on the group's first card; PR groups open
    Pull requests."""
    monkeypatch.setattr(C, "GATES", {"spec": True})
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#decide").focus()
        await pilot.press(*["down"] * row, "enter")
        await settle(pilot)
        assert app.active_tab == tab
        view = app.query_one(tui_cards.CardsView)
        assert view.only_mine == (tab == "cards")
        if selected:
            assert view._current()[0] == selected
            assert "d0000004aaaa" not in view._rows   # PR open needs nobody: filtered out


async def test_decide_next_row_with_zero_count_still_navigates(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())   # spec gate off: no specs waiting
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#decide").focus()
        await pilot.press("down", "enter")
        await settle(pilot)
        assert app.active_tab == "cards" and app.query_one(tui_cards.CardsView).only_mine


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
    monkeypatch.setattr(tui_cards, "retry", lambda cid: got.append((cid, threading.current_thread())) or [f"{cid[:8]}: reset"])
    _fake_card_read(monkeypatch, [])
    data = fake_data()
    data["snapshot"]["rows"].append({**_card("dead0001aaaa", "A card whose spec agent died", "Inbox", kind="needs"), "failed": True})
    data["snapshot"]["rows"].append(_card("wait0001aaaa", "An agent waiting on a person", "Inbox", kind="needs"))
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "spec0001aaaa")
        await pilot.press("t")          # not a failed card: nothing to retry, no dialog
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        await _open_cards(pilot, app, "wait0001aaaa")
        await pilot.press("t")          # needs you, but waiting on a person: its agent did not fail
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        await _open_cards(pilot, app, "dead0001aaaa")
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
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "spec0001aaaa")
        await pilot.press("m")          # no agent window: nothing to move, no dialog
        await settle(pilot)
        assert got == [] and len(app.screen_stack) == 1
        await _open_cards(pilot, app, "live0001aaaa")
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


async def test_alerts_have_their_own_tab_and_k_acknowledges(monkeypatch):
    from pl import alerts
    from pl.tui import alerts as tui_alerts
    acked = []
    monkeypatch.setattr(alerts, "ack", lambda key: acked.append(key) or True)
    data = fake_data()
    data["alerts"] = [{"key": "stage_failed:abcd1234:spec", "severity": "high", "title": "Card abcd1234: the spec agent died 3 times",
                       "fix": "pl retry abcd1234", "first_seen": NOW.timestamp() - 7200, "last_seen": NOW.timestamp(), "count": 4}]
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        assert "ALERTS" not in screen_text(app)   # the Pipeline lists only cards; alerts live on their own tab
        assert "4 Alerts 1" in screen_text(app)
        await pilot.press("4")
        await pilot.pause()
        assert app.active_tab == "alerts"
        text = screen_text(app)
        for s in ("high 2h", "pl retry abcd1234", "×4"):
            assert s in text, s
        assert app.query_one(tui_alerts.AlertsView)._current()[0] == "stage_failed:abcd1234:spec"
        await pilot.press("k")
        await pilot.pause()
        assert acked == ["stage_failed:abcd1234:spec"] and "acked" in screen_text(app)
        data["alerts"] = []                       # resolved: the dispatcher's next read leaves it out
        app.refresh_data()
        await settle(pilot)
        assert "none open" in screen_text(app) and "4 Alerts 0" in screen_text(app)


def test_an_old_alert_title_with_a_cut_github_id_shows_repo_and_number():
    from pl.tui import alerts as tui_alerts
    old = {"key": "pr_waiting:your-org/your-repo#43", "severity": "warn", "title": "Card your-org: its PR waits over 24 h",
           "fix": "review and merge it, or move the card on", "first_seen": NOW.timestamp(), "count": 1}
    assert "Card your-repo#43: its PR waits" in _cells(old, tui_alerts)
    assert "Card your-repo#43: its PR waits" in tui_alerts.detail(old).plain
    failed = {**old, "key": "stage_failed:your-org/your-repo#44:spec", "title": "Card your-org: the spec agent died 3 times"}
    assert "Card your-repo#44: the spec agent died" in _cells(failed, tui_alerts)
    board = {**old, "key": "pr_waiting:4a000001aaaa", "title": "Card 4a000001: its PR waits over 24 h"}
    assert "Card 4a000001: its PR waits" in _cells(board, tui_alerts)


def _cells(a, mod):
    return " ".join(t.plain for t in mod.cells(a))


async def test_pipeline_filter_with_nothing_waiting_says_so_and_keys_only_notify(monkeypatch):
    _fake_card_read(monkeypatch, [])
    data = fake_data()
    data["snapshot"]["rows"] = [r for r in data["snapshot"]["rows"]
                                if r.get("card") and r.get("col") not in ("Spec ready", "Plan for review", "Manual")]
    assert data["snapshot"]["rows"]   # cards remain, none needs a person
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        await pilot.press("n")
        await settle(pilot)
        text = screen_text(app)
        assert "Nothing needs you." in text and "select a row" in text
        assert str(app.query_one(TabbedContent).get_tab("cards").label) == "2 Pipeline 1"   # no "need you" part
        assert "PR OPEN" not in text
        for key in ("a", "x", "o", "enter", "down", "up"):
            await pilot.press(key)
            await pilot.pause()
        assert len(app.screen_stack) == 1 and app.is_running
        assert any("select a spec" in str(n.message) for n in app._notifications)


# ---------- the Pipeline tab: every card as a list, its text and actions ----------

def _fake_card_read(monkeypatch, reads):
    from pl.tui import cards as tui_cards

    def read(cid):
        reads.append((cid, threading.current_thread()))
        return {"id": cid, "title": "t", "updated_at": "x", "description": f"[bold]the text of {cid[:8]}[/bold]"}
    monkeypatch.setattr(tui_cards, "card", read)


async def _open_cards(pilot, app, cid=None):
    from pl.tui import cards as tui_cards
    await settle(pilot)
    await pilot.press("2")
    await settle(pilot)
    if cid is not None:
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index(cid))
        await settle(pilot)
    return app.query_one(tui_cards.CardsView)


async def test_pipeline_lists_every_card_by_column_with_its_text(monkeypatch):
    reads = []
    _fake_card_read(monkeypatch, reads)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app, "4a000001aaaa")
        assert app.active_tab == "cards"
        text = screen_text(app)
        assert "2 Pipeline 5 · 3 need you" in text and "Kanban" not in text and "Needs you" not in text
        heads = ["SPEC READY 1", "PLAN FOR REVIEW 2", "MANUAL 1", "PR OPEN 1"]
        for s in heads + ["4a000001", "60000002", "c0000003", "d0000004", "spec0001"]:
            assert s in text, s
        assert [text.index(h) for h in heads] == sorted(text.index(h) for h in heads)   # board order
        assert "INBOX" not in text and "DONE" not in text   # empty columns and Done are left out
        assert "column   Plan for review" in text
        assert "[bold]the text of 4a000001[/bold]" in text   # card text is data, never markup
        assert reads and all(t is not threading.main_thread() for _, t in reads)
        assert view._current()[0] == "4a000001aaaa"


async def test_pipeline_a_and_x_review_only_specs_and_plans(monkeypatch):
    from pl.tui import needs as tui_needs
    from pl.tui.review import ConfirmScreen, ReviewScreen
    _fake_card_read(monkeypatch, [])
    pushed = []
    monkeypatch.setattr(tui_needs, "ReviewScreen", lambda *a, **kw: pushed.append((a, kw)) or ConfirmScreen("review"))
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "d0000004aaaa")   # PR open: nothing to review
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index("4a000001aaaa"))
        await settle(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Approve the plan for 4a000001" in screen_text(app)
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert pushed and pushed[0][0][:2] == ("plan", "4a000001aaaa") and pushed[0][1]["notes_first"]
        assert ReviewScreen is not None


async def test_pipeline_v_moves_a_card_to_another_column_after_asking(monkeypatch):
    from pl.tui import cards as tui_cards
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    moved = []
    monkeypatch.setattr(tui_cards, "move_to", lambda cid, col: moved.append((cid, col, threading.current_thread())) or cid)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "c0000003aaaa")   # Manual
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, tui_cards.ColumnPicker)
        assert "Manual" not in app.screen.columns and "Done" in app.screen.columns   # not its own column
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and moved == []
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("down", "enter")   # Inbox, Spec ready: the second one
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen), app.screen
        assert "from Manual to Spec ready" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [m[:2] for m in moved] == [("c0000003aaaa", "Spec ready")] and moved[0][2] is not threading.main_thread()


async def test_pipeline_t_retries_and_enter_opens_the_whole_card(monkeypatch):
    _fake_card_read(monkeypatch, [])
    got = []
    monkeypatch.setattr(tui_cards, "retry", lambda cid: got.append(cid) or ["reset"])
    data = fake_data()
    data["snapshot"]["rows"].append({**_card("dead0001aaaa", "A card whose spec agent died", "Inbox", kind="needs"), "failed": True})
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "dead0001aaaa")
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert got == ["dead0001aaaa"]
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, tui_cards.CardScreen)


# ---------- the Pipeline's attention marker and filter ----------

def test_needs_me_reuses_the_needs_you_rules(monkeypatch):
    from pl.tui.needs import needs_me
    monkeypatch.setattr(C, "GATES", {"spec": True})
    rows = {r["card"]["id"]: r for r in fake_data()["snapshot"]["rows"] if r.get("card")}
    assert {cid for cid, r in rows.items() if needs_me(r)} == {"spec0001aaaa", "4a000001aaaa", "60000002aaaa", "c0000003aaaa"}
    assert needs_me({**_card("dead0001aaaa", "died", "Inbox"), "failed": True})
    assert needs_me(_card("wait0001aaaa", "an agent waits on a person", "Inbox", kind="needs"))
    assert not needs_me(_card("busy0001aaaa", "working", "Inbox", kind="working"))
    assert not needs_me(_pr("frontend", 1117, "a PR", "decide"))   # PRs live on the Pull requests tab
    monkeypatch.setattr(C, "GATES", {})
    assert not needs_me(rows["spec0001aaaa"])   # spec gate off: the planner takes the spec, nobody waits


def _attention_data():
    data = fake_data()
    data["snapshot"]["rows"].append({**_card("dead0001aaaa", "A card whose spec agent died", "Inbox", kind="needs"), "failed": True})
    return data


MINE = {"dead0001aaaa", "4a000001aaaa", "60000002aaaa", "c0000003aaaa"}   # spec gate off: the spec waits on nobody


async def test_pipeline_marks_the_cards_that_need_you_and_counts_them_on_the_tab(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_attention_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        t = app.query_one("#cards-table")
        marked = {cid for cid in view._rows if t.get_row(cid)[2].plain.startswith("! ")}
        assert marked == MINE
        assert any("red" in str(sp.style) for sp in t.get_row("c0000003aaaa")[2].spans)
        assert "2 Pipeline 6 · 4 need you" in screen_text(app)
        assert "4 need you" in t.border_title and "n only those" in t.border_title


async def test_n_shows_only_the_cards_that_need_you_and_n_again_shows_all(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_attention_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app, "60000002aaaa")
        t = app.query_one("#cards-table")
        await pilot.press("n")
        await settle(pilot)
        assert set(view._rows) == MINE and view._current()[0] == "60000002aaaa"   # the selected card stays selected
        assert "needs you only" in t.border_title and "4 of 6 cards" in t.border_title
        text = screen_text(app)
        assert "PR OPEN" not in text and "SPEC READY" not in text and "INBOX 1" in text
        app.refresh_data()
        await settle(pilot)
        assert set(view._rows) == MINE   # the filter stays on across refreshes
        await pilot.press("n")
        await settle(pilot)
        assert len(view._rows) == 6 and "needs you only" not in t.border_title


# ---------- the Pipeline's drop, hand off, done, show Done and undo drop keys ----------

def _called(monkeypatch, name, out):
    """Replace a backend function in the cards module: record its arguments and thread, answer out."""
    got = []
    monkeypatch.setattr(tui_cards, name, lambda *a: got.append((a, threading.current_thread())) or out)
    return got


def _done_row(cid, title, **meta):
    r = _card(cid, title, "Done")
    r["card"]["metadata"].update(meta)
    return r


def _with_done():
    data = fake_data()
    data["snapshot"]["done"] = [_done_row("f0000001aaaa", "A shipped card", done_at=_iso(1)),
                                _done_row("f0000002aaaa", "A card nobody needs", dropped_at=_iso(2),
                                          drop_reason="[red]customer cancelled[/red]", dropped_from="Spec ready")]
    return data


def test_the_pipeline_keys_do_not_clash_with_the_console_keys():
    app_keys = {b.key for b in PlApp.BINDINGS}
    view_keys = [b.key for b in tui_cards.CardsView.BINDINGS]
    assert len(view_keys) == len(set(view_keys))
    assert not ({"d", "h", "f", "z", "u", "e"} & app_keys) and {"d", "h", "f", "z", "u", "e"} <= set(view_keys)
    assert "D" in app_keys and "D" not in view_keys   # D stays the dispatcher's key


async def test_d_asks_for_a_reason_then_confirms_then_drops_off_the_ui_thread(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "drop", ["dropped c0000003"])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "c0000003aaaa")
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, tui_cards.ReasonScreen)
        await pilot.press("escape")      # esc: nothing dropped, no confirm
        await pilot.pause()
        assert len(app.screen_stack) == 1 and got == []
        await pilot.press("d")
        await pilot.pause()
        for ch in "not needed":
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Drop c0000003" in app.screen.message
        assert "not needed" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")       # no reason given: None
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("c0000003aaaa", "not needed"), ("c0000003aaaa", None)]
    assert all(g[1] is not threading.main_thread() for g in got)
    assert any("dropped c0000003" in str(n.message) for n in app._notifications)


async def test_d_shows_a_refusal_and_the_console_runs_on(monkeypatch):
    _fake_card_read(monkeypatch, [])

    def refuse(*a):
        raise SystemExit("pl drop: its run agent could not be stopped")
    monkeypatch.setattr(tui_cards, "drop", refuse)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "c0000003aaaa")
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert app.is_running and any("not dropped: pl drop: its run agent" in str(n.message) for n in app._notifications)


async def test_h_hands_a_card_to_manual_after_asking_and_refuses_one_already_there(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "move_to", "spec0001aaaa")
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "c0000003aaaa")   # already in Manual
        await pilot.press("h")
        await settle(pilot)
        assert len(app.screen_stack) == 1 and got == []
        assert any("already in Manual" in str(n.message) for n in app._notifications)
        await _open_cards(pilot, app, "spec0001aaaa")
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "from Spec ready to Manual" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("spec0001aaaa", "Manual")] and got[0][1] is not threading.main_thread()


async def test_f_marks_a_card_done_after_asking(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "done", ["done d0000004"])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "d0000004aaaa")
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "done" in app.screen.message.lower()
        await pilot.press("n")
        await settle(pilot)
        assert got == []
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("d0000004aaaa",)] and got[0][1] is not threading.main_thread()
    assert any("done d0000004" in str(n.message) for n in app._notifications)


async def test_z_shows_done_and_dropped_cards_without_a_new_refresh(monkeypatch):
    _fake_card_read(monkeypatch, [])
    provider = Provider(_with_done())
    app = PlApp(snapshot_provider=provider)
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        assert "DONE" not in screen_text(app) and "f0000001aaaa" not in view._rows
        calls = len(provider.threads)
        await pilot.press("z")
        await settle(pilot)
        assert len(provider.threads) == calls   # no extra board call: the last refresh already has them
        t = app.query_one("#cards-table")
        text = screen_text(app)
        assert "DONE 2" in text
        assert t.get_row("f0000001aaaa")[2].plain.strip() == "done"
        assert t.get_row("f0000002aaaa")[2].plain.strip() == "dropped"
        assert "[red]customer cancelled[/red]" in t.get_row("f0000002aaaa")[3].plain   # card text is data
        assert "2 Pipeline 5 · 3 need you" in text   # the tab count leaves Done out
        assert "z hides Done" in t.border_title
        await _open_cards(pilot, app, "f0000002aaaa")
        assert "from Spec ready: [red]customer cancelled[/red]" in screen_text(app)   # the detail pane says why
        await pilot.press("z")
        await settle(pilot)
        assert "f0000001aaaa" not in view._rows and "Done is hidden" in t.border_title


async def test_u_undoes_a_drop_only_on_a_dropped_card(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "undrop", ["undropped f0000002"])
    app = PlApp(snapshot_provider=Provider(_with_done()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        await pilot.press("z")
        await settle(pilot)
        for cid in ("spec0001aaaa", "f0000001aaaa"):   # not dropped: a notice, no dialog
            await _open_cards(pilot, app, cid)
            await pilot.press("u")
            await settle(pilot)
            assert len(app.screen_stack) == 1
        await _open_cards(pilot, app, "f0000002aaaa")
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "back to Spec ready" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("f0000002aaaa",)] and got[0][1] is not threading.main_thread()


async def test_drop_hand_off_and_done_refuse_a_card_already_in_done(monkeypatch):
    _fake_card_read(monkeypatch, [])
    got = [_called(monkeypatch, n, ["x"]) for n in ("drop", "done", "move_to")]
    app = PlApp(snapshot_provider=Provider(_with_done()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        await pilot.press("z")
        await settle(pilot)
        await _open_cards(pilot, app, "f0000001aaaa")
        for key in ("d", "f", "h"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1, key
    assert got == [[], [], []]


async def test_the_tab_keys_are_digits_in_strip_order():
    from pl.tui.chrome import TAB_KEYS, TABS
    assert [(TAB_KEYS[tid], name) for tid, name in TABS] == [
        ("0", "Assistant"), ("1", "Dashboard"), ("2", "Pipeline"), ("3", "Ideas"), ("4", "Alerts"),
        ("5", "Pull requests"), ("6", "Loops"), ("7", "Activity"), ("8", "Settings"), ("9", "Background")]


def _github_data():
    """fake_data with GitHub-style card ids: every id starts with the same owner."""
    data = fake_data()
    for r in data["snapshot"]["rows"]:
        if r.get("card"):
            n = {"spec0001aaaa": 41, "4a000001aaaa": 42, "60000002aaaa": 43, "c0000003aaaa": 44, "d0000004aaaa": 45}[r["card"]["id"]]
            r["card"]["id"] = f"your-org/your-repo#{n}"
    return data


async def test_github_card_ids_show_as_repo_and_number_on_every_list(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_github_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        text = screen_text(app)
        assert all(f"your-repo#{n}" in text for n in (41, 42, 43, 44, 45)) and "your-org" not in text.split("card     ")[0]


def _cursor(app, table):
    t = app.query_one(table)
    return t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value


async def test_pipeline_down_and_up_skip_the_column_headings(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        assert _cursor(app, "#cards-table") == "spec0001aaaa"   # the first row selected is a card
        await pilot.press("down")   # onto PLAN FOR REVIEW 2: its first card instead
        await settle(pilot)
        assert _cursor(app, "#cards-table") == "4a000001aaaa"
        assert "column   Plan for review" in screen_text(app)
        await pilot.press("up")     # onto the heading: the previous column's last card
        await settle(pilot)
        assert _cursor(app, "#cards-table") == "spec0001aaaa"
        await pilot.press("up")     # the top heading: the first card stays selected
        await settle(pilot)
        assert _cursor(app, "#cards-table") == "spec0001aaaa"
        text = screen_text(app)
        assert "column   Spec ready" in text and "select a row" not in text


async def test_pull_requests_down_skips_headings_and_none_rows(monkeypatch):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        t = app.query_one("#prs-table")
        first = _cursor(app, "#prs-table")
        assert first.startswith("https://")
        seen = [first]
        for _ in range(8):
            await pilot.press("down")
            await settle(pilot)
            seen.append(_cursor(app, "#prs-table"))
        assert all(k.startswith("https://") for k in seen), seen
        assert seen[-1] == seen[-2] and t.row_count > len(set(seen))


# ---------- the Assistant tab ----------

class FakeAssistant:
    """Stands in for pl.assistant in the tab: records calls, raises when told to."""

    def __init__(self, monkeypatch):
        from pl import agents, assistant
        self.ensured, self.sent, self.screens, self.resets, self.modes, self.fail = 0, [], 0, 0, [], None
        self.state = {"window": "@7", "pane": "%7", "mode": "chat"}
        self.dead, self.ready, self.warn, self.reg, self.lines = False, None, None, {}, None
        monkeypatch.setattr(agents, "registry", lambda: self.reg)
        monkeypatch.setattr(assistant, "restart_if_updated", lambda: False)
        monkeypatch.setattr(assistant, "_type", lambda p, t, enter=True: self.sent.append(t))   # answer() types here
        monkeypatch.setattr(assistant, "warning", lambda: self.warn)
        monkeypatch.setattr(assistant, "pane", lambda st=None: None if self.dead else "%7")
        monkeypatch.setattr(assistant, "ready_idea", lambda: self.ready)
        monkeypatch.setattr(C, "CONFIG_DIR", C.STATE_DIR.parent / ".pl-t")   # the tab needs a loaded profile
        monkeypatch.setattr(assistant, "ensure", self.ensure)
        monkeypatch.setattr(assistant, "send", self.sent.append)
        monkeypatch.setattr(assistant, "screen", self.screen)
        monkeypatch.setattr(assistant, "reset", self.reset)
        monkeypatch.setattr(assistant, "load", lambda: dict(self.state))
        monkeypatch.setattr(assistant, "set_mode", self.set_mode)

    def ensure(self):
        self.ensured += 1
        self.dead = False
        if self.fail:
            raise self.fail
        return "assistant running in pl-t:assistant (acme)"

    def screen(self, n):
        self.screens += 1
        return self.lines if self.lines is not None else ["> what is stuck?", "card 4a000001 waits for your review"]

    def reset(self):
        self.resets += 1

    def set_mode(self, mode):
        self.modes.append(mode)
        self.state["mode"] = mode


async def _open_assistant(pilot):
    await settle(pilot)
    await pilot.press("0")
    await settle(pilot)


async def test_the_assistant_tab_is_on_by_default_and_0_opens_it(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.active_tab == "assistant" and fa.ensured == 1
        assert "0 Assistant" in screen_text(app)
        assert "waits for your review" in str(app.query_one("#assistant-screen").render())


async def test_the_assistant_tab_is_absent_when_disabled(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    monkeypatch.setattr(C, "ASSISTANT", {"enabled": False})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.active_tab != "assistant" and fa.ensured == 0
        assert "Assistant" not in screen_text(app)


async def test_opening_the_tab_twice_starts_the_assistant_once(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        app.action_tab("dashboard")
        await settle(pilot)
        app.action_tab("assistant")
        await settle(pilot)
        assert fa.ensured == 1


async def test_typing_in_the_box_sends_it_and_digits_do_not_switch_tabs(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.focused is app.query_one("#assistant-input")
        for ch in "retry abc":
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("2")
        await pilot.press("enter")
        await settle(pilot)
        assert fa.sent == ["retry abc", "2"] and app.active_tab == "assistant"
        assert app.query_one("#assistant-input").value == ""


async def test_the_screen_is_not_polled_while_another_tab_is_active(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        view = app.query_one("#assistant-view")
        view.tick()
        await settle(pilot)
        assert fa.screens == 0                  # never opened
        await _open_assistant(pilot)
        app.action_tab("dashboard")
        await settle(pilot)
        before = fa.screens
        for _ in range(3):
            view.tick()
        await settle(pilot)
        assert fa.screens == before
        app.action_tab("assistant")
        await settle(pilot)
        view.tick()
        await settle(pilot)
        assert fa.screens > before


async def test_ensure_failing_shows_a_notice_and_the_console_runs_on(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.fail = SystemExit("pl: no harness account is free for the assistant (pl accounts)")
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert any("no harness account is free" in str(n.message) for n in app._notifications)
        await pilot.press("ctrl+t")      # still alive: keys work
        app.action_tab("dashboard")
        await settle(pilot)
        assert app.active_tab == "dashboard"


async def test_ctrl_r_asks_then_starts_a_new_conversation(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen" and "new assistant conversation" in app.screen.message
        app.screen.dismiss(True)
        await settle(pilot)
        assert fa.resets == 1 and fa.ensured == 2


async def test_an_empty_submit_sends_nothing(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("space")
        await pilot.press("enter")
        await settle(pilot)
        assert fa.sent == []


async def test_a_dead_assistant_is_started_again_while_the_tab_is_open(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert fa.ensured == 1
        fa.dead = True                  # /exit, or tmux lost the window
        app.query_one("#assistant-view").tick()
        await settle(pilot)
        assert fa.ensured == 2


async def test_a_ready_idea_is_filed_only_after_the_confirm(monkeypatch):
    from pl import ideas
    fa = FakeAssistant(monkeypatch)
    fa.ready = {"id": "abcdefabcdef", "title": "Faster page", "status": "interviewing", "ready_to_file": True,
                "version": 2, "brief": {"problem": "Pages load slowly", "open_questions": []}}
    filed = []
    monkeypatch.setattr(ideas, "load", lambda i: dict(fa.ready))
    monkeypatch.setattr(ideas, "is_clear", lambda i: True)
    monkeypatch.setattr(ideas, "approve", lambda i: filed.append(i["id"]) or {**i, "status": "approved", "card_id": "c0000001"})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view = app.query_one("#assistant-view")
        view.tick()
        await settle(pilot)
        assert "Idea ready: Faster page" in str(app.query_one("#assistant-status").render())
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen" and "Faster page" in app.screen.message
        assert "Pages load slowly" in app.screen.message
        app.screen.dismiss(False)
        await settle(pilot)
        assert filed == []
        await pilot.press("ctrl+f")
        await pilot.pause()
        app.screen.dismiss(True)
        await settle(pilot)
        assert filed == ["abcdefabcdef"]


async def test_ctrl_f_refuses_a_draft_changed_since_the_dialog_showed_it(monkeypatch):
    from pl import ideas
    fa = FakeAssistant(monkeypatch)
    fa.ready = {"id": "abcdefabcdef", "title": "Faster page", "status": "interviewing", "ready_to_file": True,
                "version": 2, "brief": {"problem": "Pages load slowly"}}
    disk, filed = dict(fa.ready), []
    monkeypatch.setattr(ideas, "load", lambda i: dict(disk))
    monkeypatch.setattr(ideas, "is_clear", lambda i: True)
    monkeypatch.setattr(ideas, "approve", lambda i: filed.append(i["id"]) or {**i, "status": "approved", "card_id": "c0000001"})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        app.query_one("#assistant-view").tick()
        await settle(pilot)
        for change in ({"version": 3}, {"version": 2, "ready_to_file": False}):
            disk.update(change)                      # saved again (or unmarked) after the snapshot was taken
            await pilot.press("ctrl+f")
            await pilot.pause()
            app.screen.dismiss(True)
            await settle(pilot)
        assert filed == []
        assert sum("changed" in str(n.message) for n in app._notifications) == 2


async def test_an_allow_rule_warning_shows_in_the_tab(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.warn = "warning: allow rules Bash(gh pr *) would skip the prompt"
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert "Bash(gh pr *)" in str(app.query_one("#assistant-warning").render())


async def test_the_tab_strip_starts_with_0_assistant_then_1_dashboard(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        labels = [str(t.label) for t in app.query_one(TabbedContent).query("Tab")]
        assert labels[0] == "0 Assistant" and labels[1] == "1 Dashboard"
        assert app.active_tab == "dashboard"
        await pilot.press("2")
        await settle(pilot)
        await pilot.press("1")
        await settle(pilot)
        assert app.active_tab == "dashboard"
        await pilot.press("0")
        await settle(pilot)
        assert app.active_tab == "assistant"


async def test_esc_leaves_the_assistant_box_and_then_a_digit_switches_tab(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("h", "i")
        await settle(pilot)
        assert app.active_tab == "assistant"
        assert "esc then 1-9" in str(app.query_one("#assistant-keys").render())
        await pilot.press("escape", "1")
        await settle(pilot)
        assert app.active_tab == "dashboard"
        await pilot.press("0")
        await settle(pilot)
        await pilot.press("alt+1")
        await settle(pilot)
        assert app.active_tab == "dashboard"


# ---------- the Assistant tab as a chat ----------

ASID = "11111111-2222-3333-4444-555555555555"
MENU = ["Bash command", "  pl approve 4a000001", "Do you want to proceed?",
        "❯ 1. Yes", "  2. Yes, and don't ask again for pl approve commands", "  3. No, and tell Claude what to do differently"]


def _chat(fa, entries):
    """A fake Claude transcript for the assistant's session under the account's projects folder."""
    fa.state.update(session_id=ASID, account="acme", harness="claude")
    C.CONFIG_DIR.mkdir(exist_ok=True)
    (C.CONFIG_DIR / "config.toml").touch()            # the profile the tab reads [harnesses] from
    d = C.PROFILES["acme"] / "projects" / "-work"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ASID}.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return p


def _user(text, ts="2026-10-01T10:00:00.000Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _said(*blocks, ts="2026-10-01T10:00:01.000Z", **kw):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": list(blocks)}, **kw}


def _chat_text(app):
    return str(app.query_one("#assistant-screen").render())


async def test_the_chat_shows_you_and_assistant_turns_and_compact_tool_lines(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("You are the pl assistant for profile t. Read /x/assistant.md and follow it, then say ready."),
               _said({"type": "text", "text": "Ready."}),
               _user("what is stuck?"),
               _said({"type": "tool_use", "name": "Read", "input": {"file_path": "/x/pl/assistant.md"}},
                     {"type": "tool_use", "name": "Bash", "input": {"command": "pl list"}}),
               {"type": "user", "message": {"role": "user", "content": [
                   {"type": "tool_result", "content": "4a000001  Plan for review  LONG RESULT BODY"}]}},
               _said({"type": "text", "text": "Card 4a000001 waits for your review; key ghp_" + "a" * 30})])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        text = _chat_text(app)
        assert "you\nwhat is stuck?" in text and "assistant\nReady." in text
        assert "↳ Read assistant.md\n" in text and "↳ Bash: pl list\n" in text
        assert "LONG RESULT BODY" not in text                         # results stay collapsed
        assert "ghp_" not in text and "***" in text                   # masked
        assert "You are the pl assistant" not in text                 # the boot prompt is hidden
        assert "waits for your review" in text


async def test_errors_show_in_red(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("go"), _said({"type": "text", "text": "API Error: overloaded"}, isApiErrorMessage=True)])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        t = app.query_one("#assistant-screen").content
        span = next(s for s in t.spans if "overloaded" in t.plain[s.start:s.end])
        assert "red" in str(span.style)


async def test_an_unchanged_transcript_is_not_redrawn(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    p = _chat(fa, [_user("hi"), _said({"type": "text", "text": "hello"})])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view = app.query_one("#assistant-view")
        before = view.redraws
        for _ in range(3):
            view.tick()
            await settle(pilot)
        assert view.redraws == before
        with p.open("a") as f:
            f.write(json.dumps(_said({"type": "text", "text": "more"})) + "\n")
        view.tick()
        await settle(pilot)
        assert view.redraws == before + 1 and "more" in _chat_text(app)


async def test_a_permission_menu_is_a_card_and_a_digit_sends_just_that_digit(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("approve it"), _said({"type": "tool_use", "name": "Bash", "input": {"command": "pl approve 4a000001"}})])
    fa.lines = MENU
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        card = app.query_one("#assistant-menu")
        assert card.display
        body = str(card.render())
        assert "pl approve 4a000001" in body and "1 Yes" in body and "2 Yes, and don't ask again" in body and "3 No" in body
        assert "waiting for you" in str(app.query_one("#assistant-status").render())
        await pilot.press("2")
        await settle(pilot)
        assert fa.sent == ["2"]                                      # one digit, no Enter, nothing auto-answered
        assert app.query_one("#assistant-input").value == ""


async def test_a_question_menu_gets_the_same_card(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.lines = ["Which board?", "❯ 1. Product", "  2. Engineering", "  3. Type something."]
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        body = str(app.query_one("#assistant-menu").render())
        assert "Which board?" in body and "1 Product" in body and "2 Engineering" in body
        assert fa.sent == []


async def test_the_status_line_says_thinking_waiting_or_ready(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("hi")])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view, status = app.query_one("#assistant-view"), lambda: str(app.query_one("#assistant-status").render())
        fa.reg[ASID] = {"sessionId": ASID, "status": "busy"}
        view.tick()
        await settle(pilot)
        assert "thinking…" in status()
        fa.reg[ASID] = {"sessionId": ASID, "status": "idle"}
        view.tick()
        await settle(pilot)
        assert "ready" in status() and "thinking" not in status()
        fa.lines = MENU
        view.tick()
        await settle(pilot)
        assert "waiting for you" in status()


async def test_without_a_registry_a_changing_screen_counts_as_thinking(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view, status = app.query_one("#assistant-view"), lambda: str(app.query_one("#assistant-status").render())
        fa.lines = ["✻ Pondering… (esc to interrupt)"]
        view.tick()
        await settle(pilot)
        assert "thinking…" in status()
        view._moved -= 10                                            # still for a while
        view.tick()
        await settle(pilot)
        assert "ready" in status()


async def test_an_update_restart_is_told_in_the_chat(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("hi", ts="2026-10-01T10:00:00.000Z"), _said({"type": "text", "text": "hello"}, ts="2026-10-01T10:00:01.000Z"),
               _user("again", ts="2026-10-01T11:00:00.000Z")])
    fa.state["updated_at"] = "2026-10-01T10:30:00+00:00"
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        text = _chat_text(app)
        assert text.index("hello") < text.index("pl updated — assistant restarted with the new settings") < text.index("again")


async def test_a_digit_is_not_sent_when_the_question_changed_under_the_card(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.lines = MENU
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.query_one("#assistant-menu").display
        fa.lines = ["Which board?", "❯ 1. Product", "  2. Engineering"]      # a new question, not drawn yet
        await pilot.press("2")
        await settle(pilot)
        assert fa.sent == []
        assert any("the question changed; look again" in str(n.message) for n in app._notifications)
        assert "Engineering" in str(app.query_one("#assistant-menu").render())   # the card was redrawn


async def test_a_digit_typed_after_text_goes_into_the_box(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.lines = MENU
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("a", "2")
        await settle(pilot)
        assert app.query_one("#assistant-input").value == "a2" and fa.sent == []


async def test_a_secret_inside_a_tool_input_renders_masked(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    _chat(fa, [_user("fetch it"), _said({"type": "tool_use", "name": "WebFetch",
                                         "input": {"url": "https://x.test/?k=ghp_" + "b" * 30}})])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        text = _chat_text(app)
        assert "↳ WebFetch" in text and "ghp_" not in text and "b" * 30 not in text


async def test_a_slow_read_says_still_reading_and_lets_a_new_read_start(monkeypatch):
    from pl import assistant
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view, gate, calls = app.query_one("#assistant-view"), threading.Event(), []

        def slow(n):
            calls.append(n)
            gate.wait(5)
            return fa.screen(n)
        monkeypatch.setattr(assistant, "screen", slow)
        try:
            view.tick()
            await pilot.pause(0.2)
            view._read_started -= 10                         # this read has run for 10 s
            view._poll()
            await pilot.pause(0.2)
            assert "still reading" in str(app.query_one("#assistant-status").render()) and len(calls) == 2
        finally:
            gate.set()
        await settle(pilot)


# ---------- list tabs: the first heading on screen; long titles never hide state and now ----------

def _many_cards_data():
    """Enough cards on the Pipeline tab that its list scrolls."""
    data = fake_data()
    data["snapshot"]["rows"] += [_card(f"b{i:07d}aaaa", f"another plan {i}", "Plan for review", kind="review") for i in range(80)]
    return data


@pytest.mark.parametrize("key,table,heading", [("2", "#cards-table", "SPEC READY 1"),
                                                ("5", "#prs-table", "NEED YOUR DECISION")])
async def test_list_tabs_open_with_the_first_heading_on_screen(monkeypatch, key, table, heading):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_many_cards_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press(key)
        await settle(pilot)
        t = app.query_one(table)
        assert t.cursor_row == 1 and t.scroll_y == 0
        assert heading in screen_text(app)


async def test_list_tab_refresh_keeps_the_scroll_while_browsing(monkeypatch):
    _fake_card_read(monkeypatch, [])
    prov = Provider(_many_cards_data())
    app = PlApp(snapshot_provider=prov)
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "b0000070aaaa")
        t = app.query_one("#cards-table")
        y = t.scroll_y
        assert y > 0
        prov.data["snapshot"]["rows"].append(_card("e0000005aaaa", "a new card", "Manual", auto=False))   # a full rebuild
        app.refresh_data()
        await settle(pilot)
        assert _cursor(app, "#cards-table") == "b0000070aaaa" and t.scroll_y == y


@pytest.mark.parametrize("size", [(176, 48), (120, 40)])
async def test_long_titles_leave_state_and_now_on_screen(monkeypatch, size):
    _fake_card_read(monkeypatch, [])
    long = "a very long card title " * 7   # about 160 characters
    data = fake_data(title=long)
    data["now"] = {"4a000001aaaa": {"line": "writing the plan now"}}
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=size) as pilot:
        await _open_cards(pilot, app)
        line = next(x for x in screen_text(app).splitlines() if "4a000001" in x)
        assert "writing the plan" in line and "…" in line
        await pilot.press("n")      # the filtered list: the same columns
        await settle(pilot)
        line = next(x for x in screen_text(app).splitlines() if "4a000001" in x)
        assert "writing the plan" in line and "…" in line


async def _click_assistant_tab(pilot):
    await settle(pilot)
    await pilot.click("Tab#--content-tab-assistant")     # the mouse leaves the focus on the tab bar
    await settle(pilot)


async def _type(pilot, text):
    names = {" ": "space", "?": "question_mark", "!": "exclamation_mark", "@": "at"}
    for ch in text:
        await pilot.press(names.get(ch, ch))


async def test_a_question_typed_after_clicking_the_tab_reaches_the_box_not_the_console_commands(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _click_assistant_tab(pilot)
        assert app.focused is app.query_one("#assistant-input")
        await _type(pilot, "Does it quit? Stop! @me")        # D, s, q, ?, !, @ are all console keys elsewhere
        await pilot.press("enter")
        await settle(pilot)
        assert fa.sent == ["Does it quit? Stop! @me"]
        assert len(app.screen_stack) == 1 and app.active_tab == "assistant"


async def test_clicking_the_active_assistant_tab_again_gives_the_box_the_focus_back(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _click_assistant_tab(pilot)
        await pilot.click("Tab#--content-tab-assistant")
        await settle(pilot)
        assert app.focused is app.query_one("#assistant-input")


async def test_ctrl_r_works_after_clicking_the_tab(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _click_assistant_tab(pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"


async def test_a_send_that_fails_shows_a_notice_and_keeps_the_text(monkeypatch):
    from pl import assistant
    fa = FakeAssistant(monkeypatch)
    notes = []

    def boom(text):
        raise SystemExit(assistant.BUSY)
    monkeypatch.setattr(assistant, "send", boom)
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await _type(pilot, "why?")
        await pilot.press("enter")
        await settle(pilot)
        assert any("busy" in n for n in notes), notes
        assert app.query_one("#assistant-input").value == "why?" and fa.sent == []


async def test_a_reset_that_fails_shows_a_notice(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    notes = []

    def boom():
        raise SystemExit("pl: tmux kill-window: no such window")
    monkeypatch.setattr(fa, "reset", boom)
    from pl import assistant
    monkeypatch.setattr(assistant, "reset", boom)
    app = PlApp(snapshot_provider=Provider())
    monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        app.screen.dismiss(True)
        await settle(pilot)
        assert any("kill-window" in n for n in notes), notes


async def test_an_empty_conversation_says_so_instead_of_showing_a_blank_tab(monkeypatch):
    from pl.tui import assistant as view
    assert "no messages" in view.render_chat([]).plain
    assert view.render_chat([("2026-10-02T10:00:00", "you", "hi")]).plain.startswith("you")
