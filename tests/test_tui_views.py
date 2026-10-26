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
from textual.widgets import DataTable, Select, Tabs, TabbedContent

from pl import cli, harnesses, trackers, watch
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
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))   # no agent window to read
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
            ["12", "11", "2", "1", "2", "6"]
        text = screen_text(app)
        for s in ("SPECS WRITTEN", "READY TO MERGE", "WAITING ON YOU", "Throughput", "Where work waits",
                  "Plan for review", "Needs you", "2 PRs pass the merge check", "Health", "Running, 2 cards waiting"):
            assert s in text, s
        assert "counting…" not in text


async def test_pull_requests_groups():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("prs")
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
        await pilot.press("]")          # data must still be there: the next window shows its own fake value
        await pilot.pause()
        assert tile(app, "specs") == "71"
        prov.fail = None
        app.refresh_data()
        await settle(pilot)
        assert "Rate exceeded" not in str(app.query_one("#header").render())


async def test_brackets_cycle_the_window():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        seen = [(tile(app, "specs"), tile(app, "opened"))]
        for _ in range(3):
            await pilot.press("]")
            await pilot.pause()
            seen.append((tile(app, "specs"), tile(app, "opened")))
        assert seen == [("12", "2"), ("71", "5"), ("3", "1"), ("12", "2")]
        await pilot.press("[")
        await pilot.pause()
        assert tile(app, "specs") == "3"


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
async def test_dashboard_bottom_row_two_equal_columns(size):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        widths = [app.query_one(i).size.width for i in ("#decide", "#standup-panel")]
        assert min(widths) > 0 and max(widths) - min(widths) <= 1, widths


async def _open_tab(pilot, app, tid):
    await settle(pilot)
    app.action_tab(tid)
    await settle(pilot)


@pytest.mark.parametrize("tid", ["cards", "waiting", "prs"])
async def test_80x24_every_tab_fits_with_no_cut_columns(tid):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await _open_tab(pilot, app, tid)
        pane = app.query_one(f"#{tid}")
        for w in pane.walk_children(with_self=True):
            if w.display and w.region.width:
                assert w.region.right <= 80, (w, w.region)
        for t in pane.query(DataTable):
            assert sum(c.get_render_width(t) for c in t.ordered_columns) <= t.content_size.width, t


async def test_80x24_enter_opens_the_card_details():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await _open_tab(pilot, app, "cards")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, tui_cards.CardScreen)


async def test_80x24_enter_on_a_pr_opens_its_overview():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await _open_tab(pilot, app, "prs")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, tui_prs.PrScreen)


@pytest.mark.parametrize("tid", ["cards", "prs"])
async def test_160x48_list_and_details_both_show(tid):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(160, 48)) as pilot:
        await _open_tab(pilot, app, tid)
        for sel in (f"#{tid}-table", f"#{tid}-side"):
            w = app.query_one(sel)
            assert w.display and w.region.width > 0, sel


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
        app.action_tab("prs")
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
        await pilot.press("W")
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


@pytest.mark.parametrize("row,tab,selected", [(1, "prs", None), (2, "cards", "spec0001aaaa"), (3, "cards", "4a000001aaaa"),
                                              (4, "prs", None), (5, "prs", None), (6, "cards", "c0000003aaaa")])
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


async def test_zero_count_rows_are_hidden(monkeypatch):
    app = PlApp(snapshot_provider=Provider())   # spec gate off: no specs waiting
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        keys = [r.key.value for r in app.query_one("#decide").ordered_rows]
        assert "specs" not in keys and keys[0] == "needs" and keys[-1] == "health"


async def test_dashboard_leads_with_needs_you_and_hides_empty_rows():
    from pl.tui.dashboard import decide_rows
    from pl.tui.needs import needs_you_count
    data = fake_data()
    rows = decide_rows(data)
    assert rows[0][0] == "needs" and rows[0][1] == needs_you_count(data) and rows[-1][0] == "health"
    assert all(n for key, n, *_ in rows[1:-1])
    data["snapshot"]["rows"] = []
    rows = decide_rows(data)
    assert [r[0] for r in rows] == ["needs", "health"] and "Nothing needs you" in rows[0][2]


async def test_health_opens_in_a_details_panel():
    from pl.tui.dashboard import HealthScreen
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "counting…" not in screen_text(app)
        table = app.query_one("#decide")
        table.focus()
        table.move_cursor(row=table.row_count - 1)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, HealthScreen)
        text = screen_text(app)
        for s in ("tokens", "counting…", "exceeded", "harness accounts"):
            assert s in text, s
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, HealthScreen)
        app.action_health()
        await settle(pilot)
        assert isinstance(app.screen, HealthScreen)


async def _open_pr(pilot, app, url="https://github.com/o/frontend/pull/1200"):
    await settle(pilot)
    app.action_tab("prs")
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


async def test_M_merges_only_after_confirmation(monkeypatch):
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("M")
        await settle(pilot)
        assert "development" in screen_text(app) and "squash" in screen_text(app)   # the modal names base and method
        assert gh.merges() == []
        await pilot.press("n")
        await settle(pilot)
        assert gh.merges() == [] and len(app.screen_stack) == 1
        await pilot.press("M")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", "https://github.com/o/frontend/pull/1200", "--squash"]]


async def test_not_ready_pr_on_M_names_the_reason_and_asks(monkeypatch):
    gh = FakeGh(_view(checks=("SUCCESS", "FAILURE")))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "1 check failed" in screen_text(app)
        await pilot.press("M")
        await settle(pilot)
        assert len(app.screen_stack) == 2 and gh.merges() == []
        assert "1 check failed" in screen_text(app) and "Merge anyway?" in screen_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", "https://github.com/o/frontend/pull/1200", "--squash"]]


async def test_repo_without_squash_offers_a_merge_commit(monkeypatch):
    gh = FakeGh(_view(), merge=(1, "", "GraphQL: Squash merges are not allowed on this repository. (mergePullRequest)"))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("M", "y")
        await settle(pilot)
        url = "https://github.com/o/frontend/pull/1200"
        assert gh.merges() == [["gh", "pr", "merge", url, "--squash"]]   # no automatic --merge retry
        assert any("press M again" in str(n.message) for n in app._notifications)
        gh.merge = (0, "merged", "")
        await pilot.press("M")
        await settle(pilot)
        assert "merge commit" in screen_text(app) and len(app.screen_stack) == 2 and len(gh.merges()) == 1
        await pilot.press("y")
        await settle(pilot)
        assert gh.merges() == [["gh", "pr", "merge", url, "--squash"], ["gh", "pr", "merge", url, "--merge"]]


def _notes(app):
    return " | ".join(str(n.message) for n in app._notifications)


async def _asks_to_merge_anyway(pilot, app, gh):
    await pilot.press("M")
    await settle(pilot)
    asked = len(app.screen_stack) == 2 and "Merge anyway?" in screen_text(app) and gh.merges() == []
    await pilot.press("escape")
    await settle(pilot)
    return asked


async def test_changes_requested_is_not_ready(monkeypatch):
    gh = FakeGh(_view(review="CHANGES_REQUESTED"))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: a reviewer asked for changes" in screen_text(app)
        assert await _asks_to_merge_anyway(pilot, app, gh)


async def test_missing_merge_ready_label_asks_to_merge_anyway(monkeypatch):
    gh = FakeGh(_view(labels=("pl:ready-for-review",)))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: no merge-ready label" in screen_text(app)
        assert await _asks_to_merge_anyway(pilot, app, gh)


async def test_a_renamed_merge_ready_label_is_the_only_one_that_counts(monkeypatch):
    monkeypatch.setattr(C, "CODE_HOST", {"owner": None, "labels": {"merge_ready": "ship-it"}})
    gh = FakeGh(_view(labels=("pl:merge-ready",)))
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        assert "not ready: no merge-ready label" in screen_text(app)
        assert await _asks_to_merge_anyway(pilot, app, gh)
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
        await pilot.press("M")
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
        for key in ("M",):
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
        assert str(app.query_one("#activity-views", TabbedContent).get_tab("alerts").label) == "Alerts 1"
        app.action_tab("alerts")
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
        assert "none open" in screen_text(app) and "Alerts 0" in screen_text(app)


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
        await pilot.press("f")
        await settle(pilot)
        text = screen_text(app)
        assert "Nothing needs you." in text and "select a row" in text
        assert str(app.query_one("#work-views", TabbedContent).get_tab("cards").label) == "Board 1"   # no "need you" part
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
    app.action_tab("cards")
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
        assert "Board 5 · 6 need you" in text and "Kanban" not in text and "Needs you" not in text
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
        assert "Board 6 · 7 need you" in screen_text(app)
        assert "7 need you" in t.border_title and "f only those" in t.border_title


async def test_f_shows_only_the_cards_that_need_you_and_f_again_shows_all(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_attention_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app, "60000002aaaa")
        t = app.query_one("#cards-table")
        await pilot.press("f")
        await settle(pilot)
        assert set(view._rows) == MINE and view._current()[0] == "60000002aaaa"   # the selected card stays selected
        assert "needs you only" in t.border_title and "4 of 6 cards" in t.border_title
        text = screen_text(app)
        assert "PR OPEN" not in text and "SPEC READY" not in text and "INBOX 1" in text
        app.refresh_data()
        await settle(pilot)
        assert set(view._rows) == MINE   # the filter stays on across refreshes
        await pilot.press("f")
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
    assert not ({"d", "h", "f", "F", "z", "u", "e"} & app_keys) and {"d", "h", "f", "F", "z", "u", "e"} <= set(view_keys)
    assert "D" in app_keys and "D" not in view_keys   # D stays the dispatcher's key
    assert "P" in view_keys and "P" not in app_keys
    assert {b.key: b.description for b in tui_cards.CardsView.BINDINGS}["P"] == "plan filter"
    assert {"ctrl+k", "exclamation_mark"} <= app_keys and not ({"ctrl+k", "exclamation_mark"} & set(view_keys))


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


async def test_F_marks_a_card_done_after_asking(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "done", ["done d0000004"])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "d0000004aaaa")
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "done" in app.screen.message.lower()
        await pilot.press("n")
        await settle(pilot)
        assert got == []
        await pilot.press("F")
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
        assert "Board 5 · 6 need you" in text   # the tab count leaves Done out
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
        for key in ("d", "F", "h"):
            await pilot.press(key)
            await settle(pilot)
            assert len(app.screen_stack) == 1, key
    assert got == [[], [], []]


async def test_the_top_tabs_are_the_eight_jobs_in_order():
    from pl.tui.chrome import GROUP_KEYS, GROUPS
    numbered = [g for g in GROUPS if g[0] != "home"]
    assert [g[1] for g in numbered] == ["Dashboard", "Assistant", "Work", "Review", "Automate", "Extend", "Activity", "Settings"]
    assert [GROUP_KEYS[g[0]] for g in numbered] == [str(i) for i in range(1, 9)]
    assert GROUP_KEYS["home"] == "H"


@pytest.mark.parametrize("old,place", [
    ("home", ("home", "home")), ("dashboard", ("dashboard", "dashboard")), ("assistant", ("assistant", "assistant")),
    ("ideas", ("work", "ideas")), ("cards", ("work", "cards")), ("prs", ("review", "prs")),
    ("waiting", ("review", "waiting")), ("loops", ("automate", "loops")), ("alerts", ("activity", "alerts")), ("activity", ("activity", "activity")),
    ("settings", ("settings", "settings")), ("subagents", ("automate", "subagents"))])
def test_every_old_tab_id_resolves(monkeypatch, old, place):
    from pl.tui import chrome
    monkeypatch.setattr(chrome, "tabs", lambda: [(v, v) for g in chrome.GROUPS for v, _ in g[2]])
    assert chrome.resolve(old) == place
    assert chrome.resolve("nope") is None


def test_a_group_id_resolves_to_its_first_view(monkeypatch):
    from pl.tui import chrome
    monkeypatch.setattr(chrome, "tabs", lambda: [(v, v) for g in chrome.GROUPS for v, _ in g[2]])
    assert chrome.resolve("work") == ("work", None) and chrome.resolve("automate") == ("automate", None)


def test_hidden_features_stay_hidden(monkeypatch):
    from pl.tui import chrome
    monkeypatch.setattr(C, "ASSISTANT", {"enabled": False})
    monkeypatch.setattr("pl.tui.getstarted.wanted", lambda: False)
    assert "assistant" not in dict(chrome.tabs())
    assert chrome.resolve("assistant") is None   # the group has no view left, so it is hidden too
    assert chrome.resolve("home") is None   # one profile
    gids = [g[0] for g in chrome.groups()]
    assert "home" not in gids and "extend" in gids


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
        app.action_tab("prs")
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
        self.ended, self.stored = ("conv-1", 2), []
        monkeypatch.setattr(assistant, "end_conversation", lambda: self.ended)
        monkeypatch.setattr(assistant, "store_turns", lambda items: 0)
        monkeypatch.setattr(assistant, "stored_items", lambda: self.stored)

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
    await pilot.press("2")
    await settle(pilot)


async def test_the_assistant_tab_is_on_by_default_and_1_opens_it(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.active_tab == "assistant" and fa.ensured == 1
        assert "2 Assistant" in screen_text(app)
        assert "waits for your review" in str(app.query_one("#assistant-screen").render())


async def test_the_assistant_tab_is_absent_when_disabled(monkeypatch):
    from pl.tui.chrome import tabs
    fa = FakeAssistant(monkeypatch)
    monkeypatch.setattr(C, "ASSISTANT", {"enabled": False})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        assert app.active_tab != "assistant" and fa.ensured == 0
        assert "assistant" not in dict(tabs())


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


async def _ctrl_r_confirmed(app, pilot):
    await pilot.press("ctrl+r")
    await pilot.pause()
    app.screen.dismiss(True)
    await settle(pilot)


def _notices(app):
    return [str(n.message) for n in app._notifications]


async def test_ctrl_r_confirmed_notifies_saved_n_and_u_undoes_it(monkeypatch):
    from pl import brain
    FakeAssistant(monkeypatch)
    undone = []
    replies = iter(["undone 2", "undone 0"])
    monkeypatch.setattr(brain, "undo", lambda conv: undone.append(conv) or next(replies))
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await _ctrl_r_confirmed(app, pilot)
        assert "saved 2 · u undo" in _notices(app)
        await pilot.press("escape", "u")
        await settle(pilot)
        assert undone == ["conv-1"] and "undone 2" in _notices(app)
        await pilot.press("u")
        await settle(pilot)
        assert "undone 0" in _notices(app)


async def test_u_with_nothing_ended_says_nothing_to_undo(monkeypatch):
    from pl import brain
    FakeAssistant(monkeypatch)
    monkeypatch.setattr(brain, "undo", lambda conv: pytest.fail("undo must not run"))
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("escape", "u")
        await settle(pilot)
        assert "nothing to undo" in _notices(app)


async def test_saved_0_leaves_nothing_to_undo(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.ended = ("conv-1", 0)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await _ctrl_r_confirmed(app, pilot)
        assert "saved 0" in _notices(app)
        await pilot.press("escape", "u")
        await settle(pilot)
        assert "nothing to undo" in _notices(app)


async def test_a_fresh_session_shows_the_stored_conversation_first(monkeypatch):
    fa = FakeAssistant(monkeypatch)
    fa.stored = [("2026-01-01T10:00:00", "you", "earlier question"), ("2026-01-01T10:00:01", "assistant", "earlier answer")]
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        text = str(app.query_one("#assistant-screen").render())
        assert text.index("earlier question") < text.index("earlier answer") < text.index("waits for your review")


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


def _labels(app, widget_id):
    return [str(t.label) for t in app.query_one(widget_id, TabbedContent).query_one(Tabs).query("Tab") if t.display]


async def test_the_console_opens_on_dashboard_and_keys_open_the_jobs(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert _labels(app, "#tabs") == ["1 Dashboard", "2 Assistant", "3 Work", "4 Review", "5 Automate", "6 Extend", "7 Activity", "8 Settings"]
        assert app.active_tab == "dashboard"
        for key, view in (("3", "ideas"), ("4", "waiting"), ("5", "loops"), ("1", "dashboard")):
            await pilot.press(key)
            await settle(pilot)
            assert app.active_tab == view, key


async def test_review_opens_on_waiting_then_pull_requests(monkeypatch):
    from pl.tui.needs import needs_you_count
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await settle(pilot)
        assert app.active_tab == "waiting"
        labels = _labels(app, "#review-views")
        assert labels[0] == "Waiting" and labels[1].startswith("Pull requests")
        assert app.query_one("#waiting-table").row_count == needs_you_count(app.data) > 0


async def test_footer_shows_the_review_keys(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("4")
        await settle(pilot)
        app.query_one("#waiting-table").focus()
        await settle(pilot)
        assert {"approve", "send back", "open"} <= _footer_keys(app)


async def test_without_the_assistant_the_keys_stay_put(monkeypatch):
    FakeAssistant(monkeypatch)
    monkeypatch.setattr(C, "ASSISTANT", {"enabled": False})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "2 Assistant" not in _labels(app, "#tabs")
        for key, view in (("8", "settings"), ("3", "ideas")):
            await pilot.press(key)
            await settle(pilot)
            assert app.active_tab == view, key
        await pilot.press("2")
        await settle(pilot)
        assert app.active_tab == "ideas"


async def test_work_shows_ideas_and_board_as_sub_tabs():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        labels = _labels(app, "#work-views")
        assert labels[0] == "Ideas" and labels[1].startswith("Board")
        app.action_tab("cards")
        await settle(pilot)
        assert app.active_tab == "cards" and app.query_one("#tabs", TabbedContent).active == "g-work"


async def test_old_tab_ids_still_open_their_view():
    app = PlApp(snapshot_provider=Provider(), tab="subagents")
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.active_tab == "subagents" and app.query_one("#tabs", TabbedContent).active == "g-automate"
        app.action_tab("alerts")
        await settle(pilot)
        assert app.active_tab == "alerts" and app.query_one("#tabs", TabbedContent).active == "g-activity"
        app.action_tab("nope")
        await settle(pilot)
        assert app.active_tab == "alerts"


async def test_80x24_every_top_tab_and_sub_tab_fits(monkeypatch):
    FakeAssistant(monkeypatch)
    from pl.tui.chrome import groups
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        for gid, _, views in groups():
            for view, _ in views:
                app.action_tab(view)
                await settle(pilot)
                outer = app.query_one("#tabs", TabbedContent).query_one(Tabs)
                assert outer.region.y < 24 and outer.virtual_size.width <= 80, view
                inner = app.query_one(f"#{gid}-views", TabbedContent)
                if len(views) > 1:
                    assert 0 < inner.query_one(Tabs).region.bottom < 24 and inner.query_one(Tabs).virtual_size.width <= 80, view


async def test_esc_leaves_the_assistant_box_and_then_a_digit_switches_tab(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        await pilot.press("h", "i")
        await settle(pilot)
        assert app.active_tab == "assistant"
        assert "esc then 0-9" in str(app.query_one("#assistant-keys").render())
        await pilot.press("escape", "1")
        await settle(pilot)
        assert app.active_tab == "dashboard"
        await pilot.press("2")
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


async def test_a_screen_read_that_lands_while_the_view_is_removed_does_not_crash(monkeypatch):
    """Live in a loaded test run: the 1 s poll's read came back while the console was closing, and the card it drew
    was already gone (NoMatches killed the app)."""
    fa = FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_assistant(pilot)
        view, go = app.query_one("#assistant-view"), threading.Event()
        real = fa.screen
        monkeypatch.setattr(fa, "screen", lambda n: go.wait(10) and real(n))
        from pl import assistant
        monkeypatch.setattr(assistant, "screen", fa.screen)
        view.tick()
        await app.query_one("#assistant-menu").remove()
        go.set()
        await settle(pilot)
        assert app.is_running


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
    from pl.tui.assistant import AssistantView
    fa = FakeAssistant(monkeypatch)
    fa.lines = MENU
    monkeypatch.setattr(AssistantView, "_poll", lambda self: None)   # the 1 s poll would draw the new question first
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


@pytest.mark.parametrize("key,table,heading", [("cards", "#cards-table", "SPEC READY 1"),
                                                ("prs", "#prs-table", "NEED YOUR DECISION")])
async def test_list_tabs_open_with_the_first_heading_on_screen(monkeypatch, key, table, heading):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_many_cards_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab(key)
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
        await pilot.press("f")      # the filtered list: the same columns
        await settle(pilot)
        line = next(x for x in screen_text(app).splitlines() if "4a000001" in x)
        assert "writing the plan" in line and "…" in line


async def _click_assistant_tab(pilot):
    await settle(pilot)
    await pilot.click("Tab#--content-tab-g-assistant")     # the mouse leaves the focus on the tab bar
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
        await pilot.click("Tab#--content-tab-g-assistant")
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


# ---------- restart (R), adopt (i) and hold (p) ----------

def _with_controls():
    data = fake_data()
    manual = _card("e0000005aaaa", "A manual card in Spec ready", "Spec ready", auto=False, hours=3)
    live = _card("e0000006aaaa", "A card with a stuck planner", "Spec ready", hours=4)
    live["worker"] = {"stage": "plan", "window": "@9", "pane": "%9", "profile": "acme"}
    held = _card("e0000007aaaa", "A held card", "Approved", hours=6)
    held["card"]["tags"] = ["parked"]
    data["snapshot"]["rows"] += [manual, live, held]
    return data


def test_the_new_pipeline_keys_do_not_clash():
    app_keys = {b.key for b in PlApp.BINDINGS}
    view_keys = [b.key for b in tui_cards.CardsView.BINDINGS]
    assert len(view_keys) == len(set(view_keys))
    assert {"R", "i", "p"} <= set(view_keys) and not ({"R", "i", "p"} & app_keys)


async def test_a_on_a_manual_spec_card_says_how_to_hand_it_to_the_pipeline(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_controls()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "e0000005aaaa")
        for key in ("a", "x", "o"):
            await pilot.press(key)
            await settle(pilot)
        assert len(app.screen_stack) == 1
        assert _notes(app).count("this card is manual: press i to hand it to the pipeline (pl adopt)") == 3


async def test_i_adopts_a_manual_card_after_a_confirm(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "adopt", ["adopted e0000005"])
    app = PlApp(snapshot_provider=Provider(_with_controls()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "spec0001aaaa")   # already in the pipeline: a notice
        await pilot.press("i")
        await settle(pilot)
        assert len(app.screen_stack) == 1 and "already in the pipeline" in _notes(app)
        await _open_cards(pilot, app, "e0000005aaaa")
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "pl adopt" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("e0000005aaaa",)] and got[0][1] is not threading.main_thread()


async def test_R_restarts_the_agent_of_a_card_after_a_confirm(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "restart", ["restarted e0000006"])
    app = PlApp(snapshot_provider=Provider(_with_controls()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "spec0001aaaa")   # no agent: a notice
        await pilot.press("R")
        await settle(pilot)
        assert len(app.screen_stack) == 1 and "R restarts" in _notes(app)
        await _open_cards(pilot, app, "e0000006aaaa")
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "plan agent" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in got] == [("e0000006aaaa",)] and got[0][1] is not threading.main_thread()


async def test_p_holds_a_card_with_a_reason_and_releases_a_held_one(monkeypatch):
    from pl.tui.review import ConfirmScreen
    _fake_card_read(monkeypatch, [])
    held = _called(monkeypatch, "hold", ["held e0000006"])
    freed = _called(monkeypatch, "unhold", ["unheld e0000007"])
    app = PlApp(snapshot_provider=Provider(_with_controls()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "e0000006aaaa")
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, tui_cards.ReasonScreen)
        for ch in "legal":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Hold" in app.screen.message and "legal" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
        await _open_cards(pilot, app, "e0000007aaaa")
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Release the hold" in app.screen.message
        await pilot.press("y")
        await settle(pilot)
    assert [g[0] for g in held] == [("e0000006aaaa", "legal")]
    assert [g[0] for g in freed] == [("e0000007aaaa",)]


async def test_header_says_kill_switch_on(monkeypatch):
    from pl import outward
    from pl.tui.chrome import header_text
    data = fake_data()
    data["kill"] = {"on": True, "since": "2026-10-23T14:30:00+00:00", "by": "user"}
    t = header_text(data)
    assert "KILL SWITCH ON since 14:30" in t.plain
    # Also in app
    outward.set(True)
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "KILL SWITCH ON since 14:30" in screen_text(app)


async def test_K_asks_then_turns_it_on_and_off(monkeypatch):
    from pl import outward
    from pl.tui.review import ConfirmScreen
    outward.set(False)
    assert not outward.state()["on"]
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "kill switch" in app.screen.message.lower()
        await pilot.press("n")
        await settle(pilot)
        assert not outward.state()["on"] and len(app.screen_stack) == 1

        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(pilot)
        assert outward.state()["on"] and len(app.screen_stack) == 1

        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "off" in app.screen.message.lower()
        await pilot.press("y")
        await settle(pilot)
        assert not outward.state()["on"] and len(app.screen_stack) == 1


async def test_merge_is_refused_while_the_kill_switch_is_on(monkeypatch):
    from pl import outward
    outward.set(True)
    gh = FakeGh(_view())
    monkeypatch.setattr(tui_prs, "_run", gh)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_pr(pilot, app)
        await pilot.press("M")
        await settle(pilot)
        assert gh.merges() == []
        assert len(app.screen_stack) == 1  # no merge confirmation opened

        # Turn it off and confirm merge is allowed
        outward.set(False)
        await pilot.press("M")
        await settle(pilot)
        assert len(app.screen_stack) == 2



# ---------- plan 5 slice 1: plain output, footer actions, per-screen help ----------

GOLDEN_PLAIN_FRAME = "\n".join([
    'pl watch  14:30:00   dispatcher: running   profiles: acme: ok (0 running, 0 queued)  acme2: ok (0 running, 0 queued)',
    'NEEDS YOU: 2 plans to review',
    'CARDS',
    '',
    'LOOPS   1 of 1 running',
    ' ○ merge-check',
    ' ? frontend#1117  Keyboard shortcuts: gather stalled PRs',
    ' ? api#1576  fix(users): delete routes staff only',
    ' ? frontend#1200  feat: merge me first',
    ' ? frontend#1201  feat: merge me second',
    ' ? api#1300  feat: waiting for the gate',
    '   spec0001  pl: track cards in GitHub Projects',
    '>> 4a000001  Add a salesforce keyword to the palette',
    '>> 60000002  api: no persistent BCC field',
    '   c0000003  Host DB proxy serves a frozen database',
    '   d0000004  objective_outcome recorded succeeded',
    '',
    'NEEDS YOU: pl review 4a000001   pl review 60000002',
    'dispatcher, last lines:',
    '  dispatcher idle',
    '',
    'agent screens: tmux attach -t pl-test   then Ctrl-b w to pick a window   |   !! = agent needs you   >> = plan waits for your review   |   pl watch (no --plain) is the TUI',
])


def _plain_frame(monkeypatch):
    monkeypatch.setattr(watch, "pane_tail", lambda pane, n: ["dispatcher idle"])
    monkeypatch.setattr(C, "TMUX_SESSION", "pl-test")
    return watch.render_watch(fake_data()["snapshot"])


def test_plain_and_once_output_is_unchanged(monkeypatch):
    assert _plain_frame(monkeypatch) == GOLDEN_PLAIN_FRAME


def test_pl_watch_once_prints_one_frame(monkeypatch, capsys):
    monkeypatch.setattr(watch, "watch_snapshot", lambda: fake_data()["snapshot"])
    frame = _plain_frame(monkeypatch)
    watch.cmd_watch(argparse.Namespace(once=True, plain=False))
    assert capsys.readouterr().out == frame + "\n"


def _footer_keys(app):
    return {b.description for _, (_, b, enabled, _) in app.screen.active_bindings.items() if enabled}


async def test_footer_on_a_plan_gate_shows_approve_send_back_and_hold(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
        assert {"approve", "send back", "hold"} <= _footer_keys(app)
        assert "hold / release" not in _footer_keys(app)


async def test_footer_on_a_running_card_hides_approve_and_send_back(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index("d0000004aaaa"))
        await settle(pilot)
        keys = _footer_keys(app)
        assert "approve" not in keys and "send back" not in keys and "hold" in keys


@pytest.mark.parametrize("tab,key", [("dashboard", "1"), ("cards", "3"), ("prs", "4"), ("loops", "5"),
                                     ("alerts", "7"), ("ideas", "3"), ("settings", "8"), ("home", "H")])
async def test_question_mark_opens_this_screens_help(tab, key, monkeypatch, tmp_path):
    from pl.tui.app import HelpScreen
    from pl.tui.chrome import HELP
    _two_profiles(monkeypatch, tmp_path)
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 120)) as pilot:
        await settle(pilot)
        await pilot.press(key)
        await settle(pilot)
        app.action_tab(tab)
        await settle(pilot)
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, HelpScreen)
        text = " ".join(screen_text(app).replace("│", " ").split())
        assert HELP[tab] in text and "quit" in text
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, HelpScreen)


def _two_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("a", "b"):
        (tmp_path / f".pl-{name}").mkdir()
        (tmp_path / f".pl-{name}" / "config.toml").write_text("")


async def _press_help(pilot, app, key, tab=None):
    await settle(pilot)
    await pilot.press(key)
    await settle(pilot)
    if tab:   # a grouped tab key (3 Work) can land on another of its views; open the one under test
        app.action_tab(tab)
        await settle(pilot)
    await pilot.press("question_mark")
    await settle(pilot)


@pytest.mark.parametrize("tab,key", [("dashboard", "1"), ("cards", "3"), ("loops", "5"), ("settings", "8"),
                                     ("ideas", "3"), ("home", "H")])
async def test_help_panel_lists_every_bound_action_with_a_command(tab, key, monkeypatch, tmp_path):
    from pl.tui.app import HelpScreen
    _two_profiles(monkeypatch, tmp_path)
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await _press_help(pilot, app, key, tab)
        assert isinstance(app.screen, HelpScreen)
        text = " ".join(app.screen.body().plain.split())
        under = app.screen_stack[-2]
        shadowed = {b.key for bs in app._bindings.key_to_bindings.values() for b in bs if b.priority}
        for b_key, active in under.active_bindings.items():
            if active.enabled and active.binding.description and (b_key not in shadowed or active.node is app):
                assert active.binding.description in text, b_key   # a priority app key (left/right views) hides the widget's own
        assert app.screen.rows and all(command for _, _, _, command in app.screen.rows)
        if tab == "cards":
            assert any(what == "drop" and command == "pl drop <id>" for _, _, what, command in app.screen.rows)


async def test_help_panel_groups_actions_in_order(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await _press_help(pilot, app, "3")
        text = app.screen.body().plain
        assert 0 <= text.index("navigate") < text.index("act on the selection")
        navigate = text[text.index("navigate"):text.index("act on the selection")]
        assert "1-8            Work" in navigate


async def test_help_on_a_pushed_card_screen_shows_its_own_description(monkeypatch):
    from pl.tui.cards import CardScreen
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 80)) as pilot:
        await _open_cards(pilot, app, "4a000001aaaa")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, CardScreen)
        await pilot.press("question_mark")
        await settle(pilot)
        text = " ".join(app.screen.body().plain.split())
        assert CardScreen.__doc__.splitlines()[0] in text
        assert ("approve", "pl approve <id>") in [(what, command) for _, _, what, command in app.screen.rows]


async def test_palette_rows_match_help_rows_on_the_current_tab(monkeypatch):
    from pl.tui import palette
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        await pilot.press("3")
        await settle(pilot)
        expected = [r[:4] for r in palette.rows(app.screen.active_bindings.items())]
        await pilot.press("question_mark")
        await settle(pilot)
        assert app.screen.rows == expected


def _palette(app):
    return {c.title: c for c in app.get_system_commands(app.screen)}


def _find(commands, *words):
    return [c for t, c in commands.items() if all(w in t.lower() for w in words)]


async def test_palette_finds_a_tab_by_name(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        commands = _palette(app)
        assert "Loops tab (5)" in commands
        commands["Loops tab (5)"].callback()
        await settle(pilot)
        assert app.active_tab == "loops"


async def test_palette_finds_an_action_by_name_or_meaning(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        commands = _palette(app)
        retry = _find(commands, "retry")
        assert retry and _find(commands, "try again")
        assert any("on Board" in c.help and "pl retry <id>" in c.help for c in retry)


async def test_palette_runs_an_action_on_the_current_tab(monkeypatch):
    _fake_card_read(monkeypatch, [])
    provider = Provider()
    app = PlApp(snapshot_provider=provider)
    async with app.run_test(size=(176, 80)) as pilot:
        await pilot.press("3")
        await settle(pilot)
        before = len(provider.threads)
        _find(_palette(app), "refresh (r)")[0].callback()
        await settle(pilot)
        assert len(provider.threads) > before


async def test_palette_switches_tab_then_runs_the_action(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        ran = []
        monkeypatch.setattr(PlApp, "run_action", lambda self, action, node: ran.append((self.active_tab, action)) or _done())
        row = next(c for t, c in _palette(app).items() if t.startswith("pause") and "Loops" in c.help)
        row.callback()
        await settle(pilot)
        assert app.active_tab == "loops" and ran and ran[0][0] == "loops"


async def _done():
    return None


async def test_palette_selection_action_with_nothing_selected_only_switches(monkeypatch):
    _fake_card_read(monkeypatch, [])
    data = fake_data()
    data["snapshot"]["rows"] = []
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        notes = []
        monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(message))
        _find(_palette(app), "drop")[0].callback()
        await settle(pilot)
        assert app.active_tab == "cards" and len(app.screen_stack) == 1
        assert notes and notes[-1].endswith("pick a card first")


async def test_palette_omits_an_action_disabled_on_another_tab(monkeypatch):
    from pl.tui.loops import LoopsView
    _fake_card_read(monkeypatch, [])
    monkeypatch.setattr(LoopsView, "check_action", lambda self, action, parameters: None if action == "pause" else True, raising=False)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        assert not [c for t, c in _palette(app).items() if t.startswith("pause") and "Loops" in c.help]
        assert [c for t, c in _palette(app).items() if t.startswith("run now") and "Loops" in c.help]


async def test_palette_keeps_existing_entries(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 80)) as pilot:
        await settle(pilot)
        commands = _palette(app)
        old = ["What's new", "Skills: view, edit, create", "Kill switch", "Quit"]
        assert all(t in commands and commands[t].discover for t in old)
        new = [c for t, c in commands.items() if t not in old and t != "Health"]
        assert len(new) > 10 and not any(c.discover for c in new if c.help.startswith(("on ", "go to tab")))


@pytest.mark.parametrize("data", [None, "full"])
@pytest.mark.parametrize("key", ["1", "3", "5", "8", "2", "H"])
async def test_every_help_row_is_in_the_palette(key, data, monkeypatch, tmp_path):
    _two_profiles(monkeypatch, tmp_path)
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(fake_data() if data else None))
    async with app.run_test(size=(176, 80)) as pilot:
        await _press_help(pilot, app, key)
        rows = [r for r in app.screen.rows if r[2] not in {"views", "switch view"} and r[0] != "navigate"]
        await pilot.press("escape")
        await settle(pilot)
        titles = " | ".join(_palette(app))
        for _, _, what, _ in rows:
            assert what in titles, what


async def test_whatsnew_opens_from_the_palette():
    from pl.tui.app import WhatsNewScreen
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        command = next(c for c in app.get_system_commands(app.screen) if c.title == "What's new")
        command.callback()
        await settle(pilot)
        assert isinstance(app.screen, WhatsNewScreen)


async def test_b_opens_the_assistant_with_a_notice(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.active_tab == "dashboard"
        await pilot.press("b")
        await settle(pilot)
        assert app.active_tab == "assistant" and app.query_one("#tabs", TabbedContent).active == "g-assistant"
        assert "Brain is now part of Assistant" in _notes(app)


async def test_b_typed_in_the_assistant_box_stays_in_the_box(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("2")
        await settle(pilot)
        await pilot.press("a", "b")
        await settle(pilot)
        assert app.query_one("#assistant-input").value.endswith("ab") and app.active_tab == "assistant"


async def test_the_assistant_group_has_only_the_assistant(monkeypatch):
    from pl.tui.chrome import resolve
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert _labels(app, "#assistant-views") == ["Assistant"]
        assert resolve("brain") is None


async def test_no_brain_command_in_the_palette():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert not [c for c in app.get_system_commands(app.screen) if "Brain" in c.title]


async def test_b_with_the_assistant_off_notifies_and_stays(monkeypatch):
    FakeAssistant(monkeypatch)
    monkeypatch.setattr(C, "ASSISTANT", {"enabled": False})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("b")
        await settle(pilot)
        assert app.active_tab == "dashboard" and "the Assistant is off" in _notes(app)


def test_drafts_no_longer_name_a_brain_box():
    assert "#brain-input" not in tui_app.DRAFTS
    assert set(tui_app.DRAFTS) == {"#assistant-input", "#idea-input"}


def test_no_brain_view_left_in_the_tui_source():
    from pathlib import Path
    tui = Path(tui_app.__file__).parent
    assert not (tui / "brain.py").exists()
    for f in [*tui.glob("*.py"), *tui.glob("*.tcss")]:
        text = f.read_text()
        assert not any(w in text for w in ("BrainView", "#brain-", "tui.brain")), f.name


def _every_binding():
    import importlib
    import inspect
    import pkgutil
    from textual.screen import ModalScreen
    import pl.tui
    from pl.tui import app as tui_app
    classes = [PlApp, *tui_app.VIEWS.values(), tui_cards.CardActions]
    for info in pkgutil.iter_modules(pl.tui.__path__):
        mod = importlib.import_module(f"pl.tui.{info.name}")
        classes += [c for _, c in inspect.getmembers(mod, inspect.isclass)
                    if issubclass(c, ModalScreen) and c.__module__ == mod.__name__]
    for cls in dict.fromkeys(classes):
        for b in cls.__dict__.get("BINDINGS", []) + cls.__dict__.get("CARD_BINDINGS", []):
            yield cls, b


def test_no_key_has_two_meanings_across_every_view_and_screen():
    from pl.tui.chrome import MEANING
    from pl.tui.review import ConfirmScreen
    from pl.tui.app import DispatcherChoice
    meanings = {}
    for cls, b in _every_binding():
        if issubclass(cls, (ConfirmScreen, DispatcherChoice)):   # y / n / enter / esc answer the dialog's own question
            continue
        assert b.action in MEANING or b.action.split("(")[0] in MEANING, f"{cls.__name__}: {b.action} has no MEANING"
        meaning = MEANING.get(b.action) or MEANING[b.action.split("(")[0]]
        meanings.setdefault(b.key, {}).setdefault(meaning, []).append(cls.__name__)
    # H: the Ideas view's own harness key wins while it has focus; elsewhere H opens Home
    assert {k: m for k, m in meanings.items() if len(m) > 1 and k != "H"} == {}


# ---------- one "needs you" count ----------

async def test_three_prs_and_two_gates_give_5_everywhere(monkeypatch):
    from pl.tui.needs import needs_you_count
    _fake_card_read(monkeypatch, [])
    monkeypatch.setattr(C, "GATES", {})
    data = fake_data()
    data["snapshot"]["rows"] = [r for r in data["snapshot"]["rows"] if not (r.get("card") and r["card"]["id"] in ("c0000003aaaa", "spec0001aaaa"))]
    assert needs_you_count(data) == 5   # decide 1 + merge 2 PRs, 2 plans; the rework PR is not counted
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        text = screen_text(app)
        assert "needs you 5" in text
        assert str(app.query_one("#work-views", TabbedContent).get_tab("cards").label) == "Board 3 · 5 need you"
        assert tile(app, "waiting") == "5"
        app.action_tab("prs")
        await settle(pilot)
        assert "5 need you" in app.query_one("#prs-table").border_title


# ---------- plan 21 slice 6: the P plan filter on the Pipeline tab ----------

def _with_plan():
    data = _with_done()
    for cid, plan, slice_no in (("spec0001aaaa", 21, 5), ("c0000003aaaa", 21, 1), ("f0000001aaaa", 21, 2), ("d0000004aaaa", 3, 1)):
        row = next(r for r in [*data["snapshot"]["rows"], *data["snapshot"]["done"]] if r.get("card") and r["card"]["id"] == cid)
        row["card"]["metadata"].update(plan=plan, slice=slice_no)
    return data


async def _filter_plan(pilot, text):
    await pilot.press("P")
    await pilot.pause()
    await pilot.press(*text, "enter")
    await settle(pilot)


async def test_P_shows_one_plan_ordered_by_slice_with_done_and_columns(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        before = {k: [c.plain for c in app.query_one("#cards-table").get_row(k)] for k in view._rows}
        await _filter_plan(pilot, "21")
        t = app.query_one("#cards-table")
        assert list(view._rows) == ["c0000003aaaa", "f0000001aaaa", "spec0001aaaa"]
        assert "plan 21 · 3 cards · P clears" in t.border_title
        for k in view._rows:
            cells = [c.plain for c in t.get_row(k)]
            assert cells[:3] == before[k][:3] if k in before else True
        assert t.get_row("f0000001aaaa")[3].plain.startswith("Done")
        assert t.get_row("spec0001aaaa")[3].plain.startswith("Spec ready")


async def test_P_equal_slices_sit_together_by_card_id(monkeypatch):
    _fake_card_read(monkeypatch, [])
    data = _with_plan()
    for r in data["snapshot"]["rows"]:
        if r.get("card") and r["card"]["id"] in ("4a000001aaaa", "60000002aaaa"):
            r["card"]["metadata"].update(plan=21, slice=2)
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        await _filter_plan(pilot, "21")
        assert list(view._rows) == ["c0000003aaaa", "4a000001aaaa", "60000002aaaa", "f0000001aaaa", "spec0001aaaa"]


async def test_P_unknown_plan_shows_empty_list_without_crashing(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        await _filter_plan(pilot, "7")
        assert view._rows == {} and "no cards in plan 7" in app.query_one("#cards-table").border_title
        assert "No cards in plan 7." in screen_text(app)


@pytest.mark.parametrize("typed", ["abc", "0", ""])
async def test_P_rejects_bad_numbers_and_esc_changes_nothing(monkeypatch, typed):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        rows = list(view._rows)
        await _filter_plan(pilot, typed)
        assert view.plan is None and list(view._rows) == rows
        assert any("whole number from 1" in str(n.message) for n in app._notifications)
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
        assert view.plan is None and list(view._rows) == rows and len(app.screen_stack) == 1


async def test_P_again_clears_the_filter(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        rows, title = list(view._rows), app.query_one("#cards-table").border_title
        await _filter_plan(pilot, "21")
        await pilot.press("P")
        await settle(pilot)
        assert view.plan is None and list(view._rows) == rows and app.query_one("#cards-table").border_title == title


async def test_plan_filter_survives_refresh(monkeypatch):
    _fake_card_read(monkeypatch, [])
    provider = Provider(_with_plan())
    app = PlApp(snapshot_provider=provider)
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        await _filter_plan(pilot, "21")
        calls = len(provider.threads)
        await pilot.press("r")
        await settle(pilot)
        assert len(provider.threads) > calls and view.plan == 21
        assert list(view._rows) == ["c0000003aaaa", "f0000001aaaa", "spec0001aaaa"]


async def test_card_keys_act_on_the_selected_plan_card(monkeypatch):
    _fake_card_read(monkeypatch, [])
    got = _called(monkeypatch, "move_to", "spec0001aaaa")
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        await _filter_plan(pilot, "21")
        t = app.query_one("#cards-table")
        t.move_cursor(row=t.get_row_index("spec0001aaaa"))
        await settle(pilot)
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert [g[0][0] for g in got] == ["spec0001aaaa"]


async def test_plan_filter_and_needs_you_combine(monkeypatch):
    from pl.tui.needs import needs_me
    _fake_card_read(monkeypatch, [])
    data = _with_plan()
    for r in data["snapshot"]["rows"]:
        if r.get("card") and r["card"]["id"] == "spec0001aaaa":
            r["kind"] = "review"
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        await _filter_plan(pilot, "21")
        await pilot.press("f")
        await settle(pilot)
        assert "spec0001aaaa" in view._rows and "f0000001aaaa" not in view._rows and "d0000004aaaa" not in view._rows
        assert all(needs_me(r) for r in view._rows.values()) and "needs you only" in app.query_one("#cards-table").border_title


async def test_an_empty_board_says_no_cards_and_how_to_add_one():
    data = fake_data()
    data["snapshot"]["rows"] = [r for r in data["snapshot"]["rows"] if r.get("card") is None and not r.get("pr")]
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        text = screen_text(app)
    from pl.tui.chrome import empty_state
    assert all(line in text for line in empty_state("cards"))
    assert "loading" not in text and "not reachable" not in text


def test_empty_state_is_three_lines_for_every_list_tab():
    from pl.tui.chrome import EMPTY, HELP, TABS, empty_state
    assert set(EMPTY) <= {t for t, _ in TABS} and {"ideas", "cards", "prs", "alerts", "loops", "activity", "subagents",
                                                   "dashboard", "home"} == set(EMPTY)
    for tid in EMPTY:
        lines = empty_state(tid)
        assert len(lines) == 3 and all(lines), tid
        assert lines[2] == "? learn more"
        assert lines[0] == HELP[tid].split(". ")[0].rstrip(".") + ".", tid


def test_empty_state_keys_are_real_bindings():
    from pl.tui.app import VIEWS
    from pl.tui.chrome import EMPTY
    for tab, (_, key, key_tab) in EMPTY.items():
        if key is not None:
            assert key in [b.key for b in VIEWS[key_tab].BINDINGS], tab


def test_empty_state_line_two_fits_the_pipeline_title():
    from pl.tui.chrome import EMPTY
    assert all(len(line2) <= tui_cards.TITLE_WIDTH for line2, _, _ in EMPTY.values())


async def test_an_empty_ideas_tab_says_what_it_is_for_and_how_to_add_one(monkeypatch):
    from pl import ideas
    from pl.tui.chrome import empty_state
    monkeypatch.setattr(ideas, "list_ideas", lambda: [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("ideas")
        await settle(pilot)
        text = screen_text(app)
    assert all(line in text for line in empty_state("ideas"))


async def test_the_first_idea_replaces_the_empty_state(monkeypatch):
    from pl import ideas
    from pl.tui.ideas import IdeasView
    monkeypatch.setattr(ideas, "list_ideas", lambda: [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("ideas")
        await settle(pilot)
        assert "? learn more" in screen_text(app)
        app.query_one(IdeasView)._fill([{"id": "abcdefabcdef", "title": "Faster page", "status": "interviewing"}])
        await settle(pilot)
        text = screen_text(app)
    assert "Faster page" in text and "? learn more" not in text


async def test_a_filtered_empty_pipeline_keeps_its_filter_message(monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(_with_plan()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        await _filter_plan(pilot, "7")
        text = screen_text(app)
        assert "No cards in plan 7." in text and "? learn more" not in text


async def test_question_mark_on_an_empty_tab_opens_its_help(monkeypatch):
    from pl import ideas
    from pl.tui.app import HelpScreen
    from pl.tui.chrome import HELP
    monkeypatch.setattr(ideas, "list_ideas", lambda: [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("ideas")
        await settle(pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert HELP["ideas"] in " ".join(screen_text(app).replace("│", " ").split())


async def test_an_empty_prs_tab_says_how_pull_requests_arrive():
    from pl.tui.chrome import empty_state
    data = fake_data()
    data["snapshot"]["rows"] = [r for r in data["snapshot"]["rows"] if not r.get("pr")]
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("prs")
        await settle(pilot)
        text = screen_text(app)
    assert all(line in text for line in empty_state("prs")) and "  none" not in text


async def test_an_empty_alerts_tab_says_how_alerts_arrive(monkeypatch):
    from pl.tui.chrome import empty_state
    data = fake_data()
    data["alerts"] = []
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("alerts")
        await settle(pilot)
        text = screen_text(app)
        assert all(line in text for line in empty_state("alerts"))
        told = []
        monkeypatch.setattr(app, "notify", lambda msg, **kw: told.append(msg))
        await pilot.press("k")
        await pilot.pause()
        assert told == ["select an alert to acknowledge"]


async def test_the_first_alert_replaces_the_empty_state():
    from pl.tui.alerts import AlertsView
    data = fake_data()
    data["alerts"] = []
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("alerts")
        await settle(pilot)
        assert "? learn more" in screen_text(app)
        data["alerts"] = [{"key": "stage_failed:abcd1234:spec", "severity": "high", "title": "Card abcd1234: the spec agent died",
                           "fix": "pl retry abcd1234", "first_seen": NOW.timestamp(), "last_seen": NOW.timestamp(), "count": 1}]
        app.query_one(AlertsView).show(data)
        await settle(pilot)
        text = screen_text(app)
    assert "pl retry abcd1234" in text and "? learn more" not in text


async def test_a_failed_refresh_shows_last_good_time_and_what_failed(monkeypatch):
    prov = Provider()
    app = PlApp(snapshot_provider=prov)
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_cards(pilot, app)
        good = app.last_good_at
        assert good is not None and "data from" not in screen_text(app)
        prov.fail = SystemExit("pl: board API answered Rate exceeded")
        app.refresh_data()
        await settle(pilot)
        text = screen_text(app)
        assert f"data from {good}" in text
        assert "refresh failed: pl: board API answered Rate exceeded" in text
        assert app.query_one("#cards-table").border_subtitle.startswith("refresh failed: ")
        assert app.query_one("#prs-table").border_subtitle.startswith("refresh failed: ")
        prov.fail = None
        app.refresh_data()
        await settle(pilot)
        assert not app.query_one("#cards-table").border_subtitle


def _snap(disp="running", working=0, needs=None, summary="", prof="acme: ok (0 running, 0 queued)", parked=False):
    rows = [{"kind": "working", "card": None}] * working
    return {"rows": rows, "disp": disp, "needs": needs or {}, "summary": summary, "prof": prof, "parked": parked, "at": "14:30:00"}


@pytest.mark.parametrize("snap,stopped,sentence", [
    (_snap(), None, "Running, no cards waiting"),
    (_snap(working=2, needs={"review": 1}), None, "Running, 2 agents working, 1 card waiting"),
    (_snap(working=1, needs={"review": 1, "manual": 1}), None, "Running, 1 agent working, 2 cards waiting"),
    (_snap(disp="NOT RUNNING (no window)"), "19:16", "Stopped (by you at 19:16)"),
    (_snap(disp="NOT RUNNING (no window)"), None, "Stopped"),
    (_snap(summary="PAUSED (draining, 1 working; pl resume)   |   NEEDS YOU: 0"), None, "Paused, draining 1 agent"),
    (_snap(summary="PAUSED (DRAINED, safe to shut down; pl resume)   |   "), None, "Paused, drained"),
    (_snap(prof="acme: ok (0 running, 0 queued)  acme2: PARKED until 21:00, checked 20:10 UTC: still limited (0 running, 0 queued)",
           parked=True), None, "Running, no cards waiting; account acme2 is out of credits until 21:00"),
])
def test_status_sentence(snap, stopped, sentence):
    from pl.tui.chrome import status_sentence
    assert status_sentence(snap, stopped) == sentence


def test_header_never_shows_the_raw_profile_line():
    from pl.tui.chrome import header_text
    data = fake_data()
    plain = header_text(data).plain
    assert "Running, 2 cards waiting" in plain
    assert "acme: ok" not in plain and "dispatcher running" not in plain and "not paused" not in plain


def _loops_state(next_at, last_run):
    C.STATE_FILE.parent.mkdir(exist_ok=True)
    C.STATE_FILE.write_text(json.dumps({"services": {"invoices": {"next_at": next_at, "last_run": last_run}}}))


def test_loops_tab_shows_next_run_last_result_and_week_tokens():
    from pl.tui import loops as tui_loops
    soon = (datetime.now() + timedelta(minutes=90)).replace(microsecond=0).isoformat()
    _loops_state(soon, {"result": "3 invoices overdue", "exit": 0, "tokens": 900})
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False},
                  "triage": {"prompt": "/loop 30m /triage", "profile": "acme"}}
    C.LOOPS_OFF = {"stale": {"prompt": "find stale cards", "every": "daily 09:00", "enabled": False}}
    data = {"snapshot": {"rows": []}, "usage": {"loops": {"invoices": {"tokens_1h": 0, "tokens_7d": 12_500, "context": None, "dead": False}}}}
    rows = {r["name"]: r for r in tui_loops.loop_rows(data)}
    assert rows["invoices"]["next"] == tui_loops.when(soon, datetime.now())
    assert rows["invoices"]["output"] == "ok, 3 invoices overdue"
    assert rows["invoices"]["tokens7d"] == "12.5k"
    assert rows["triage"]["tokens7d"] == "-" and rows["triage"]["next"] == "-"
    assert rows["invoices"]["every"] == "hourly" and rows["triage"]["every"] is None
    assert rows["stale"]["state"] == "paused" and rows["stale"]["next"] == "-"


async def test_loops_keys_edit_pause_run_delete_and_window(monkeypatch):
    from pl import agents, loops, skills
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False}}
    C.LOOPS_OFF = {}
    calls = []
    monkeypatch.setattr(loops, "edit", lambda name, **kw: calls.append(("edit", name, kw)))
    monkeypatch.setattr(loops, "set_enabled", lambda name, on: calls.append(("enabled", name, on)))
    monkeypatch.setattr(loops, "run", lambda name, **kw: calls.append(("run", name)))
    monkeypatch.setattr(loops, "remove", lambda name: calls.append(("remove", name)))
    monkeypatch.setattr(agents, "jump_to_window", lambda name: calls.append(("window", name)) or "ok")
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    monkeypatch.setattr(skills, "scan", lambda: [])
    data = fake_data()
    data["snapshot"]["rows"].append({"loop": "invoices", "kind": "loop_idle", "worker": {"pane": "%1", "window": "@7"}})
    app = PlApp(snapshot_provider=Provider(data))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        assert [r["name"] for r in app.query_one(tui_loops.LoopsView).loops] == ["invoices"]
        await pilot.press("p")
        await settle(pilot)
        assert calls == []   # the cursor starts on the "+ New" row, so p does nothing there
        await pilot.press("down")
        await settle(pilot)
        await pilot.press("p")
        await settle(pilot)
        await pilot.press("g")
        await settle(pilot)
        await pilot.press("w")
        await settle(pilot)
        await pilot.press("d")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.ConfirmScreen) and ("remove", "invoices") not in calls
        await pilot.press("y")
        await settle(pilot)
        await pilot.press("e")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm)
        assert "Edit loop invoices" in str(app.screen.query_one("Static").render())
        app.screen.query_one("#loop-kind", Select).value = "daily"
        await settle(pilot)
        app.screen.query_one("#loop-at").value = "09:30"
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert calls == [("enabled", "invoices", False), ("run", "invoices"), ("window", "invoices"), ("remove", "invoices"),
                     ("edit", "invoices", {"every": "daily 09:30"})]


def test_dashboard_health_lists_every_harness_account():
    from pl.tui.dashboard import health
    data = fake_data()
    data["snapshot"]["prof"] = "acme: ok (2 running, 1 queued)  acme2: PARKED until 21:00 (0 running, 0 queued)"
    plain = health(data).plain
    assert "acme: ok (2 running, 1 queued)" in plain and "acme2: PARKED until 21:00" in plain


async def _open_loop_form(app, pilot, monkeypatch):
    from pl import loops, skills
    from pl.tui import loops as tui_loops
    monkeypatch.setattr(skills, "scan", lambda: [{"name": "pl-billing-chase"}])
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    await settle(pilot)
    await pilot.press("5")
    await settle(pilot)
    await pilot.press("n")
    await settle(pilot)
    assert isinstance(app.screen, tui_loops.TemplateGallery)
    app.screen.query_one("#gallery-table").move_cursor(row=len(loops.TEMPLATES))
    await pilot.press("enter")
    await settle(pilot)
    assert isinstance(app.screen, tui_loops.LoopForm)
    return app.screen


async def _open_gallery(app, pilot, monkeypatch):
    from pl import skills
    from pl.tui import loops as tui_loops
    monkeypatch.setattr(skills, "scan", lambda: [])
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    await settle(pilot)
    await pilot.press("5")
    await settle(pilot)
    await pilot.press("n")
    await settle(pilot)
    assert isinstance(app.screen, tui_loops.TemplateGallery)
    return app.screen


def test_gallery_rows_say_what_each_template_does_and_needs():
    from pl import loops
    from pl.tui import loops as tui_loops
    rows = {r[0]: r for r in tui_loops.gallery_rows()}
    assert [r[0] for r in tui_loops.gallery_rows()] == [*loops.TEMPLATES, "Blank loop"]
    needs = {"auto-review": "write access", "inbox-triage": "email connector", "overdue-invoices": "payments connector",
             "daily-standup": "nothing extra", "dependency-updates": "nothing extra", "stale-cards": "nothing extra"}
    assert {n: rows[n][2] for n in needs} == needs
    assert rows["inbox-triage"][1] == "Read the new mail through the email connector and group it as urgent, reply-needed or FYI."
    assert rows["auto-review"][3] == "every 30m, 08:00-20:00"


async def test_n_opens_the_template_gallery_first(monkeypatch):
    from pl import loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        gallery = await _open_gallery(app, pilot, monkeypatch)
        table = gallery.query_one("#gallery-table")
        assert table.row_count == len(loops.TEMPLATES) + 1
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert names == [*loops.TEMPLATES, "Blank loop"]


async def test_choosing_a_template_in_the_gallery_prefills_the_form(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append((name, kw)))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        gallery = await _open_gallery(app, pilot, monkeypatch)
        gallery.query_one("#gallery-table").move_cursor(row=list(loops.TEMPLATES).index("overdue-invoices"))
        await pilot.press("enter")
        await settle(pilot)
        form = app.screen
        assert isinstance(form, tui_loops.LoopForm)
        template = loops.TEMPLATES["overdue-invoices"]
        assert form.query_one("#loop-template").value == "overdue-invoices"
        assert form.query_one("#loop-prompt").value == template["prompt"]
        assert form._every() == template["every"]
        assert form.query_one("#loop-hours").value == template["hours"]
        assert form.query_one("#loop-writes").value is False
        assert all(form.query_one(f"#loop-{k}").value == "" for k in ("name", "account", "team"))
        form.query_one("#loop-kind").value = "minutes"
        form.query_one("#loop-number").value = "60"
        form.query_one("#loop-name").value = "invoices"
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert added == [("invoices", {"skill": None, "prompt": template["prompt"], "every": "60m", "hours": template["hours"],
                                   "account": None, "team": None, "writes": False})]


async def test_blank_loop_opens_an_empty_form(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    from textual.widgets import Input, Select
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append(name))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        gallery = await _open_gallery(app, pilot, monkeypatch)
        gallery.query_one("#gallery-table").move_cursor(row=len(loops.TEMPLATES))
        await pilot.press("enter")
        await settle(pilot)
        form = app.screen
        assert isinstance(form, tui_loops.LoopForm)
        assert all(i.value == "" for i in form.query(Input))
        assert form._every() == ""
        assert form.query_one("#loop-template").value is Select.NULL
        assert form.query_one("#loop-writes").value is False
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, tui_loops.LoopForm)
    assert added == []


async def test_esc_in_the_gallery_opens_no_form(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append(name))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await _open_gallery(app, pilot, monkeypatch)
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, (tui_loops.TemplateGallery, tui_loops.LoopForm))
    assert added == []


NIGHTLY = {"skill": "pl-billing-chase", "prompt": "/pl-billing-chase", "every": "daily 09:00", "hours": "09-18",
           "profile": "acme", "writes": True}


async def _open_edit_form(app, pilot, monkeypatch, edit=None):
    from pl import loops, skills
    from pl.tui import loops as tui_loops
    edits = []
    monkeypatch.setattr(loops, "edit", edit or (lambda name, **kw: edits.append((name, kw))))
    monkeypatch.setattr(loops, "add", lambda name, **kw: edits.append(("add", name, kw)))
    monkeypatch.setattr(skills, "scan", lambda: [{"name": "pl-billing-chase"}])
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    await settle(pilot)
    await pilot.press("5")
    await settle(pilot)
    await pilot.press("down")
    await pilot.press("e")
    await settle(pilot)
    assert isinstance(app.screen, tui_loops.LoopForm)
    return app.screen, edits


def _pick_every(form, kind, number="", cron=""):
    from textual.widgets import Select
    form.query_one("#loop-kind", Select).value = kind
    form.query_one("#loop-number").value = number
    form.query_one("#loop-cron").value = cron


def _edit_app():
    C.SERVICES = {"nightly": dict(NIGHTLY)}
    C.LOOPS_OFF = {}
    return PlApp(snapshot_provider=Provider(fake_data()))


async def test_edit_opens_the_loop_form_filled_with_the_loops_values(monkeypatch):
    from textual.widgets import Checkbox, Select
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, _ = await _open_edit_form(app, pilot, monkeypatch)
        assert "Edit loop nightly" in str(form.query_one("Static").render())
        assert form.query_one("#loop-kind", Select).value == "daily"
        assert form.query_one("#loop-at").value == "09:00"
        assert form.query_one("#loop-hours").value == "09-18"
        assert form.query_one("#loop-account").value == "acme"
        assert form.query_one("#loop-skill", Select).value == "pl-billing-chase"
        assert form.query_one("#loop-writes", Checkbox).value is True
        assert not form.query("#loop-template")


async def test_edit_form_locks_the_name_and_never_adds(monkeypatch):
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, edits = await _open_edit_form(app, pilot, monkeypatch)
        name = form.query_one("#loop-name")
        assert name.disabled and name.value == "nightly"
        assert "exists" not in str(form.query_one("#loop-problem").render())
        _pick_every(form, "minutes", "30")
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert edits == [("nightly", {"every": "30m"})]


async def test_edit_saves_only_the_changed_field(monkeypatch):
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, edits = await _open_edit_form(app, pilot, monkeypatch)
        _pick_every(form, "minutes", "30")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert not isinstance(app.screen, type(form))
    assert edits == [("nightly", {"every": "30m"})]


async def test_edit_with_no_change_writes_nothing(monkeypatch):
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, edits = await _open_edit_form(app, pilot, monkeypatch)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert app.screen is not form
        form, edits2 = await _open_edit_form(app, pilot, monkeypatch)
        form.query_one("#loop-hours").value = ""
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert app.screen is not form
    assert edits == [] and edits2 == []


async def test_edit_refuses_a_bad_every_on_the_problem_line(monkeypatch):
    from pl.tui import loops as tui_loops
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, edits = await _open_edit_form(app, pilot, monkeypatch)
        _pick_every(form, "cron", cron="soon")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm)
        assert str(form.query_one("#loop-problem").render()).strip()
    assert edits == []


async def test_edit_refusal_shows_the_cli_message(monkeypatch):
    from pl.tui import loops as tui_loops

    def refuse(name, **kw):
        print("pl: invalid account", file=sys.stderr)
        raise SystemExit(1)
    app = _edit_app()
    notes = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append((str(msg), kw.get("severity"))))
    async with app.run_test(size=(176, 48)) as pilot:
        form, _ = await _open_edit_form(app, pilot, monkeypatch, edit=refuse)
        form.query_one("#loop-account").value = "nope"
        await pilot.press("ctrl+s")
        await settle(pilot)
        await settle(pilot)
        assert not isinstance(app.screen, tui_loops.LoopForm)
    assert notes == [("pl: invalid account", "error")]


async def test_saved_edit_shows_on_the_next_refresh(monkeypatch):
    from pl.tui import loops as tui_loops

    def edit(name, **kw):
        C.SERVICES[name].update(kw)
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, _ = await _open_edit_form(app, pilot, monkeypatch, edit=edit)
        view = app.query_one(tui_loops.LoopsView)
        before = view.loops[0]["schedule"]
        _pick_every(form, "minutes", "30")
        await pilot.press("ctrl+s")
        await settle(pilot)
        await settle(pilot)
        view.show(fake_data())
        assert view.loops[0]["schedule"] != before and "30" in view.loops[0]["schedule"]


async def test_escape_closes_the_edit_form_without_writing(monkeypatch):
    app = _edit_app()
    async with app.run_test(size=(176, 48)) as pilot:
        form, edits = await _open_edit_form(app, pilot, monkeypatch)
        _pick_every(form, "minutes", "30")
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is not form
    assert edits == []


async def test_picking_a_template_fills_every_field_and_each_stays_editable(monkeypatch):
    from pl import loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append((name, kw)))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-template").value = "overdue-invoices"
        await settle(pilot)
        template = loops.TEMPLATES["overdue-invoices"]
        assert form.query_one("#loop-prompt").value == template["prompt"]
        assert form.query_one("#loop-kind").value == "weekdays"
        assert form.query_one("#loop-at").value == "09:00"
        assert form.query_one("#loop-hours").value == template["hours"]
        assert form.query_one("#loop-writes").value is False
        form.query_one("#loop-kind").value = "minutes"
        form.query_one("#loop-number").value = "1"
        form.query_one("#loop-unit").value = "hours"
        form.query_one("#loop-name").value = "invoices"
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert added == [("invoices", {"skill": None, "prompt": template["prompt"], "every": "60m", "hours": template["hours"],
                                   "account": None, "team": None, "writes": False})]


async def test_add_form_rejects_a_bad_every_with_the_forms(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append(name))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-name").value = "Bad Name"
        form.query_one("#loop-prompt").value = "do it"
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm) and "a-z" in str(form.query_one("#loop-problem").render())
        form.query_one("#loop-name").value = "ok"
        form.query_one("#loop-kind").value = "cron"
        form.query_one("#loop-cron").value = "every tuesday"
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm)
        problem = str(form.query_one("#loop-problem").render())
        assert all(f in problem for f in ("30m", "hourly", "daily HH:MM", "weekdays HH:MM", "cron:"))
    assert added == []


async def test_add_form_offers_four_schedule_kinds(monkeypatch):
    from pl import schedule
    C.SERVICES, C.LOOPS_OFF = {}, {}
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        labels = [label for label, _ in form.query_one("#loop-kind", Select)._options if str(label)]
        assert labels == [label for _, label in schedule.KINDS] and len(labels) == 4
        assert "no schedule is set" in str(form.query_one("#loop-preview").render())


async def test_weekdays_preview_updates_as_you_type(monkeypatch):
    from pl.tui import loops as tui_loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    monkeypatch.setattr(tui_loops.LoopForm, "_now", staticmethod(lambda: datetime(2026, 10, 30, 10, 0)))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-kind").value = "weekdays"
        await settle(pilot)
        form.query_one("#loop-at").focus()
        await pilot.press(*"09:00")
        await settle(pilot)
        preview = str(form.query_one("#loop-preview").render())
    assert all(t in preview for t in ("Mon 02 Nov 09:00", "Tue 03 Nov 09:00", "Wed 04 Nov 09:00"))


async def test_picker_saves_the_every_loops_add_accepts(monkeypatch):
    from pl import loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append((name, kw["every"])))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-name").value = "first"
        form.query_one("#loop-prompt").value = "do it"
        form.query_one("#loop-kind").value = "minutes"
        form.query_one("#loop-number").value = "2"
        form.query_one("#loop-unit").value = "hours"
        await pilot.press("ctrl+s")
        await settle(pilot)
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-name").value = "second"
        form.query_one("#loop-prompt").value = "do it"
        form.query_one("#loop-kind").value = "cron"
        form.query_one("#loop-cron").value = "0 9 * * 1"
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert added == [("first", "120m"), ("second", "cron:0 9 * * 1")]


async def test_bad_or_never_firing_schedule_blocks_save(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append(name))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-name").value = "ok"
        form.query_one("#loop-prompt").value = "do it"
        form.query_one("#loop-kind").value = "cron"
        for cron, words in (("61 * * * *", "not valid"), ("0 0 31 2 *", "never fires")):
            form.query_one("#loop-cron").value = cron
            await settle(pilot)
            preview = form.query_one("#loop-preview").render()
            assert words in str(preview) and "red" in str(preview.spans)
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert isinstance(app.screen, tui_loops.LoopForm)
            assert words in str(form.query_one("#loop-problem").render())
    assert added == []


async def test_picking_a_skill_after_a_template_drops_the_template_prompt(monkeypatch):
    from pl import loops
    C.SERVICES, C.LOOPS_OFF = {}, {}
    added = []
    monkeypatch.setattr(loops, "add", lambda name, **kw: added.append((name, kw)))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        form = await _open_loop_form(app, pilot, monkeypatch)
        form.query_one("#loop-template").value = "overdue-invoices"
        await settle(pilot)
        form.query_one("#loop-skill").value = "pl-billing-chase"
        form.query_one("#loop-name").value = "invoices"
        await pilot.press("ctrl+s")
        await settle(pilot)
    assert added[0][1]["skill"] == "pl-billing-chase" and added[0][1]["prompt"] is None


def test_loop_row_reads_in_plain_words():
    from pl.tui import loops as tui_loops
    soon = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    _loops_state(soon, None)
    C.SERVICES = {"invoices": {"prompt": "Review the open pull requests that wait for review: label them", "profile": "acme",
                               "every": "30m", "writes": False},
                  "triage": {"skill": "triage", "prompt": "/triage", "profile": "acme", "every": "daily 09:00", "writes": True},
                  "watch": {"prompt": "/loop 30m check the board", "profile": "acme"}}
    C.LOOPS_OFF = {}
    rows = {r["name"]: r for r in tui_loops.loop_rows({"snapshot": {"rows": []}})}
    assert rows["invoices"]["does"] == "Review the open pull requests that wait for review"
    assert rows["invoices"]["schedule"] == "every 30 minutes" and rows["invoices"]["read_only"] is True
    assert rows["invoices"]["next"] == tui_loops.when(soon, datetime.now())
    assert rows["triage"]["does"] == "runs /triage" and rows["triage"]["read_only"] is False
    assert rows["watch"]["does"] == "check the board" and rows["watch"]["schedule"] == "every 30 minutes"
    assert rows["watch"]["read_only"] is False


def test_loop_sentence():
    from pl.tui import loops as tui_loops
    row = {"does": "Review the open pull requests that wait for review", "schedule": "every 30 minutes",
           "next": "16:30", "output": "ok, 3 PRs"}
    assert tui_loops.sentence(row) == ("Review the open pull requests that wait for review every 30 minutes; "
                                       "next at 16:30; last run ok, 3 PRs")
    assert "next at" not in tui_loops.sentence({**row, "next": "-"})


def test_failed_last_run_says_what_failed():
    from pl.tui import loops as tui_loops
    assert tui_loops.last_result({"exit": 1, "result": "gh: not logged in"}) == "failed: gh: not logged in (exit 1)"
    assert tui_loops.last_result({"exit": 2}) == "failed: no output (exit 2)"
    assert tui_loops.last_result({"exit": 0, "result": "3 PRs"}) == "ok, 3 PRs"
    assert tui_loops.last_result({"result": "3 PRs"}) == "ok, 3 PRs"
    assert tui_loops.last_result({"exit": 0}) == "ok"
    assert tui_loops.last_result(None) == "not run yet"
    assert tui_loops.last_result({}) == "not run yet"
    assert tui_loops.last_result({"exit": -1, "result": "unreadable run record"}) == "unreadable run record"


def test_when_formats_a_next_run_short():
    from pl.tui import loops as tui_loops
    now = datetime(2026, 10, 25, 12, 0)
    assert tui_loops.when("2026-10-25T16:30:00", now) == "16:30"
    assert tui_loops.when("2026-10-26T09:00:00", now) == "tomorrow 09:00"
    assert tui_loops.when("2026-10-30T09:00:00", now) == "Fri 09:00"
    assert tui_loops.when("-", now) == "-" and tui_loops.when("in ~5m", now) == "in ~5m"


def _loops_log(name, lines):
    d = C.STATE_DIR / "loops"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.log").write_text("\n".join(lines) + "\n")


async def test_loop_log_hides_secrets(monkeypatch):
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False}}
    C.LOOPS_OFF = {}
    _loops_log("invoices", ["token ghp_" + "a" * 36])
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        await pilot.press("down", "enter")   # row 0 is + New loop
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LogScreen)
        assert "ghp_" not in screen_text(app)


async def test_enter_on_a_loop_opens_its_log(monkeypatch):
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False}}
    C.LOOPS_OFF = {}
    _loops_log("invoices", [f"line {i} [bold]" for i in range(300)])
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        await pilot.press("down", "enter")   # row 0 is + New loop
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LogScreen)
        text = screen_text(app)
        assert "line 299 [bold]" in text and "line 99 " not in text
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, tui_loops.LogScreen)


async def test_enter_without_a_log_says_so(monkeypatch):
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False}}
    C.LOOPS_OFF = {}
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    notes = []
    app = PlApp(snapshot_provider=Provider(fake_data()))
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append(msg))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        await pilot.press("down", "enter")   # row 0 is + New loop
        await settle(pilot)
        assert not isinstance(app.screen, tui_loops.LogScreen)
        assert any("no log yet for invoices" in n for n in notes)


def test_new_row_reads_plus_new_thing_and_is_dim():
    from pl.tui.chrome import NEW_ROW, new_row
    row = new_row("loop")
    assert row.plain == "+ New loop" and "dim" in str(row.style)
    assert NEW_ROW == "+new"


async def test_loops_tab_first_row_is_new_loop_and_enter_and_n_open_the_form(monkeypatch):
    from pl import loops
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly", "writes": False}}
    C.LOOPS_OFF = {}
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        t = app.query_one("#loops-table", DataTable)
        assert t.get_row_at(0)[0].plain == "+ New loop" and t.row_count == 2
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.TemplateGallery)
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        app.screen.query_one("#gallery-table").move_cursor(row=len(loops.TEMPLATES))
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm)
        assert app.screen.query_one("#loop-kind").value is Select.NULL
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, tui_loops.LoopForm)
        await pilot.press("down")
        await pilot.press("n")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.TemplateGallery)


async def test_add_form_shows_a_refusal_of_pl_loops_add_under_its_field_and_stays_open(monkeypatch, tmp_path):
    from pl.tui import loops as tui_loops
    profile = tmp_path / ".pl-fake"
    profile.mkdir()
    (profile / "config.toml").write_text("")
    C.load(config_dir=str(profile))
    monkeypatch.setattr(tui_loops.LoopsView, "load_tail", lambda self: None)
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        await pilot.press("end", "enter")
        await settle(pilot)
        form = app.screen
        form.query_one("#loop-name").value = "chase"
        form.query_one("#loop-prompt").value = "chase invoices"
        form.query_one("#loop-hours").value = "never o'clock"
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert isinstance(app.screen, tui_loops.LoopForm)
        assert str(form.query_one("#loop-hours-error").render()).strip()
        assert not str(form.query_one("#loop-problem").render()).strip()
    assert "chase" not in (profile / "config.toml").read_text()


EXTEND_VIEWS = ["skills", "connectors", "pods", "models", "extensions"]


def _fake_extend_cli(monkeypatch):
    """Every pl subcommand the Extend panels run, answered with an empty or one-row list; returns the argv log."""
    from pl.tui import connectors, panels, skills
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        out = json.dumps([{"name": "conn1", "type": "github", "status": "connected"}]) \
            if argv[:3] == ["pl", "connectors", "list"] else "[]"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    for mod in (connectors, panels, skills):
        monkeypatch.setattr(mod, "_run", fake_run, raising=False)
    return calls


def test_extend_tab_lists_its_five_sub_tabs_in_order():
    from pl.tui.chrome import GROUP_KEYS, GROUPS
    views = next(g[2] for g in GROUPS if g[0] == "extend")
    assert [v[0] for v in views] == EXTEND_VIEWS
    assert [v[1] for v in views] == ["Skills", "Connectors", "Pods", "Models", "Extensions"]
    assert GROUP_KEYS["extend"] == "6"


def test_a_chat_view_id_does_not_resolve():
    from pl.tui import chrome
    assert chrome.resolve("chat") is None


async def test_six_opens_extend_on_skills(monkeypatch):
    from pl.tui.skills import SkillsView
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("6")
        await settle(pilot)
        assert app.active_tab == "skills"
        assert app.query_one("#extend-views").query_one(SkillsView)


@pytest.mark.parametrize("vid,panel", [("skills", "SkillsView"), ("connectors", "ConnectorsPanel"), ("pods", "CommandPanel"),
                                       ("models", "CommandPanel"), ("extensions", "ExtensionsView")])
async def test_each_extend_sub_tab_shows_its_panel(monkeypatch, vid, panel):
    calls = _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab(vid)
        await settle(pilot)
        assert app.active_tab == vid
        assert [type(w).__name__ for w in app.query(f"#{vid} > VerticalScroll > *")] == [panel]
        if vid == "connectors":
            table = app.query_one("#connectors").query_one(DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("t")
            await settle(pilot)
            assert ["pl", "connectors", "test", "conn1"] in calls


async def test_extend_fits_80_columns(monkeypatch):
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        app.action_tab("connectors")
        await settle(pilot)
        row = next(line for line in screen_text(app).splitlines() if "Extend" in line)
        assert "Extend" in row
        subs = next(line for line in screen_text(app).splitlines() if "Skills" in line and "Extensions" in line)
        assert all(label in subs for label in ("Skills", "Connectors", "Pods", "Models", "Extensions"))
        app.query_one("#connectors-table").focus()
        await settle(pilot)
        assert {"test", "revoke"} <= _footer_keys(app)


async def test_palette_skills_opens_extend_skills(monkeypatch):
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_skills()
        await settle(pilot)
        assert app.active_tab == "skills"
        assert app.focused is not None and app.focused.id == "skills-table"
        texts = [str(c.help) for c in app.get_system_commands(app.screen) if c.title.startswith("Skills")]
        assert any("(Extend tab)" in t and "(Settings tab)" not in t for t in texts)


@pytest.mark.parametrize("vid", EXTEND_VIEWS)
async def test_tab_flag_opens_extend_views(monkeypatch, vid):
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider(), tab=vid)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.active_tab == vid
        assert app.query_one("#tabs", TabbedContent).active == "g-extend"


# ---------- plan 36 slice 6: Skills under Extend ----------

SKILLS_KEYS = [("n", "new"), ("I", "install"), ("e", "edit"), ("E", "external_edit"),
               ("d", "delete"), ("S", "share"), ("l", "link"), ("u", "update")]


async def test_settings_shows_one_skills_moved_line(monkeypatch):
    from pl.tui.skills import SkillsView
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        line = app.query_one("#skills-moved")
        assert str(line.label) == "Skills moved to Extend → Skills"
        assert line.size.height == 1
        assert not list(app.query_one("#settings-scroll").query(SkillsView))


@pytest.mark.parametrize("how", ["enter", "click"])
async def test_skills_moved_line_opens_extend_skills(monkeypatch, how):
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        if how == "enter":
            app.query_one("#skills-moved").focus()
            await pilot.pause()
            await pilot.press("enter")
        else:
            app.query_one("#skills-moved").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#skills-moved")
        await settle(pilot)
        assert app.active_tab == "skills"
        assert app.focused is not None and app.focused.id == "skills-table"


async def test_create_project_button_still_asks(monkeypatch, tmp_path):
    from pl.tui.settings import ProjectScreen
    _fake_extend_cli(monkeypatch)
    monkeypatch.setattr(C, "TRACKER", {"type": "github-project", "owner": "octo"})
    monkeypatch.setattr(C, "path", lambda: tmp_path / "pl.toml")
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        app.query_one("#create-project").press()
        await settle(pilot)
        assert isinstance(app.screen, ProjectScreen)


@pytest.mark.parametrize("key,name", SKILLS_KEYS)
async def test_every_skills_key_runs_its_action_on_extend(monkeypatch, key, name):
    from pl.tui.skills import SkillsView
    _fake_extend_cli(monkeypatch)
    ran = []
    monkeypatch.setattr(SkillsView, f"action_{name}", lambda self: ran.append(name))
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_skills()
        await settle(pilot)
        await pilot.press(key)
        await settle(pilot)
        assert ran == [name]


async def test_extend_skills_footer_lists_every_skills_key(monkeypatch):
    _fake_extend_cli(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_skills()
        await settle(pilot)
        assert {"new skill", "install", "edit", "$EDITOR", "remove", "share to library", "link to account",
                "update check"} <= _footer_keys(app)


# ---------- waiting for you (plan 40, slice 4) ----------

def _waiting_card(monkeypatch, cid="wait0001aaaa", desc="", col="In progress", status="alive", reg=None, blocked=None,
                  tmux_calls=None, **meta):
    from pl import agents
    from pl.tui import needs
    c = {"id": cid, "title": "A waiting card", "list_id": col, "tags": [], "updated_at": _iso(1), "description": desc,
         "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}
    calls = tmux_calls if tmux_calls is not None else []
    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(watch, "cards", lambda: [c])
    monkeypatch.setattr(watch, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(watch, "registry", lambda: reg or {})
    monkeypatch.setattr(watch, "worker_status", lambda w, r: (status, None))
    monkeypatch.setattr(agents, "worker_status", lambda w, r: (status, None))
    monkeypatch.setattr(watch, "run_blocked", lambda card: blocked)
    monkeypatch.setattr(agents, "pane_exists", lambda pane: False)
    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    monkeypatch.setattr(agents.harnesses, "_run", fake_run)
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    snap = watch.watch_snapshot()
    row = next(r for r in snap["rows"] if r.get("card"))
    return row, needs.needs_you({"snapshot": snap}), snap


def test_ac1_an_open_question_reads_waiting_for_you_with_its_text(monkeypatch):
    desc = "# PIPELINE: QUESTIONS\nQ: first thing?\nA: yes\n\nQ: Which database?\nsecond line"
    row, _, _ = _waiting_card(monkeypatch, desc=desc, question="h1")
    assert row["approved"] == "waiting for you: Which database?"
    long = "Q: " + "x" * 80
    row, _, _ = _waiting_card(monkeypatch, desc=f"# PIPELINE: QUESTIONS\n{long}", question="h1")
    assert len(row["approved"]) == len("waiting for you: ") + 48 and row["approved"].endswith("…")
    row, _, _ = _waiting_card(monkeypatch, desc="# PIPELINE: QUESTIONS\nno entry here", question="h1")
    assert row["approved"] == "waiting for you: open question"


def test_ac2_a_permission_wait_reads_waiting_for_you_permission(monkeypatch):
    row, _, _ = _waiting_card(monkeypatch, worker={"stage": "run", "permission_wait": "Bash(rm -rf build)"})
    assert row["approved"] == "waiting for you: permission Bash(rm -rf build)"
    row, _, _ = _waiting_card(monkeypatch, worker={"stage": "run", "permission_wait": "waiting: permission"})
    assert row["approved"] == "waiting for you: permission"


def test_ac3_a_trust_wait_reads_waiting_for_you_trust_folder(monkeypatch):
    row, _, _ = _waiting_card(monkeypatch, worker={"stage": "plan", "trust_wait": "/work/app"})
    assert row["approved"] == "waiting for you: trust /work/app"


def test_ac4_a_live_run_agent_at_a_gate_reads_the_gate_line(monkeypatch):
    w = {"stage": "run", "run_waiting": "GATE: before-push"}
    row, _, _ = _waiting_card(monkeypatch, worker=w)
    assert row["approved"] == "waiting for you: GATE: before-push"
    row, _, _ = _waiting_card(monkeypatch, worker=w, status="dead")
    assert not (row["approved"] or "").startswith("waiting for you")


def test_ac5_a_waiting_card_counts_once_in_needs_you(monkeypatch):
    from pl.tui.needs import needs_me
    row, out, snap = _waiting_card(monkeypatch, question="h1", desc="# PIPELINE: QUESTIONS\nQ: Which?", blocked="nothing-runnable")
    assert row["kind"] == "needs" and needs_me(row)
    assert [r["card"]["id"] for r in out] == ["wait0001aaaa"]


def test_ac6_question_wins_over_trust_permission_and_gate(monkeypatch):
    w = {"stage": "run", "trust_wait": "/work", "permission_wait": "Bash(ls)", "run_waiting": "GATE: before-push"}
    row, _, _ = _waiting_card(monkeypatch, desc="# PIPELINE: QUESTIONS\nQ: Which?", question="h1", worker=w)
    assert row["approved"] == "waiting for you: Which?"
    row, _, _ = _waiting_card(monkeypatch, worker=w)
    assert row["approved"] == "waiting for you: trust /work"
    row, _, _ = _waiting_card(monkeypatch, worker={**w, "trust_wait": None})
    assert row["approved"] == "waiting for you: permission Bash(ls)"
    row, _, _ = _waiting_card(monkeypatch, worker={**w, "trust_wait": None, "permission_wait": None})
    assert row["approved"] == "waiting for you: GATE: before-push"


def test_ac7_a_cleared_wait_returns_the_previous_state(monkeypatch):
    w = {"stage": "run", "permission_wait": "Bash(ls)"}
    row, out, _ = _waiting_card(monkeypatch, worker=w)
    assert row["kind"] == "needs" and len(out) == 1
    row, out, _ = _waiting_card(monkeypatch, worker={"stage": "run"})
    assert row["kind"] != "needs" and not (row["approved"] or "").startswith("waiting for you") and out == []


def test_ac8_waiting_states_add_no_capture_pane_calls(monkeypatch):
    without, with_wait = [], []
    _waiting_card(monkeypatch, worker={"stage": "run"}, tmux_calls=without)
    _waiting_card(monkeypatch, worker={"stage": "run", "permission_wait": "Bash(ls)", "run_waiting": "GATE: before-push"},
                  tmux_calls=with_wait)
    def captures(calls):
        return sum("capture-pane" in a for a in calls)
    assert captures(with_wait) == captures(without)


def test_ac9_markup_and_secrets_in_wait_text_show_plain_and_masked(monkeypatch):
    row, _, _ = _waiting_card(monkeypatch, worker={"stage": "run", "permission_wait": "echo [red]x[/] ghp_" + "a" * 36})
    assert "[red]x[/]" in row["approved"] and "ghp_" not in row["approved"] and "***" in row["approved"]


def _detail_runs():
    first = datetime(2026, 10, 26, 9, 0, 0).astimezone()
    newest = {"started_at": first.isoformat(), "ended_at": (first + timedelta(seconds=125)).isoformat(), "ok": True,
              "reason": "", "output": ["checked 4", "3 invoices overdue"]}
    older = {"started_at": (first - timedelta(days=1)).isoformat(), "ended_at": (first - timedelta(days=1)).isoformat(),
             "ok": False, "reason": "usage limit", "output": ["stopped"]}
    return first, [newest, older]


def test_loop_detail_shows_last_run_output_and_earlier_runs():
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly"}}
    C.LOOPS_OFF = {}
    row = tui_loops.loop_rows({"snapshot": {"rows": []}})[0]
    first, runs = _detail_runs()
    plain = tui_loops.run_detail(row, runs).plain
    assert "What it does: list overdue invoices" in plain and "Account acme" in plain
    assert f"Last run {first:%H:%M} · ok · 2m 5s" in plain
    assert "3 invoices overdue" in plain and "Earlier runs" in plain and "failed: usage limit" in plain


def test_loop_detail_with_no_runs_says_when_it_runs():
    from pl.tui import loops as tui_loops
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly"}}
    C.LOOPS_OFF = {}
    row = tui_loops.loop_rows({"snapshot": {"rows": []}})[0]
    plain = tui_loops.run_detail(row, []).plain
    assert "No run yet" in plain and "Earlier runs" not in plain
    bad = tui_loops.run_detail(row, [{"started_at": "bad", "ended_at": "worse", "ok": True, "output": []}]).plain
    assert "Last run ?" in bad


async def test_loops_view_shows_run_detail_for_a_scheduled_loop(monkeypatch):
    from pl import agents
    C.SERVICES = {"invoices": {"prompt": "list overdue invoices", "profile": "acme", "every": "hourly"}}
    C.LOOPS_OFF = {}
    folder = C.STATE_DIR / "loops" / "invoices"
    folder.mkdir(parents=True)
    _, runs = _detail_runs()
    (folder / "runs.json").write_text(json.dumps(runs[::-1]))
    monkeypatch.setattr(agents, "pane_tail", lambda *a, **k: pytest.fail("a scheduled loop reads its run log, not tmux"))
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("5")
        await settle(pilot)
        await pilot.press("down")
        await settle(pilot)
        await settle(pilot)
        assert "Last run" in str(app.query_one("#loops-screen").visual)
