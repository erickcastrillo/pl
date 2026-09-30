"""WP22: the console and the dispatcher stay inside GitHub's rate limit.

A fake `gh` (and `tmux`) come first on PATH and log every argv; HOME and the profile live under tmp_path.
Nothing here calls the real gh, the network or tmux.
"""
import argparse
import json
import sys
import time

import pytest

from pl import config as C
from pl import board, dispatch, events, trackers, watch
from pl.trackers import github
from pl.tui import app as tui_app
from pl.tui.app import PlApp

FAKE_GH = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
state = json.load(open(os.environ["FAKE_GH_STATE"]))
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
COLS = {cols}
def out(x):
    print(json.dumps(x)); sys.exit(0)
def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default
if args[:2] == ["api", "-i"] and state.get("headers") is not None:
    print(state["headers"]); sys.exit(1)   # gh api -i prints the failing reply's headers and body, then exits 1
if args[:2] == ["api", "rate_limit"] and "search" in args[-1]:   # the REST search budget: 30 a minute
    print(state.get("search_remaining", 30)); print(state.get("search_reset", 0)); sys.exit(0)
if args[:2] == ["api", "rate_limit"]:
    if state.get("reset") is None:
        print("gh: cannot reach api.github.com", file=sys.stderr); sys.exit(1)
    print(state.get("remaining", 0)); print(state["reset"]); sys.exit(0)
if state["mode"] == "search-limit" and (args[:1] == ["search"] or "--search" in args):   # live 2026-09-29
    print(state.get("error", "GraphQL: API rate limit already exceeded for user ID 1."), file=sys.stderr); sys.exit(1)
if state["mode"] == "ratelimit":
    print(state.get("error", "GraphQL: API rate limit exceeded for user ID 1."), file=sys.stderr); sys.exit(1)
if state["mode"] == "unknown-owner" and args[:1] == ["project"]:
    print("unknown owner type", file=sys.stderr); sys.exit(1)
if args[:2] == ["project", "field-list"]:
    out({{"fields": [{{"id": "F1", "name": "Status", "type": "ProjectV2SingleSelectField",
                     "options": [{{"id": "opt-" + c, "name": c}} for c in COLS]}}], "totalCount": 1}})
if args[:2] == ["project", "item-list"]:
    items = []
    for n in range(1, state["items"] + 1):
        long = n == 1   # a Spec ready frontend card whose body is long: has_design looks it up by id
        body = ("x" * (1000 if long else 50)) + '\n<!-- pl:meta {{"pipeline_mode":"auto","profile":"main"}} -->'
        body = state.get("bodies", {{}}).get(str(n), body)
        col = state.get("status", {{}}).get(str(n)) or ("Spec ready" if long else COLS[n % 7])
        items.append({{"id": f"PVTI_{{n}}", "status": col,
                      "labels": ["frontend"] if long else [], "assignees": [],
                      "content": {{"type": "Issue", "number": n, "title": f"card {{n}}", "body": body,
                                  "url": f"https://github.com/acme/app/issues/{{n}}"}}}})
    out({{"items": items[:int(flag("--limit", "30"))], "totalCount": len(items)}})
if args[:2] == ["api", "graphql"] and "projectItems" in args[-1]:
    import re
    if state.get("lookup_error"):
        print("GraphQL: Could not resolve to an Issue with the number of 12. (repository.issue)", file=sys.stderr); sys.exit(1)
    decoys = [{{"id": "PVTI_OTHER_OWNER", "project": {{"id": "PVT_9", "number": 7, "owner": {{"login": "someone"}}}}}},
              {{"id": "PVTI_OTHER_NUMBER", "project": {{"id": "PVT_8", "number": 8, "owner": {{"login": "acme"}}}}}}]
    data = {{}}
    for alias, n in re.findall(r'(i\d+):repository\(owner:"acme",name:"app"\)\{{issue\(number:(\d+)\)', args[-1]):
        n = int(n)
        on = n not in state.get("off_project", [])
        data[alias] = {{"issue": {{"number": n, "title": f"card {{n}}", "url": f"https://github.com/acme/app/issues/{{n}}",
            "body": 'new\n<!-- pl:meta {{"pipeline_mode":"auto"}} -->', "updatedAt": "2026-09-28T11:00:00Z",
            "labels": {{"nodes": [{{"name": "backend"}}]}}, "assignees": {{"nodes": []}},
            "projectItems": {{"nodes": [{{"id": f"PVTI_{{n}}", "project": {{"id": "PVT_1", "number": 7, "owner": {{"login": "acme"}},
                "field": {{"id": "F1", "options": [{{"id": "opt-" + c, "name": c}} for c in COLS]}}}},
                "fieldValueByName": {{"name": "Inbox", "optionId": "opt-Inbox"}}}}] if on else []}}}}}}
        if state.get("decoys"):
            data[alias]["issue"]["projectItems"]["nodes"][:0] = decoys
    out({{"data": data}})
if args[:2] == ["api", "graphql"] and "updatedAt" in args[-1]:   # updatedAt stamps, one alias per issue
    import re
    stamps = state.get("stamps", {{}})
    gone = state.get("stamp_gone", [])   # a deleted issue: GitHub answers the rest and gh exits 1
    if gone:
        print(json.dumps({{"data": {{a: None if int(n) in gone else {{"issue": {{"updatedAt": stamps.get(n, "2026-09-28T10:00:00Z")}}}}
                                   for a, n in re.findall(r'(i\d+):repository\(owner:"acme",name:"app"\)\{{issue\(number:(\d+)\)', args[-1])}},
                          "errors": [{{"message": "Could not resolve to an Issue"}}]}}))
        print("gh: Could not resolve to an Issue", file=sys.stderr); sys.exit(1)
    out({{"data": {{a: {{"issue": {{"updatedAt": stamps.get(n, "2026-09-28T10:00:00Z")}}}}
                   for a, n in re.findall(r'(i\d+):repository\(owner:"acme",name:"app"\)\{{issue\(number:(\d+)\)', args[-1])}}}})
if args[:2] == ["project", "item-edit"] and flag("--field-id") != "F1":
    print("GraphQL: Could not resolve to a node with the global id of 'F-stale'.", file=sys.stderr); sys.exit(1)
if args[:2] == ["issue", "create"]:
    print(f"https://github.com/acme/app/issues/{{state.get('next', 12)}}"); sys.exit(0)
if args[:2] == ["project", "item-add"]:
    out({{"id": f"PVTI_{{state.get('next', 12)}}"}})
if args[:2] == ["project", "view"]:
    out({{"id": "PVT_1", "url": "https://github.com/orgs/acme/projects/7"}})
if args[:2] == ["issue", "list"] and state.get("intake_error"):
    print("gh: Something went wrong", file=sys.stderr); sys.exit(1)
if args[:2] == ["issue", "list"]:
    out([{{"number": n, "updatedAt": "2026-09-28T10:00:00Z"}} for n in range(1, state["items"] + 1)])
if args[:2] in (["search", "prs"], ["label", "list"]):
    out([])
if args[:2] == ["issue", "edit"]:
    sys.exit(0)
print("github.com: signed in"); sys.exit(0)
'''

CONFIG = '''tmux_session = "pl-rate-test"
[tracker]
type = "github-project"
owner = "acme"
number = 7
repo = "acme/app"
[code_host]
owner = "acme"
[code_host.labels]
review = "auto-fix"
ready = "ready-for-human-review"
merge_ready = "merge-ready"
rework = "needs-rework"
failed = "auto-review-failed"
[accounts.main]
harness = "claude"
config_dir = "~/.claude-main"
[stages.spec]
prompt = "/spec {id}"
[stages.design]
prompt = "/design {id}"
[stages.plan]
prompt = "/plan {id}"
[stages.run]
prompt = "/run {id}"
'''


class Gh:
    """Handle on the fake gh: its call log and its behaviour."""

    def __init__(self, tmp_path):
        self.log_file, self.state_file = tmp_path / "gh.log", tmp_path / "gh-state.json"
        self.set(mode="ok", items=11, remaining=0, reset=int(time.time()) + 600)

    def set(self, **kw):
        st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.state_file.write_text(json.dumps({**st, **kw}))

    def calls(self):
        return [json.loads(x) for x in self.log_file.read_text().splitlines()] if self.log_file.exists() else []

    def count(self, *prefix):
        return sum(1 for a in self.calls() if a[:len(prefix)] == list(prefix))

    def clear(self):
        self.log_file.write_text("")

    def points(self):
        """GraphQL points in this fake's accounting: item-list 1 per item asked, issue list limit/5, search 1,
        graphql 2, /rate_limit 0, any other gh call 1."""
        def cost(a):
            if a[:2] == ["project", "item-list"]:
                return int(a[a.index("--limit") + 1])
            if a[:2] == ["issue", "list"]:
                return int(a[a.index("--limit") + 1]) // 5 if "--limit" in a else 6
            if a[:1] == ["search"]:
                return 1
            if a[:2] == ["api", "rate_limit"]:
                return 0
            return 2 if a[:1] == ["api"] else 1
        return sum(cost(a) for a in self.calls())


@pytest.fixture
def gh(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable, cols=json.dumps(C.COLUMNS)))
    (bin_dir / "tmux").write_text("#!/bin/sh\nexit 1\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "config.toml").write_text(CONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh-state.json"))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "GH_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    C.load(config_dir=str(prof))
    trackers.reset()
    github._LIMIT.update(until=0, hits=0)
    getattr(github, "_LIMIT_SEARCH", {}).update(until=0, hits=0)
    monkeypatch.setattr(watch, "_pr_cache", {"at": 0, "counts": None})
    monkeypatch.setattr(board, "_SHARE", {}, raising=False)
    monkeypatch.setattr(dispatch, "_INTAKE", {"at": None})
    yield Gh(tmp_path)
    for k, v in saved.items():
        setattr(C, k, v)
    trackers.reset()
    github._LIMIT.update(until=0, hits=0)
    getattr(github, "_LIMIT_SEARCH", {}).update(until=0, hits=0)


def _pass():
    dispatch.cmd_dispatch(argparse.Namespace(once=True, dry_run=True, no_pull=False, max_runs=None, max_prep=None,
                                             interval=None))


# ---------- one board read per refresh / pass ----------

def test_one_console_refresh_reads_the_board_once(gh):
    """Measured before the fix: 9 calls, 2 item-lists (has_design re-read the whole board for one card).
    The need: field-list 1 (cold), item-list 1, issue list 1 (one repo), pr_counts 2 searches, pr_activity 2 = 7."""
    tui_app.default_provider()
    assert gh.count("project", "item-list") == 1
    assert len(gh.calls()) <= 7


def test_one_dispatcher_pass_reads_the_board_once(gh):
    """Measured before the fix: 5 calls, 2 item-lists. The need on a cold pass: field-list 1, item-list 1, one aliased
    graphql for the updatedAt stamps (WP37 replaced the per-read issue list; a warm pass skips it) = 3, plus the
    default issue intake's one open-issue search (WP36) = 4."""
    _pass()
    assert gh.count("project", "item-list") == 1
    assert gh.count("issue", "list") == 1
    assert len(gh.calls()) <= 4


def test_views_in_one_refresh_share_one_item_list_and_a_write_reads_fresh(gh):
    t = trackers.get("tracker")
    t.cards()
    t.card("acme/app#3")
    assert gh.count("project", "item-list") == 1
    t.update("acme/app#3", verify=False, title="renamed")
    t.card("acme/app#3")
    assert gh.count("project", "item-list") == 2   # the write dropped the cached list


def test_item_list_asks_for_what_the_board_holds_and_pages_when_full(gh):
    t = trackers.get("tracker")
    assert len(t.cards()) == 11
    limits = [int(a[a.index("--limit") + 1]) for a in gh.calls() if a[:2] == ["project", "item-list"]]
    assert limits and limits[0] < 500
    trackers.reset()
    gh.set(items=limits[0] + 7)                    # a board bigger than the first read
    gh.clear()
    assert len(trackers.get("tracker").cards()) == limits[0] + 7


# ---------- rate-limit backoff ----------

def test_rate_limit_stops_gh_calls_until_the_reset(gh, monkeypatch):
    reset = int(time.time()) + 600
    gh.set(mode="ratelimit", reset=reset)
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until == reset
    assert gh.count("api", "rate_limit") == 1
    gh.clear()
    for _ in range(3):
        with pytest.raises(github.RateLimited):
            trackers.reset()
            trackers.get("tracker").cards()
    assert watch.pr_activity() is None and watch.pr_counts() is None
    assert gh.calls() == []                        # no gh process at all while backing off
    gh.set(mode="ok")
    monkeypatch.setattr(github, "_clock", lambda: reset + 1)
    trackers.reset()
    assert len(trackers.get("tracker").cards()) == 11


@pytest.mark.parametrize("error", ["gh: API rate limit exceeded for user ID 1.",
                                   "You have exceeded a secondary rate limit. Please wait a few minutes.",
                                   "You have triggered an abuse detection mechanism."])
def test_with_no_reset_known_the_wait_starts_at_a_minute_and_doubles_per_hit(gh, monkeypatch, error):
    gh.set(mode="ratelimit", error=error, reset=None)   # no headers and the rate_limit call fails: back off exponentially
    now = [time.time()]
    monkeypatch.setattr(github, "_clock", lambda: now[0])
    waits = []
    for _ in range(4):
        with pytest.raises(github.RateLimited) as e:
            trackers.reset()
            trackers.get("tracker").cards()
        waits.append(e.value.until - now[0])
        now[0] = e.value.until + 1
    assert 60 <= waits[0] <= 75 and 120 <= waits[1] <= 150 and 240 <= waits[2] <= 300 and 480 <= waits[3] <= 600
    github._LIMIT["hits"] = 10
    monkeypatch.setattr(github.random, "uniform", lambda a, b: b)
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until - now[0] == pytest.approx(15 * 60 * 1.25)   # capped at 15 minutes, jitter on top


def test_the_doubling_is_shared_across_processes_and_resets_on_success(gh, monkeypatch):
    gh.set(mode="ratelimit", reset=None)
    now = [time.time()]
    monkeypatch.setattr(github, "_clock", lambda: now[0])

    def hit():
        start = now[0]
        with pytest.raises(github.RateLimited) as e:
            trackers.reset()
            trackers.get("tracker").cards()
        now[0] = e.value.until + 1
        return e.value.until - start

    assert 60 <= hit() <= 75
    github._LIMIT.update(until=0, hits=0)                # a new process keeps doubling
    assert 120 <= hit() <= 150
    gh.set(mode="ok")
    trackers.reset()
    assert len(trackers.get("tracker").cards()) == 11    # a success starts the doubling over
    gh.set(mode="ratelimit")
    assert 60 <= hit() <= 75


def test_a_call_that_failed_while_another_saved_the_back_off_asks_gh_nothing(gh):
    gh.set(mode="ratelimit")
    until = github.back_off("API rate limit exceeded").until   # a console worker hits the limit first
    gh.clear()
    assert github.back_off("API rate limit exceeded").until == until   # its racer failed before that was saved
    assert gh.calls() == []


def test_a_short_back_off_never_shortens_a_longer_one(gh):
    reset = int(time.time()) + 1800
    github._limit_file().write_text(json.dumps({"until": reset}))   # another process saw a 30-minute reset
    github._LIMIT["until"] = 0
    gh.set(mode="ratelimit", reset=None)
    assert github.back_off("API rate limit exceeded").until == reset
    assert json.loads(github._limit_file().read_text())["until"] == reset


@pytest.mark.parametrize("saved", ['{"until": 1e300}', '{"until": Infinity}', '{"until": NaN}', '{"until": "soon"}'])
def test_a_garbage_or_far_back_off_file_is_ignored(gh, saved):
    github._limit_file().write_text(saved)
    assert len(trackers.get("tracker").cards()) == 11


def test_a_retry_after_is_capped_at_an_hour(gh):
    gh.set(mode="ratelimit", headers="HTTP/2.0 403 Forbidden\nRetry-After: 999999999999\n\n{{}}")
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until <= time.time() + 3600


def test_budget_left_on_rate_limit_endpoint_does_not_mean_wait_for_its_reset(gh):
    gh.set(mode="ratelimit", remaining=4000, reset=int(time.time()) + 3000)
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until <= time.time() + 75


@pytest.mark.parametrize("headers, wait", [
    ("HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 0\nX-Ratelimit-Used: 5000\nX-Ratelimit-Reset: {reset}\n\n{{}}", 130),
    ("HTTP/2.0 403 Forbidden\nRetry-After: 90\nX-Ratelimit-Remaining: 4000\n\n{{}}", 90)])
def test_the_failing_calls_own_headers_beat_the_rate_limit_endpoint(gh, headers, wait):
    """Live 2026-09-29: the failing call said Remaining 0, reset in 2 minutes; /rate_limit said 4 used, reset in an hour."""
    now = int(time.time())
    gh.set(mode="ratelimit", remaining=4996, reset=now + 3600, headers=headers.format(reset=now + 130))
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert now + wait - 2 <= e.value.until <= time.time() + wait
    assert e.value.note.startswith(f"GitHub rate-limited, retrying at {time.strftime('%H:%M', time.localtime(e.value.until))}")


def test_the_back_off_is_shared_with_other_processes_of_the_profile(gh, monkeypatch):
    reset = int(time.time()) + 600
    gh.set(mode="ratelimit", reset=reset)
    with pytest.raises(github.RateLimited):
        trackers.get("tracker").cards()
    github._LIMIT.update(until=0, hits=0)                # a second process: pl list, the console, an agent's pl move
    trackers.reset()
    gh.clear()
    gh.set(mode="ok")
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until == reset and gh.calls() == []
    monkeypatch.setattr(github, "_clock", lambda: reset + 1)
    assert len(trackers.get("tracker").cards()) == 11
    assert not list(C.STATE_DIR.glob("gh-rate*"))       # cleared once passed


def test_console_header_shows_the_resume_time_and_keeps_the_numbers(gh):
    import asyncio

    reset = int(time.time()) + 600
    hhmm = time.strftime("%H:%M", time.localtime(reset))

    async def run():
        app = PlApp(snapshot_provider=tui_app.default_provider, interval=3600)
        async with app.run_test(size=(176, 48)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if app.data is not None:
                    break
            first = app.data
            assert first is not None
            await pilot.pause()
            await app.workers.wait_for_complete()   # the first render's standup and review workers call gh too
            gh.set(mode="ratelimit", reset=reset)
            trackers.reset()
            for _ in range(3):
                app.error = None   # wait for this refresh to land: overlapping ones all call gh before the back-off is saved
                app.refresh_data()
                for _ in range(1200):
                    await pilot.pause(0.05)
                    if app.error:
                        break
            header = str(app.query_one("#header").render())
            assert f"GitHub rate-limited, retrying at {hhmm}" in header
            assert "refresh failed" not in header
            assert app.data is first
    asyncio.run(run())
    assert gh.count("api", "rate_limit") == 1


def test_dispatcher_logs_one_event_per_backoff_window(gh, monkeypatch):
    import types

    gh.set(mode="ratelimit")
    passes = []

    def sleep(_):   # the dispatcher loop's wait between passes: stop after the third pass
        passes.append(1)
        if len(passes) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatch, "time", types.SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        dispatch.cmd_dispatch(argparse.Namespace(once=False, dry_run=True, no_pull=False, max_runs=None, max_prep=None,
                                                 interval=None))
    assert len(passes) == 3
    errors = [e for e in events._read() if e.get("kind") == "error"]
    assert len(errors) == 2 and all("rate-limited" in e["message"] for e in errors)   # WP40: one per window, searches apart
    assert gh.count("project") + gh.count("issue", "list") == 2   # pass 1: the intake search, then the board read; 2 and 3 made none


# ---------- fewer calls: the stage field, pl move, index lag ----------

def test_the_stage_field_is_read_once_across_dispatcher_passes(gh):
    _pass()
    dispatch.reload_config()                              # the loop re-reads settings, which rebuilds the tracker
    _pass()
    assert gh.count("project", "field-list") == 1
    assert gh.count("project", "item-list") == 2


def test_an_unknown_option_on_the_board_reads_the_field_again(gh):
    trackers.get("tracker").columns()
    github._KNOWN[("field", "acme", "7", "Status")]["options"].pop()   # the cached field lacks a column GitHub has
    trackers.reset("tracker")
    trackers.get("tracker").ensure_column(C.COLUMNS[-1])
    assert gh.count("project", "field-list") == 2


def _move(ref, column):
    from pl import commands
    commands.cmd_move(argparse.Namespace(id=ref, column=column))


@pytest.mark.parametrize("ref", ["acme/app#3", "#3", "3"])
def test_pl_move_sets_the_stage_with_one_lookup_and_no_board_listing(gh, ref, capsys):
    _move(ref, "Plan for review")
    assert gh.count("project", "item-list") == 0 and gh.count("project", "field-list") == 0
    (edit,) = [a for a in gh.calls() if a[:2] == ["project", "item-edit"]]
    assert edit[2:] == ["--id", "PVTI_3", "--project-id", "PVT_1", "--field-id", "F1",
                        "--single-select-option-id", "opt-Plan for review"]
    assert len(gh.calls()) == 2
    assert "acme/app#3" in capsys.readouterr().out


def test_pl_move_picks_this_projects_item_among_others(gh):
    gh.set(decoys=True)                                  # the issue is on another owner's project 7 and acme's project 8 too
    _move("3", "Plan for review")
    (edit,) = [a for a in gh.calls() if a[:2] == ["project", "item-edit"]]
    assert edit[2:6] == ["--id", "PVTI_3", "--project-id", "PVT_1"]


def test_a_stale_cached_field_is_read_again_once_when_an_edit_fails(gh):
    t = trackers.get("tracker")
    t.columns()
    github._KNOWN[("field", "acme", "7", "Status")]["id"] = "F-stale"   # the column was deleted and re-added
    t.update("acme/app#3", verify=False, column="Plan for review")
    edits = [a for a in gh.calls() if a[:2] == ["project", "item-edit"]]
    assert [a[a.index("--field-id") + 1] for a in edits] == ["F-stale", "F1"]
    assert gh.count("project", "field-list") == 2


def test_pl_move_refuses_an_issue_off_the_project_or_an_unknown_column(gh):
    gh.set(off_project=[4])
    with pytest.raises(SystemExit, match="not on project acme/7"):
        _move("acme/app#4", "Plan for review")
    with pytest.raises(SystemExit, match="Nope"):
        _move("acme/app#3", "Nope")
    assert gh.count("project", "item-edit") == 0


def test_pl_move_waits_out_the_back_off(gh):
    github._LIMIT["until"] = time.time() + 300
    with pytest.raises(github.RateLimited):
        _move("acme/app#3", "Plan for review")
    assert gh.calls() == []


def _lookups(gh):
    """Issue lookups by id (the lag aid and pl move), not the updatedAt stamp query."""
    return sum(1 for a in gh.calls() if a[:2] == ["api", "graphql"] and "projectItems" in a[-1])


def test_a_new_card_shows_while_the_project_listing_lags(gh, monkeypatch):
    gh.set(items=0)                                      # GitHub's item index has not caught up yet
    trackers.get("tracker").create("Inbox", title="card 12", metadata={"pipeline_mode": "auto"})
    trackers.reset()                                     # the dispatcher: another process, another tracker
    got = trackers.get("tracker").cards()
    assert [(c["id"], c["list_id"], c["item_id"], c["metadata"]) for c in got] == [
        ("acme/app#12", "opt-Inbox", "PVTI_12", {"pipeline_mode": "auto"})]
    gh.set(items=12)                                     # the listing caught up: no second copy, no extra lookup
    trackers.reset()
    gh.clear()
    assert [c["id"] for c in trackers.get("tracker").cards()].count("acme/app#12") == 1
    trackers.reset()
    trackers.get("tracker").cards()
    assert _lookups(gh) == 0
    gh.set(items=0)
    trackers.reset()
    assert trackers.get("tracker").cards() == []         # forgotten once listed


def test_a_lagging_card_github_cannot_find_is_dropped_and_the_listing_still_shows(gh):
    trackers.get("tracker").create("Inbox", title="card 12")   # then the issue is deleted or moved away
    gh.set(lookup_error=True)
    trackers.reset()
    assert len(trackers.get("tracker").cards()) == 11
    assert github._recent() == []
    gh.clear()
    trackers.reset()
    trackers.get("tracker").cards()
    assert _lookups(gh) == 0                             # not looked up again


def test_a_lagging_card_is_forgotten_after_15_minutes(gh, monkeypatch):
    gh.set(items=0)
    trackers.get("tracker").create("Inbox", title="card 12")
    later = time.time() + 16 * 60
    monkeypatch.setattr(github, "_clock", lambda: later)
    trackers.reset()
    assert trackers.get("tracker").cards() == []


# ---------- "unknown owner type" ----------

def test_unknown_owner_type_under_an_exhausted_limit_is_the_rate_limit(gh):
    gh.set(mode="unknown-owner", remaining=0)
    with pytest.raises(github.RateLimited):
        trackers.get("tracker").cards()


def test_unknown_owner_type_with_budget_left_keeps_the_message_and_adds_a_hint(gh):
    gh.set(mode="unknown-owner", remaining=4000)
    with pytest.raises(SystemExit) as e:
        trackers.get("tracker").cards()
    assert not isinstance(e.value, github.RateLimited)
    msg = str(e.value)
    assert "unknown owner type" in msg and "owner" in msg and "gh auth status" in msg
    assert github.limited() is None


# ---------- refresh cadence ----------

def test_console_refreshes_a_github_board_every_60_seconds_unless_told(gh):
    assert PlApp(snapshot_provider=lambda: None).interval == 60
    assert PlApp(snapshot_provider=lambda: None, interval=5).interval == 5
    C.TRACKER = {"type": "mcp"}
    assert PlApp(snapshot_provider=lambda: None).interval == 15


# ---------- WP37: the probe on "unknown owner type", no per-read issue list, one shared board read ----------

def test_unknown_owner_type_trusts_the_probe_headers_over_a_misreporting_rate_limit_endpoint(gh):
    """Live 2026-09-29 18:57-19:02Z: item-list said "unknown owner type" every pass; /rate_limit said budget left, so pl
    never backed off, while a GraphQL call's own headers said Remaining 0, reset 19:22:47Z."""
    now = int(time.time())
    gh.set(mode="unknown-owner", remaining=4996, reset=now + 3600,
           headers=f"HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 0\r\nX-Ratelimit-Used: 5000\r\nX-Ratelimit-Reset: {now + 1350}\r\n\r\n{{}}")
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert e.value.until == now + 1350
    assert json.loads(github._limit_file().read_text())["until"] == now + 1350
    assert gh.count("api", "-i") == 1                     # back_off takes the probe's answer: no second probe


def _stamp_queries(gh):
    return [a[-1] for a in gh.calls() if a[:2] == ["api", "graphql"] and "projectItems" not in a[-1]]


def test_a_board_read_lists_no_issues_and_stamps_only_cards_that_changed(gh, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(github, "_clock", lambda: now[0])
    got = trackers.get("tracker").cards()
    assert gh.count("issue", "list") == 0
    assert {c["updated_at"] for c in got} == {"2026-09-28T10:00:00+00:00"}
    assert len(_stamp_queries(gh)) == 1
    gh.clear()
    now[0] += 60                                          # past READ_TTL, same process: nothing changed
    trackers.reset("tracker")
    trackers.get("tracker").cards()
    assert _stamp_queries(gh) == []
    gh.set(status={"3": "Done"}, stamps={"3": "2026-09-28T11:00:00Z"})
    now[0] += 60
    trackers.reset("tracker")
    got = {c["id"]: c for c in trackers.get("tracker").cards()}
    (q,) = _stamp_queries(gh)
    assert "number:3)" in q and "number:4)" not in q
    assert got["acme/app#3"]["updated_at"] == "2026-09-28T11:00:00+00:00"
    assert got["acme/app#4"]["updated_at"] == "2026-09-28T10:00:00+00:00"


def test_the_console_and_pl_list_reuse_the_dispatchers_board_read(gh, monkeypatch, capsys):
    from pl import commands
    _pass()
    assert (C.STATE_DIR / "board-cache.json").exists()
    trackers.reset()                                      # another process of the profile
    gh.clear()
    tui_app.default_provider()
    commands.cmd_list(argparse.Namespace(product=False, all=False))
    assert gh.count("project", "item-list") == 0 and _stamp_queries(gh) == []
    later = time.time() + 271                             # older than the dispatcher's longest next wait (240 s) + 30
    monkeypatch.setattr(board, "_clock", lambda: later)
    monkeypatch.setattr(github, "_clock", lambda: later)
    board.cards()
    assert gh.count("project", "item-list") == 1


def test_a_write_through_pl_or_r_makes_the_next_console_read_fresh(gh):
    _pass()
    trackers.reset()
    board.share("read")
    _move("3", "Plan for review")                         # an agent's pl move
    gh.clear()
    board.cards()
    assert gh.count("project", "item-list") == 1
    _pass()
    trackers.reset()
    board.share("read")
    gh.clear()
    board.cards()
    assert gh.count("project", "item-list") == 0
    board.fresh_next()                                    # r in the console
    board.cards()
    assert gh.count("project", "item-list") == 1


def test_a_read_that_started_before_a_write_is_not_shared(gh, monkeypatch):
    t = trackers.get("tracker")
    real = t.cards

    def slow_read(query=None):                            # another process writes while this read is in flight
        got = real(query)
        board.dirty()
        return got
    monkeypatch.setattr(t, "cards", slow_read)
    board.share("write")
    board.cards()
    trackers.reset()
    board.share("read")
    gh.clear()
    board.cards()
    assert gh.count("project", "item-list") == 1


class _Proc:
    """One pl process: what github.py, trackers, watch and board remember lives per process."""
    NAMES = ((github, "_KNOWN"), (github, "_LIMITS"), (github, "_STAMPS"), (github, "_LIMIT"), (trackers, "_cache"),
             (watch, "_pr_cache"), (board, "_SHARE"))

    def __init__(self):
        self.mem = {n: {} for _, n in self.NAMES}
        self.mem.update(_LIMIT={"until": 0, "hits": 0}, _pr_cache={"at": 0, "counts": None})

    def __enter__(self):
        for m, n in self.NAMES:
            setattr(m, n, self.mem[n])

    def __exit__(self, *_):
        return False


def _hour(monkeypatch, console_every):
    """One hour: a dispatcher pass every 120 s, a console refresh every console_every s, on a fake clock."""
    import types
    for m, n in _Proc.NAMES:
        monkeypatch.setattr(m, n, getattr(m, n, {}), raising=False)
    now = [time.time()]
    monkeypatch.setattr(github, "_clock", lambda: now[0])
    monkeypatch.setattr(board, "_clock", lambda: now[0], raising=False)
    monkeypatch.setattr(watch, "time", types.SimpleNamespace(time=lambda: now[0], sleep=time.sleep))
    disp, cons, start = _Proc(), _Proc(), now[0]
    for s in range(0, 3600, console_every):
        if s % 120 == 0:
            now[0] = start + s
            with disp:
                _pass()
        now[0] = start + s + 1
        with cons:
            tui_app.default_provider()


def test_an_hour_of_dispatcher_and_console_stays_inside_a_1500_point_budget(gh, monkeypatch):
    _hour(monkeypatch, 60)
    print(f"github points/hour: {gh.points()}  item-lists {gh.count('project', 'item-list')}  "
          f"issue lists {gh.count('issue', 'list')}  searches {gh.count('search')}  graphql {gh.count('api', 'graphql')}")
    by = {}
    for a in gh.calls():
        k = " ".join(a[:2])
        by[k] = by.get(k, 0) + (int(a[a.index("--limit") + 1]) if a[:2] == ["project", "item-list"] else 1)
    print(by)
    assert gh.points() < 1500
    assert gh.count("issue", "list") <= 12      # the default issue intake searches at most every 5 minutes


@pytest.fixture
def mcp_calls(gh, monkeypatch):
    """The pipeline board is a fake MCP tracker; the list holds every tool call's action."""
    from types import SimpleNamespace as NS
    from pl.trackers import mcp as M
    calls = []
    items = {f"c{n}": {"id": f"c{n}", "title": f"card {n}", "description": "", "tags": [], "metadata": {},
                       "list_id": f"col-{C.COLUMNS[n % 7]}", "updated_at": "2026-09-28T10:00:00+00:00"} for n in range(1, 12)}

    class Sess:
        def call_tool(self, tool, args):
            calls.append(args["action"])
            a = args["action"]
            body = ([{"id": f"col-{c}", "title": c} for c in C.COLUMNS] if a == "lists" else list(items.values())
                    if a == "item_list" else items.get(args.get("item_id")) or {})
            if a == "item_update":
                body.update(args.get("fields") or {})
            return NS(isError=False, content=[NS(type="text", text=json.dumps(body))])

    class S:
        def run(self, fn):
            return fn(Sess())

        def scrub(self, t, n=None):
            return t
    monkeypatch.setattr(M, "_session", lambda server, timeout, as_is=False: ("k", S()))
    act = {"columns": ("lists", {}), "cards": ("item_list", {}), "card": ("item_get", {"item_id": "{item_id}"}),
           "update": ("item_update", {"item_id": "{item_id}", "fields": "{fields}"})}
    C.TRACKER = {"type": "mcp", "server": {"command": "x"}, "board_id": "b",
                 "tools": {k: {"tool": "boards", "args": {"action": a, **x}} for k, (a, x) in act.items()}}
    C.ISSUE_INTAKE = None   # what C.load gives an mcp tracker: the default issue intake is github-project only
    trackers.reset()
    return calls


def test_an_mcp_board_is_shared_the_same_way(mcp_calls):
    _pass()
    assert mcp_calls.count("item_list") == 1
    trackers.reset()
    tui_app.default_provider()
    assert mcp_calls.count("item_list") == 1              # the console reused the dispatcher's read
    board.update("c3", verify=False, title="renamed")     # a write through pl
    board.cards()
    assert mcp_calls.count("item_list") == 2


def test_an_hour_of_dispatcher_and_console_on_an_mcp_board(mcp_calls, monkeypatch):
    _hour(monkeypatch, 15)                                # the console refreshes a non-GitHub board every 15 s
    print(f"mcp calls/hour: {len(mcp_calls)}  item_list {mcp_calls.count('item_list')}  lists {mcp_calls.count('lists')}")
    assert mcp_calls.count("item_list") <= 31


# ---------- WP37: the dispatcher slows down while nothing changes ----------

def test_an_idle_dispatcher_doubles_its_wait_up_to_5_minutes_and_a_change_resets_it(gh, monkeypatch):
    def on_slice(n, slept):
        if n == 4 and slept == 300:
            gh.set(status={"3": "Done"})                  # someone moves a card on GitHub
    assert _loop(monkeypatch, on_slice, passes=5) == [120, 240, 300, 300, 120]


# ---------- WP37 fix round 1 ----------

def _col(cards, cid):
    return board.col_name(next(c for c in cards if c["id"] == cid)["list_id"])


def test_a_console_copy_older_than_another_process_write_is_never_saved_as_fresh(gh):
    """Review H1: the console seeded its tracker from the save, another process moved card 3 through pl, and the
    console's next read handed back the seeded copy and saved it stamped now: pl list showed the old column."""
    _pass()
    trackers.reset()
    board.share("read")
    board.cards()                                         # from the save; seeds the tracker
    gh.set(status={"3": "Approved"})
    board.dirty()                                         # an agent's pl move in another process
    assert _col(board.cards(), "acme/app#3") == "Approved"
    trackers.reset()
    board.share("read")
    assert _col(board.cards(), "acme/app#3") == "Approved"


def test_a_second_read_in_one_pass_after_another_process_write_is_fresh(gh):
    board.share("write", hold=150)
    board.cards()                                         # the intake's read
    gh.set(status={"3": "Approved"})
    board.dirty()                                         # the console approves between the pass's two reads
    board.cards()
    trackers.reset()
    board.share("read")
    assert _col(board.cards(), "acme/app#3") == "Approved"


def test_readers_reuse_a_save_for_at_most_120_seconds_however_long_the_dispatcher_waits(gh, monkeypatch):
    _pass()                                               # holds its save 270 s (next wait 240 + 30)
    trackers.reset()
    later = time.time() + 121
    monkeypatch.setattr(board, "_clock", lambda: later)
    monkeypatch.setattr(github, "_clock", lambda: later)
    board.share("read")
    gh.clear()
    board.cards()
    assert gh.count("project", "item-list") == 1


def test_approve_reads_the_card_fresh_not_from_the_save(gh):
    from pl import commands
    gh.set(status={"3": "Plan for review"})
    _pass()
    trackers.reset()
    board.share("read")
    board.cards()                                         # the console's copy says Plan for review
    gh.set(status={"3": "Done"})                          # moved on GitHub, outside pl
    with pytest.raises(SystemExit, match="Done"):
        commands.cmd_approve(argparse.Namespace(id="acme/app#3", force=False))


def test_a_spec_edit_reads_the_card_fresh_and_never_overwrites_a_newer_body(gh):
    from pl.tui import review
    body = "# PIPELINE: SPEC\nold spec\n<!-- pl:meta {\"pipeline_mode\":\"auto\"} -->"
    gh.set(bodies={"1": body})
    _pass()
    trackers.reset()
    board.share("read")
    board.cards()
    gh.set(bodies={"1": body.replace("old spec", "edited on GitHub")})
    with pytest.raises(SystemExit, match="changed on the board"):
        review.write_spec("acme/app#1", "old spec", "mine")
    assert gh.count("issue", "edit") == 0


def test_every_write_marks_the_board_after_it_lands_too():
    """A read that starts mid-write must not be shared: the write marks the board again once it is done."""
    now = [1000.0]
    mid = []

    def write():
        now[0] += 1
        mid.append(now[0])                                # another process reads the board here
        now[0] += 1
    saved = (board._clock, C.STATE_DIR)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        try:
            board._clock, C.STATE_DIR = (lambda: now[0]), Path(d)
            trackers._marked(write)()
            assert board.last_write() > mid[0]
        finally:
            board._clock, C.STATE_DIR = saved


def test_adopting_an_issue_marks_the_board(gh):
    t = trackers.get("tracker")
    assert board.last_write() is None
    t.adopt("acme/app#12", "Inbox", description="x", metadata={})
    assert board.last_write() is not None


def test_the_shared_save_is_private_to_the_user(gh):
    import stat
    _pass()
    board.dirty()
    assert stat.S_IMODE((C.STATE_DIR / "board-cache.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((C.STATE_DIR / "board-dirty.json").stat().st_mode) == 0o600


def test_two_threads_never_share_a_temp_file(gh, monkeypatch):
    import os
    import threading
    names = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (names.append(str(a)), real(a, b)))
    t = threading.Thread(target=board.dirty)
    t.start()
    t.join()
    board.dirty()
    assert len(set(names)) == 2


def test_one_deleted_issue_leaves_the_rest_of_its_batch_stamped(gh):
    gh.set(stamp_gone=[4], stamps={"3": "2026-09-28T11:00:00Z"})
    got = {c["id"]: c for c in trackers.get("tracker").cards()}
    assert got["acme/app#3"]["updated_at"] == "2026-09-28T11:00:00+00:00"
    assert got["acme/app#4"]["updated_at"] == ""


def test_a_failed_intake_search_is_tried_again_on_the_next_pass(gh):
    gh.set(intake_error=True)
    _pass()
    gh.set(intake_error=False)
    gh.clear()
    _pass()
    assert gh.count("issue", "list") == 1


def _loop(monkeypatch, on_slice, passes=3):
    """Run the dispatcher loop on a fake sleep; returns the seconds slept after each pass."""
    import types
    naps = []
    real = dispatch.dispatch_once

    def once(*a, **kw):
        if len(naps) == passes:
            raise KeyboardInterrupt
        naps.append(0)
        return real(*a, **kw)

    def sleep(s):
        naps[-1] += s
        on_slice(len(naps), naps[-1])
    monkeypatch.setattr(dispatch, "dispatch_once", once)
    monkeypatch.setattr(dispatch, "time", types.SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        dispatch.cmd_dispatch(argparse.Namespace(once=False, dry_run=True, no_pull=False, max_runs=None, max_prep=None,
                                                 interval=None))
    return naps


def test_a_write_through_pl_wakes_an_idle_dispatcher_and_resets_its_wait(gh, monkeypatch):
    def on_slice(n, slept):
        if n == 1 and slept == 15:
            board.dirty()                                 # an approve in the console
    assert _loop(monkeypatch, on_slice) == [15, 120, 240]


def test_a_new_idea_wakes_an_idle_dispatcher(gh, monkeypatch):
    (C.STATE_DIR / "ideas").mkdir(parents=True, exist_ok=True)

    def on_slice(n, slept):
        if n == 2 and slept == 30:
            (C.STATE_DIR / "ideas" / "new.md").write_text("an idea")
    assert _loop(monkeypatch, on_slice) == [120, 30, 120]


# ---------- WP37 fix round 2 ----------

def _at(monkeypatch, t):
    monkeypatch.setattr(board, "_clock", lambda: t)
    monkeypatch.setattr(github, "_clock", lambda: t)


def test_console_reject_reads_fresh(gh):
    """A send-back in the console rebuilds the whole body from what it read: a body edited outside pl survives."""
    from pl import commands
    gh.set(status={"3": "Plan for review"})
    _pass()
    trackers.reset()
    board.share("read")
    board.cards()                                         # the console's refresh seeds the tracker from the save
    gh.set(bodies={"3": "EDITED ON GITHUB\n# PIPELINE: PLAN\nplan\n<!-- pl:meta {\"pipeline_mode\":\"auto\"} -->"})
    gh.clear()
    with pytest.raises(SystemExit):                       # the fake never keeps an edit: the verify refuses
        commands.cmd_reject(argparse.Namespace(id="acme/app#3", notes="redo"))
    edits = [a for a in gh.calls() if a[:2] == ["issue", "edit"] and "--body" in a]
    assert edits and all("EDITED ON GITHUB" in a[a.index("--body") + 1] for a in edits)


def test_expired_save_reseeded_copy_is_not_saved_as_fresh(gh, monkeypatch):
    """A save made from the tracker's own copy is stamped with when that copy was read, not with now."""
    t0 = time.time()
    _at(monkeypatch, t0)
    board.share("write", hold=150)
    board.cards()                                         # the dispatcher reads the board at t0
    gh.set(status={"3": "Approved"})                      # edited on GitHub, outside pl: no dirty mark
    _at(monkeypatch, t0 + 15)
    board.cards()                                         # its copy (15 s old) answers and is saved again
    trackers.reset()
    board.share("read")
    _at(monkeypatch, t0 + 125)                            # 125 s after the board was really read
    assert _col(board.cards(), "acme/app#3") == "Approved"   # pl list must not get the t0 listing


def test_a_refresh_seeding_between_fresh_next_and_the_write_does_not_feed_it_an_old_copy(gh, monkeypatch):
    """A console refresh thread loaded the save just before a write asked for a fresh read, then seeded the new tracker."""
    gh.set(status={"3": "Plan for review"})
    _pass()
    trackers.reset()
    board.share("read")
    gh.set(status={"3": "Done"})                          # moved on GitHub, outside pl
    real = board._load

    def load(path):
        got = real(path)
        if path.name == "board-cache.json":
            board.fresh_next()                            # the write in the main thread runs here
        return got
    monkeypatch.setattr(board, "_load", load)
    board.cards()                                         # the refresh thread
    assert board.col_name(board.card("acme/app#3")["list_id"]) == "Done"


def test_a_seeded_copy_never_outlives_the_120_second_cap(gh, monkeypatch):
    t0 = time.time()
    _at(monkeypatch, t0)
    _pass()                                               # the dispatcher's save at t0
    trackers.reset()
    board.share("read")
    _at(monkeypatch, t0 + 110)
    board.cards()                                         # the console seeds its tracker from the 110 s old save
    gh.set(status={"3": "Done"})                          # moved on GitHub, outside pl
    _at(monkeypatch, t0 + 125)
    assert board.col_name(board.card("acme/app#3")["list_id"]) == "Done"   # the t0 copy is 125 s old


# ---------- WP40: a search limit pauses only the searches; the raw error is kept ----------

def test_a_search_limit_pauses_only_the_searches_and_the_board_keeps_working(gh):
    """Live 2026-09-29: `gh issue list --search` (the intake) failed with "API rate limit already exceeded" while the
    GraphQL budget had 4326 left; pl then stopped every gh call, board reads included, for hours."""
    gh.set(mode="search-limit")
    _pass()
    assert gh.count("project", "item-list") == 1          # the board was read in the same pass
    assert not github._limit_file().exists()               # board reads and moves are not stopped
    assert github.limited() is None and github.limited("search") is not None
    errors = [e for e in events._read() if e.get("kind") == "error"]
    assert len(errors) == 1 and "already exceeded" in errors[0]["message"]
    gh.clear()
    trackers.reset()
    assert len(trackers.get("tracker").cards()) == 11
    assert watch.pr_counts() is None and watch.pr_activity() is None
    _pass()
    assert gh.count("issue", "list") == 0 and gh.count("search") == 0   # the searches wait for their own reset
    assert len([e for e in events._read() if e.get("kind") == "error"]) == 1   # one event per window


def test_a_search_limit_waits_for_the_search_reset_or_a_minute(gh, monkeypatch):
    now = int(time.time())
    gh.set(mode="search-limit", search_remaining=0, search_reset=now + 45)
    assert github.back_off("API rate limit exceeded", resource="search").until == now + 45
    github._limit_file("search").unlink()
    github._LIMIT_SEARCH.update(until=0, hits=0)
    gh.set(search_remaining=30)
    assert 60 <= github.back_off("API rate limit exceeded", resource="search").until - now <= 76


def test_the_rate_limit_note_carries_gh_s_raw_error_masked_and_short(gh):
    err = "GraphQL: API rate limit already exceeded for user ID 1. token ghp_" + "a" * 36 + " " + "x" * 500
    gh.set(mode="ratelimit", error=err)
    with pytest.raises(github.RateLimited) as e:
        trackers.get("tracker").cards()
    assert "already exceeded for user ID 1" in e.value.note and "ghp_aaaa" not in e.value.note
    assert "already exceeded" in str(e.value) and len(e.value.note) < 400


def test_pr_searches_are_cached_5_minutes_and_shared_through_the_state_folder(gh, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(github, "_clock", lambda: now[0])
    watch.pr_counts()
    watch.pr_activity()
    assert gh.count("search", "prs") == 4
    gh.clear()
    monkeypatch.setattr(watch, "_pr_cache", {"at": 0, "counts": None})   # another process: the console, pl watch
    now[0] += 290
    assert watch.pr_counts() is not None and watch.pr_activity() is not None
    assert gh.count("search") == 0
    now[0] += 20
    watch.pr_counts()
    watch.pr_activity()
    assert gh.count("search", "prs") == 4
