"""Cards in PR open or In progress move to Done when all their pull requests are merged, on any tracker."""
import json
import types

import pytest

from pl import alerts, board, commands, dispatch, pr_done, trackers
from pl import config as C
from pl.trackers import github
from pl.util import short_id

PR1, PR2 = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
MERGED = {"merged": True, "state": "closed", "merged_at": "2026-11-03T10:00:00Z", "merge_commit_sha": "abcdef0123456789"}
OPEN = {"merged": False, "state": "open"}
CLOSED = {"merged": False, "state": "closed"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[tracker]\ntype = "local"\n')
    C.load("t")
    C.USER_EMAIL = "octo@example.com"
    trackers.reset()
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    yield tmp_path
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def host(monkeypatch):
    """The fake code host: url number -> pull request JSON (or an exception to raise); every call is logged."""
    h = types.SimpleNamespace(prs={}, calls=[])

    def gh(args):
        if args[0] != "api" or not args[1].startswith("repos/o/r/pulls/"):   # other dispatcher steps' reads
            return "{}"
        h.calls.append(list(args))
        got = h.prs[int(args[1].rsplit("/", 1)[1])]
        if isinstance(got, BaseException):
            raise got
        return json.dumps(got)

    monkeypatch.setattr(github, "_gh", gh)
    return h


def make(column="PR open", urls=(PR1,), plan=None, **meta):
    desc = "# PIPELINE: SPEC\nbody\n" + (f"\n# PIPELINE: PLAN\n{plan}\n" if plan else "")
    c = trackers.get("tracker").create(column, title="Card", description=desc, metadata={"pr_urls": list(urls), **meta})
    return board.card(c["id"])


def fresh(c):
    trackers.reset()
    return board.card(c["id"])


def column(c):
    return board.col_name(fresh(c)["list_id"])


# ---------- WP1: pl done --note ----------

def test_done_note_ends_a_pipeline_card_with_a_closed_section():
    c = make()
    commands.done(c["id"], [c], note="  merged by hand  ")
    got = fresh(c)
    assert got["description"].endswith("\n\n## Closed " + got["metadata"]["done_at"][:10] + "\nmerged by hand\n")
    assert board.col_name(got["list_id"]) == "Done"


def test_done_without_note_leaves_the_description_alone():
    c = make()
    commands.done(c["id"], [c])
    assert fresh(c)["description"] == c["description"]


def test_done_retry_with_the_same_note_adds_no_second_section():
    c = make()
    commands.done(c["id"], [c], note="evidence")
    commands.done(c["id"], [c], note="evidence")
    assert fresh(c)["description"].count("## Closed") == 1


def test_done_reason_is_saved_only_when_given():
    a, b = make(), make()
    commands.done(a["id"], [a], reason="all PRs merged")
    commands.done(b["id"], [b])
    assert fresh(a)["metadata"]["done_reason"] == "all PRs merged"
    assert "done_reason" not in fresh(b)["metadata"]


# ---------- WP2: the rule ----------

def sweep(c, reg=None, dry=False):
    trackers.reset()
    pr_done.sweep(board.cards(), reg or {}, dry)


def test_one_merged_pr_moves_the_card_to_done_with_evidence(host):
    c = make()
    host.prs[1] = MERGED
    sweep(c)
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "Done"
    assert "## Closed " in got["description"] and "o/r#1 merged 2026-11-03T10:00:00Z (abcdef012)" in got["description"]
    assert got["metadata"]["done_reason"] == "all PRs merged"


def test_all_merged_lists_every_pr(host):
    c = make(urls=(PR1, PR2))
    host.prs.update({1: MERGED, 2: {**MERGED, "merge_commit_sha": "1111111122222"}})
    sweep(c)
    d = fresh(c)["description"]
    assert column(c) == "Done" and "o/r#1 merged" in d and "o/r#2 merged" in d and d.count("## Closed") == 1


@pytest.mark.parametrize("other", [OPEN, CLOSED])
def test_any_open_pr_leaves_the_card_and_description_unchanged(host, other):
    c = make(urls=(PR1, PR2))
    host.prs.update({1: MERGED, 2: other})
    sweep(c)
    assert column(c) == "PR open" and fresh(c)["description"] == c["description"]


def test_linked_product_card_moves_too(host, monkeypatch):
    moved = []
    monkeypatch.setattr(commands, "intake_configured", lambda: True)
    monkeypatch.setattr(commands, "product_lists", lambda: {"Done": "pd"})
    monkeypatch.setattr(trackers, "get", lambda name="tracker", _g=trackers.get: _Intake(moved) if name == "intake" else _g(name))
    c = make(product_card="p1")
    host.prs[1] = MERGED
    sweep(c)
    assert moved == [("p1", "pd")] and column(c) == "Done"


def test_product_card_failure_is_printed_and_the_card_is_still_done(host, monkeypatch, capsys):
    monkeypatch.setattr(commands, "intake_configured", lambda: True)
    monkeypatch.setattr(commands, "product_lists", lambda: {"Done": "pd"})
    monkeypatch.setattr(trackers, "get", lambda name="tracker", _g=trackers.get: _Intake(None) if name == "intake" else _g(name))
    c = make(product_card="p1")
    host.prs[1] = MERGED
    sweep(c)
    assert column(c) == "Done" and "not moved" in capsys.readouterr().out


class _Intake:
    def __init__(self, moved):
        self.moved = moved

    def update(self, cid, verify=True, list_id=None):
        if self.moved is None:
            raise SystemExit("intake down")
        self.moved.append((cid, list_id))


def test_no_recorded_prs_or_another_column_is_not_moved_and_costs_no_gh_call(host):
    a, b = make(urls=()), make(column="Approved")
    sweep(a)
    assert host.calls == [] and column(a) == "PR open" and column(b) == "Approved"


@pytest.mark.parametrize("bad", [SystemExit("held"), SystemExit("budget"), "url"])
def test_unreadable_state_keeps_the_card_and_the_pass_finishes(host, bad):
    c = make(urls=(PR1, "https://example.com/not-a-pr") if bad == "url" else (PR1,))
    host.prs[1] = MERGED if bad == "url" else bad
    sweep(c)
    assert column(c) == "PR open" and alerts.listing() == []


def test_a_done_card_costs_no_gh_call_and_gets_no_second_section(host):
    c = make()
    host.prs[1] = MERGED
    sweep(c)
    host.calls.clear()
    sweep(c)
    sweep(c)
    assert host.calls == [] and fresh(c)["description"].count("## Closed") == 1


def test_each_pr_costs_one_gh_call_per_pass(host):
    make(urls=(PR1,))
    a = make(urls=(PR1, PR2))
    host.prs.update({1: OPEN, 2: OPEN})
    sweep(a)
    assert sorted(x[1] for x in host.calls) == ["repos/o/r/pulls/1", "repos/o/r/pulls/2"]


def test_paused_or_dry_run_moves_nothing_and_writes_nothing(host, capsys):
    c = make()
    host.prs[1] = MERGED
    sweep(c, dry=True)
    assert column(c) == "PR open" and fresh(c)["description"] == c["description"]
    assert "would move" in capsys.readouterr().out


def test_paused_dispatcher_runs_no_step(host, monkeypatch):
    c = make()
    host.prs[1] = MERGED
    monkeypatch.setattr(dispatch, "paused", lambda: {"since": "2026-11-03T00:00:00"})
    _pass(monkeypatch)
    assert column(c) == "PR open" and host.calls == []


def _pass(monkeypatch, started=None):
    for name in ("sweep_untracked", "ensure_services", "permission_wait", "trust_wait"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, *a, **k: (started if started is not None else []).append((c["id"], stage)))
    dispatch.dispatch_once(1, dry=False, pull=False)


def test_the_dispatcher_pass_moves_the_card_in_the_same_pass(host, monkeypatch):
    c = make()
    host.prs[1] = MERGED
    started = []
    _pass(monkeypatch, started)
    assert column(c) == "Done" and started == []


def test_failed_move_retries_next_pass_with_one_closed_section(host, monkeypatch):
    c = make()
    host.prs[1] = MERGED
    real, fail = board.update, {"on": True}

    def flaky(cid, **f):
        if fail["on"] and f.get("list_id"):
            raise SystemExit("write failed")
        return real(cid, **f)

    monkeypatch.setattr(commands, "update", flaky)
    sweep(c)
    assert column(c) == "PR open"
    fail["on"] = False
    sweep(c)
    sweep(c)
    assert column(c) == "Done" and fresh(c)["description"].count("## Closed") == 1


DEAD = {"stage": "run", "harness": "claude", "pane": "%9", "session_id": "s", "attempts": 1}
PLAN2 = "## Slices\n\n### Slice 1: a\nx\n\n### Slice 2: b\ny"


def test_in_progress_all_merged_agent_ended_moves_and_starts_no_agent(host, monkeypatch):
    c = make(column="In progress", worker=DEAD)
    host.prs[1] = MERGED
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("dead", None))
    started = []
    _pass(monkeypatch, started)
    assert column(c) == "Done" and not [s for s in started if s[0] == c["id"]]


def test_in_progress_agent_still_running_stays_then_moves_when_it_is_gone(host, monkeypatch):
    c = make(column="In progress", worker=DEAD)
    host.prs[1] = MERGED
    status = {"s": "alive"}
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: (status["s"], None))
    sweep(c)
    assert column(c) == "In progress" and host.calls == []
    status["s"] = "dead"
    sweep(c)
    assert column(c) == "Done"


def test_in_progress_with_fewer_prs_than_slices_stays(host):
    c = make(column="In progress", plan=PLAN2)
    host.prs[1] = MERGED
    sweep(c)
    assert column(c) == "In progress"


def test_pr_open_with_fewer_prs_than_slices_moves(host):
    c = make(plan=PLAN2)
    host.prs[1] = MERGED
    sweep(c)
    assert column(c) == "Done"


# ---------- WP3: a pull request closed unmerged goes to a person ----------

def open_alerts():
    return [a for a in alerts.listing() if a["key"].startswith("pr_closed:")]


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_closed_unmerged_pr_opens_an_alert_naming_pr_and_card(host, mode):
    c = make(pipeline_mode=mode)
    host.prs[1] = CLOSED
    sweep(c)
    (a,) = open_alerts()
    assert "o/r#1" in a["title"] and short_id(c["id"]) in a["title"] and "pl done" in a["fix"]
    assert column(c) == "PR open"


def test_the_alert_clears_when_the_card_leaves_the_column(host):
    c = make()
    host.prs[1] = CLOSED
    sweep(c)
    commands.done(c["id"], [c])
    sweep(c)
    assert open_alerts() == []


def test_merged_and_closed_mixed_stays_and_alerts(host):
    c = make(urls=(PR1, PR2))
    host.prs.update({1: MERGED, 2: CLOSED})
    sweep(c)
    assert column(c) == "PR open" and len(open_alerts()) == 1 and "o/r#2" in open_alerts()[0]["title"]


def test_any_open_pr_opens_no_alert_even_with_a_closed_one(host):
    c = make(urls=(PR1, PR2))
    host.prs.update({1: OPEN, 2: CLOSED})
    sweep(c)
    assert open_alerts() == []


def test_unreadable_state_opens_no_alert_and_keeps_an_open_one(host):
    c = make()
    host.prs[1] = CLOSED
    sweep(c)
    host.prs[1] = SystemExit("held")
    sweep(c)
    assert len(open_alerts()) == 1


def test_dry_run_opens_no_alert(host):
    c = make()
    host.prs[1] = CLOSED
    sweep(c, dry=True)
    assert open_alerts() == []
