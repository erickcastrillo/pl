"""WP46: pl standup, a short summary of the last 24 hours to paste in chat, and the Dashboard's s key."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from pl import cli, standup, watch
from pl import config as C
from pl.tui.app import PlApp

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=24)


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _write(*evs):
    """(kind, hours ago, card, extra) per event."""
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.STATE_DIR / "events.jsonl", "a") as f:
        for kind, ago, card, extra in evs:
            ts = (NOW - timedelta(hours=ago)).isoformat()
            f.write(json.dumps({"ts": ts, "profile": "t", "kind": kind, "card": card, **extra}) + "\n")


def _card(cid, title, col, kind="row", hours=5):
    c = {"id": cid, "title": title, "list_id": col, "updated_at": (NOW - timedelta(hours=hours)).isoformat(),
         "metadata": {"pipeline_mode": "auto", "profile": "acme"}, "description": "SECRET BODY"}
    return {"kind": kind, "card": c, "worker": {}, "win": "", "col": col, "text": cid}


def _snap(rows=None, attention=0):
    return {"rows": rows or [], "needs": {"review": 0, "attention": attention, "failed": 0, "manual": 0},
            "prof": "", "summary": "", "summary2": "", "disp": "running", "at": "12:00:00", "parked": False}


def _rows():
    return [_card("aaaa0001", "Spec card waiting", "Spec ready"),
            _card("bbbb0002", "Plan one to review", "Plan for review", kind="review"),
            _card("bbbb0003", "Plan two to review", "Plan for review", kind="review"),
            _card("cccc0004", "Building the thing", "In progress", kind="working"),
            _card("dddd0005", "PR waits", "PR open"),
            {"kind": "review", "card": None, "col": "PRs", "worker": {}, "win": "", "text": "",
             "pr": {"repo": "api", "number": 7, "title": "fix: stopped PR", "state": "decide", "url": "u"}}]


def _p(repo, number, title, yours=False):
    return {"repo": repo, "number": number, "title": title, "yours": yours}


def _prs(merged=(), opened=(), closed=(), totals=None, yours=None):
    """pr_summary's shape: per kind the total GitHub counted and up to 100 PRs; merged also counts yours."""
    t = totals or {}
    out = {k: {"total": t.get(k, len(v)), "items": list(v)} for k, v in (("merged", merged), ("opened", opened), ("closed", closed))}
    out["merged"]["yours"] = yours if yours is not None else sum(p["yours"] for p in merged)
    return out


PRS = _prs(merged=[_p("api", 12, "feat: merged one", yours=True)],
           opened=[_p("web", 13, "feat: opened one"), _p("web", 14, "feat: opened two")])


def test_counts_per_section_in_the_window():
    _write(("idea_approved", 1, "i1", {}), ("idea_approved", 30, "old", {}),       # 30 h ago: outside
           ("moved", 2, "aaaa0001", {"from": "Inbox", "to": "Spec ready"}),
           ("spec_approved", 3, "aaaa0001", {}),
           ("moved", 4, "bbbb0002", {"from": "Spec ready", "to": "Plan for review"}),
           ("moved", 4, "bbbb0003", {"from": "Spec ready", "to": "Plan for review"}),
           ("approved", 5, "cccc0004", {}), ("rejected", 6, "bbbb0003", {}), ("spec_rejected", 6, "x", {}),
           ("moved", 7, "done0001", {"from": "PR open", "to": "Done"}),
           ("error", 1, None, {"message": "board API answered Rate exceeded, try again in a while please ok"}),
           ("error", 2, None, {"message": "board API answered Rate exceeded, try again in a while please ok 2"}),
           ("error", 3, None, {"message": "agent never started"}))
    out = standup.text(_snap(_rows(), attention=1), START, PRS, now=NOW,
                       cards=[{"id": "done0001", "title": "Shipped card", "list_id": "Done"}], col=lambda c: c["list_id"])
    assert "PRs: 1 merged (1 yours), 2 opened, 0 closed without merging" in out
    assert "- api#12 feat: merged one" in out
    assert ("Pipeline: 1 ideas added, 1 specs written, 1 specs approved, 2 plans written, 1 plans approved, "
            "2 sent back, 1 cards done") in out
    assert "- Shipped card" in out
    assert ("In progress now: Spec ready 1, Plan for review 2, Approved 0, In progress 1, PR open 1; "
            "1 agents running, 1 waiting on you") in out
    assert "- Building the thing" in out
    assert "Needs you: 3" in out
    assert "- Plan one to review" in out and "- fix: stopped PR" in out
    assert "Problems: 3 errors" in out
    assert "- 2x board API answered Rate exceeded" in out
    assert "SECRET BODY" not in out


def test_bullets_are_capped_and_titles_cut():
    long = "x" * 100
    prs = _prs(merged=[_p("r", i, long) for i in range(9)])
    out = standup.text(_snap(), START, prs, now=NOW)
    bullets = [l for l in out.splitlines() if l.startswith("- r#")]
    assert len(bullets) == 5
    assert all(len(l) <= 2 + 70 for l in bullets)


def test_done_falls_back_to_the_board_when_events_are_missing():
    cards = [{"id": "d1", "title": "Done recently", "list_id": "Done", "updated_at": (NOW - timedelta(hours=2)).isoformat()},
             {"id": "d2", "title": "Done long ago", "list_id": "Done", "updated_at": (NOW - timedelta(days=3)).isoformat()}]
    out = standup.text(_snap(), START, PRS, now=NOW, cards=cards, col=lambda c: c["list_id"])
    assert "1 cards done" in out and "- Done recently" in out and "Done long ago" not in out


def test_no_activity_prints_zeros_and_no_problems_line():
    out = standup.text(_snap(), START, _prs(), now=NOW)
    assert "PRs: 0 merged (0 yours), 0 opened, 0 closed without merging" in out
    assert "Pipeline: 0 ideas added" in out
    assert "Needs you: 0" in out
    assert "Problems" not in out


def test_markdown_bolds_the_section_names():
    out = standup.text(_snap(), START, PRS, now=NOW, markdown=True)
    assert out.splitlines()[0].startswith("### Standup")
    assert "**PRs:** 1 merged" in out


@pytest.mark.parametrize("arg,want", [
    ("24h", NOW - timedelta(hours=24)), ("90m", NOW - timedelta(minutes=90)), ("7d", NOW - timedelta(days=7)),
    ("2026-09-29", datetime(2026, 9, 29).astimezone()), ("2026-09-29T08:30", datetime(2026, 9, 29, 8, 30).astimezone()),
    (None, NOW - timedelta(hours=24))])
def test_since_parsing(arg, want):
    assert standup.parse_since(arg, now=NOW) == want


@pytest.mark.parametrize("arg", ["yesterday", "24", "-3h", "2026-13-01"])
def test_bad_since_is_refused(arg):
    with pytest.raises(SystemExit, match="--since"):
        standup.parse_since(arg, now=NOW)


def _item(repo, n, author, assignees=()):
    return {"number": n, "title": f"pr {n}", "html_url": f"https://github.com/acme-org/{repo}/pull/{n}",
            "repository_url": f"https://api.github.com/repos/acme-org/{repo}", "user": {"login": author},
            "assignees": [{"login": a} for a in assignees]}


def test_team_counts_use_the_owner_scope_totals_and_mark_yours(monkeypatch):
    C.CODE_HOST = {"owner": "acme-org", "labels": {}, "repos": []}
    seen = []
    merged = [_item("api", 1, "alice"), _item("api", 2, "me"), _item("web", 3, "bob", ["me"])] + \
             [_item("web", 10 + i, "bob") for i in range(4)]

    def fake_gh(args):
        seen.append(args)
        if args[:2] == ["api", "user"]:
            return {"login": "me"}
        q = next(a for a in args if a.startswith("q="))
        if "merged:>=" in q:
            return {"total_count": 103, "items": merged}
        if "created:>=" in q:
            return {"total_count": 9, "items": [_item("api", 20, "alice")]}
        return {"total_count": 2, "items": [_item("api", 30, "me")]}
    monkeypatch.setattr(watch, "_gh", fake_gh)
    got = standup.pr_summary(START)
    searches = [a for a in seen if a[:2] != ["api", "user"]]
    assert len(searches) == 3
    assert all("user:acme-org" in next(a for a in s_ if a.startswith("q=")) and "per_page=100" in s_ for s_ in searches)
    assert not any("@me" in " ".join(a) for a in searches)            # the team's PRs, not only yours
    closed_q = next(a for a in searches[2] if a.startswith("q="))
    assert "is:unmerged" in closed_q and "closed:>=" in closed_q
    assert (got["merged"]["total"], got["merged"]["yours"], got["opened"]["total"], got["closed"]["total"]) == (103, 2, 9, 2)
    out = standup.text(_snap(), START, got, now=NOW)
    assert "PRs: 103 merged (2 yours), 9 opened, 2 closed without merging" in out
    bullets = [l for l in out.splitlines() if l.startswith("- api#") or l.startswith("- web#")]
    assert bullets[:3] == ["- api#2 pr 2", "- web#3 pr 3", "- api#30 pr 30"]   # yours first
    assert len(bullets) == 5


def test_repos_narrow_the_scope(monkeypatch):
    C.CODE_HOST = {"owner": "acme-org", "labels": {}, "repos": ["api", "other/web"]}
    seen = []

    def fake_gh(args):
        seen.append(args)
        return {"login": "me"} if args[:2] == ["api", "user"] else {"total_count": 0, "items": []}
    monkeypatch.setattr(watch, "_gh", fake_gh)
    standup.pr_summary(START)
    q = next(a for a in seen[-1] if a.startswith("q="))
    assert "repo:acme-org/api" in q and "repo:other/web" in q and "user:" not in q


def test_no_owner_says_unavailable(monkeypatch):
    C.CODE_HOST = {"owner": None, "labels": {}, "repos": []}
    monkeypatch.setattr(watch, "_gh", lambda args: pytest.fail("no search without a scope"))
    assert "owner" in standup.pr_summary(START)


@pytest.mark.parametrize("err,reason", [(OSError("pl: GitHub rate-limited, retrying at 12:30"), "rate-limited"),
                                        (subprocess.TimeoutExpired("gh", 30), "timed out"),
                                        (FileNotFoundError("gh"), "not installed")])
def test_gh_failure_says_unavailable(monkeypatch, err, reason):
    C.CODE_HOST = {"owner": "acme-org", "labels": {}, "repos": []}
    def boom(args):
        raise err
    monkeypatch.setattr(watch, "_gh", boom)
    got = standup.pr_summary(START)
    assert isinstance(got, str) and reason in got
    out = standup.text(_snap(), START, got, now=NOW)
    assert f"PRs: unavailable ({got})" in out


def test_cli_prints_the_summary(monkeypatch, capsys):
    monkeypatch.setattr(watch, "watch_snapshot", lambda: _snap(_rows()))
    monkeypatch.setattr(standup, "_cards", lambda: [])
    monkeypatch.setattr(standup, "pr_summary", lambda start: "gh is not installed")
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "standup", "--since", "2h", "--markdown"])
    cli.main()
    out = capsys.readouterr().out
    assert "### Standup" in out and "**PRs:** unavailable (gh is not installed)" in out


def _provider():
    return {"snapshot": _snap(_rows()), "metrics_by_window": {}, "pr_activity": None, "daily": {}}


async def test_s_on_the_dashboard_shows_the_standup_and_y_copies_it(monkeypatch):
    monkeypatch.setattr(standup, "pr_summary", lambda start: PRS)
    monkeypatch.setattr("shutil.which", lambda name: None)   # no real clipboard program runs
    copied = []
    app = PlApp(snapshot_provider=_provider)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("s")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert type(screen).__name__ == "StandupScreen"
        assert "feat: merged one" in screen.text
        await pilot.press("y")
        assert len(copied) == 1 and copied[0].startswith("*Standup") and "feat: merged one" in copied[0]
        await pilot.press("escape")
        await pilot.pause()
        assert type(app.screen).__name__ != "StandupScreen"


async def test_s_does_nothing_off_the_dashboard(monkeypatch):
    monkeypatch.setattr(standup, "pr_summary", lambda start: PRS)
    app = PlApp(snapshot_provider=_provider)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.press("7", "s")
        await pilot.pause()
        assert type(app.screen).__name__ != "StandupScreen"


def test_help_lists_standup(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "pl standup" in capsys.readouterr().out


async def _settled(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_dashboard_has_a_standup_panel_with_the_counts(monkeypatch):
    from pl.tui.dashboard import StandupPanel
    monkeypatch.setattr(standup, "pr_summary", lambda start: PRS)
    app = PlApp(snapshot_provider=_provider)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settled(pilot)
        panel = app.query_one(StandupPanel)
        assert "Standup (last 24 h)" in panel.border_title
        assert "1 merged (1 yours), 2 opened" in panel.text and "Needs you: 3" in panel.text
        assert panel.region.height > 0 and panel.region.bottom <= 24


async def test_standup_panel_still_shows_the_rest_when_prs_are_unavailable(monkeypatch):
    from pl.tui.dashboard import StandupPanel
    monkeypatch.setattr(standup, "pr_summary", lambda start: "gh is not installed")
    app = PlApp(snapshot_provider=_provider)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settled(pilot)
        text = app.query_one(StandupPanel).text
        assert "PRs: unavailable (gh is not installed)" in text and "Pipeline:" in text


async def test_unchanged_snapshot_does_not_rebuild_the_panel_and_prs_are_cached(monkeypatch):
    calls = {"text": 0, "prs": 0}
    real = standup.text

    def counting_text(*a, **k):
        calls["text"] += k.get("fmt") is None   # each build also makes the Slack copy
        return real(*a, **k)

    def prs(start):
        calls["prs"] += 1
        return PRS
    monkeypatch.setattr(standup, "text", counting_text)
    monkeypatch.setattr(standup, "pr_summary", prs)
    changing = {"n": 0}

    def provider():
        d = _provider()
        d["snapshot"]["at"] = f"12:00:{changing['n']:02d}"   # the clock differs on every refresh
        if changing["n"] >= 2:
            d["snapshot"]["needs"]["attention"] = 4
        changing["n"] += 1
        return d
    app = PlApp(snapshot_provider=provider)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settled(pilot)
        app.refresh_data()
        await _settled(pilot)
        assert calls == {"text": 1, "prs": 1}          # same rows and needs: no rebuild
        app.refresh_data()
        await _settled(pilot)
        assert calls == {"text": 2, "prs": 1}          # snapshot changed: rebuilt, PRs still cached


async def test_y_on_the_standup_panel_copies_it(monkeypatch):
    from pl.tui.dashboard import StandupPanel
    monkeypatch.setattr(standup, "pr_summary", lambda start: PRS)
    monkeypatch.setattr("shutil.which", lambda name: None)
    copied = []
    app = PlApp(snapshot_provider=_provider)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settled(pilot)
        panel = app.query_one(StandupPanel)
        panel.focus()
        await pilot.pause()
        await pilot.press("y")
        assert len(copied) == 1 and "feat: merged one" in copied[0]
        await pilot.press("s")
        await _settled(pilot)
        assert type(app.screen).__name__ == "StandupScreen"


SLACK_PRS = _prs(merged=[{**_p("api", 12, "fix <b> & co > x", yours=True), "url": "https://github.com/o/api/pull/12"}],
                 opened=[_p("web", 13, "no url one")])


def test_slack_format_uses_mrkdwn_not_markdown():
    _write(("moved", 2, "aaaa0001", {"from": "Inbox", "to": "Spec ready"}))
    out = standup.text(_snap(_rows()), START, SLACK_PRS, now=NOW, fmt="slack")
    lines = out.splitlines()
    assert lines[0].startswith("*Standup") and lines[0].endswith("*")
    assert "#" not in out.replace("api#12", "").replace("web#13", "")   # no headers (the # in repo#n is a PR name)
    assert "**" not in out and "```" not in out and "\n- " not in out
    assert "*PRs:* 1 merged" in out and "*Pipeline:*" in out
    assert "• Spec card waiting" in out


def test_slack_escapes_titles_and_links_prs():
    out = standup.text(_snap(_rows()), START, SLACK_PRS, now=NOW, fmt="slack")
    assert "• <https://github.com/o/api/pull/12|api#12 fix &lt;b&gt; &amp; co &gt; x>" in out
    assert "• web#13 no url one" in out               # no url: plain, still a bullet
    assert "<b>" not in out


def test_slack_escapes_the_unavailable_reason(monkeypatch):
    out = standup.text(_snap(), START, "gh said <oops> & died", now=NOW, fmt="slack")
    assert "unavailable (gh said &lt;oops&gt; &amp; died)" in out


def test_standup_slack_flag(monkeypatch, capsys):
    monkeypatch.setattr(watch, "watch_snapshot", lambda: _snap(_rows()))
    monkeypatch.setattr(standup, "_cards", lambda: [])
    monkeypatch.setattr(standup, "pr_summary", lambda start: "gh is not installed")
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "standup", "--slack"])
    cli.main()
    out = capsys.readouterr().out
    assert out.startswith("*Standup") and "*PRs:* unavailable" in out


async def test_y_copies_the_slack_version_and_says_so(monkeypatch):
    from pl.tui.dashboard import StandupPanel
    monkeypatch.setattr(standup, "pr_summary", lambda start: SLACK_PRS)
    monkeypatch.setattr("shutil.which", lambda name: None)
    copied, notes = [], []
    app = PlApp(snapshot_provider=_provider)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
    async with app.run_test(size=(80, 24)) as pilot:
        await _settled(pilot)
        app.query_one(StandupPanel).focus()
        await pilot.pause()
        await pilot.press("y")
        assert len(copied) == 1 and copied[0].startswith("*Standup") and "<https://github.com/o/api/pull/12|" in copied[0]
        assert "copied for Slack" in notes
        await pilot.press("s")
        await _settled(pilot)
        await pilot.press("y")
        assert copied[1].startswith("*Standup") and notes[-1] == "copied for Slack"


def _standup_cli(monkeypatch, *flags):
    monkeypatch.setattr(watch, "watch_snapshot", lambda: _snap(_rows()))
    monkeypatch.setattr(standup, "_cards", lambda: [])
    monkeypatch.setattr(standup, "pr_summary", lambda start: "gh is not installed")
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "standup", *flags])
    cli.main()


def test_summary_from_the_local_model_goes_above_the_standup(monkeypatch, capsys):
    from pl import local_model
    seen = []
    monkeypatch.setattr(local_model, "chat", lambda system, text, max_chars: seen.append(text) or ("Two PRs moved. All calm.", ""))
    _standup_cli(monkeypatch, "--summary")
    got = capsys.readouterr()
    assert got.out.startswith("Two PRs moved. All calm.\n\nStandup for t") and got.err == ""
    assert seen and seen[0].startswith("Standup for t")   # the model reads the standup text itself


def test_slack_summary_is_escaped(monkeypatch, capsys):
    from pl import local_model
    monkeypatch.setattr(local_model, "chat", lambda *a, **k: ("Ping <!channel> & <http://x|y>", ""))
    _standup_cli(monkeypatch, "--summary", "--slack")
    assert capsys.readouterr().out.startswith("Ping &lt;!channel&gt; &amp; &lt;http://x|y&gt;\n\n*Standup")


@pytest.mark.parametrize("reply", [OSError("down"), "not json", TimeoutError("slow")])
def test_failing_model_leaves_the_standup_unchanged_with_one_note(monkeypatch, capsys, fake_home, reply):
    from pl import local_model
    (fake_home / ".pl-t" / "config.toml").write_text("[local_model]\nenabled = true\n")

    def fake(url, payload, timeout):
        if isinstance(reply, BaseException):
            raise reply
        return reply
    monkeypatch.setattr(local_model, "_request", fake)
    _standup_cli(monkeypatch)
    plain = capsys.readouterr().out
    _standup_cli(monkeypatch, "--summary")
    got = capsys.readouterr()
    assert got.out == plain
    assert got.err.startswith("pl standup: no summary (") and got.err.count("\n") == 1


def test_summary_off_by_default_says_so_and_never_calls(monkeypatch, capsys):
    from pl import local_model
    monkeypatch.setattr(local_model, "_request", lambda *a: pytest.fail("called the model"))
    _standup_cli(monkeypatch, "--summary")
    got = capsys.readouterr()
    assert got.out.startswith("Standup for t") and "[local_model] enabled" in got.err


def test_no_summary_flag_never_calls_the_model(monkeypatch, capsys, fake_home):
    from pl import local_model
    (fake_home / ".pl-t" / "config.toml").write_text("[local_model]\nenabled = true\n")
    monkeypatch.setattr(local_model, "_request", lambda *a: pytest.fail("called the model"))
    _standup_cli(monkeypatch)
    assert capsys.readouterr().err == ""
