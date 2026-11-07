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


def test_done_note_that_appears_in_the_description_is_still_written():
    c = make()
    commands.done(c["id"], [c], note="body")
    assert fresh(c)["description"].endswith("\n## Closed " + fresh(c)["metadata"]["done_at"][:10] + "\nbody\n")


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


# ---------- closing record ----------

def card_text(c, capsys):
    capsys.readouterr()
    commands.cmd_card(types.SimpleNamespace(id=c["id"], fresh=False, delete=False, json=False))
    return capsys.readouterr().out


def closed_line(out):
    return next((l for l in out.splitlines() if l.startswith("closed:")), None)


def test_card_shows_closed_line_for_merged_card(capsys):
    c = make(urls=(PR1, PR2))
    commands.done(c["id"], [c], reason="all PRs merged")
    assert closed_line(card_text(c, capsys)).startswith("closed: all PRs merged, 2 PRs, ")


def test_pl_done_records_reason_and_no_pr(capsys):
    c = make(urls=())
    commands.cmd_done(types.SimpleNamespace(id=c["id"], note=None))
    assert fresh(c)["metadata"]["done_reason"] == "pl done"
    assert closed_line(card_text(c, capsys)).startswith("closed: pl done, no PR, ")


def test_one_pr_is_singular(capsys):
    c = make()
    commands.done(c["id"], [c], reason="pl done")
    assert closed_line(card_text(c, capsys)).startswith("closed: pl done, 1 PR, ")


def test_done_card_without_record_says_no_record(capsys):
    c = make("Done")
    assert closed_line(card_text(c, capsys)) == "closed: no record"


def test_done_at_without_reason_says_no_reason_recorded(capsys):
    c = make("Done", done_at="2026-11-01T00:00:00+00:00")
    assert closed_line(card_text(c, capsys)).startswith("closed: no reason recorded, ")


def test_dropped_card_says_dropped(capsys):
    c = make("Done", dropped_at="2026-11-01T00:00:00+00:00", done_at="2026-11-01T00:00:00+00:00")
    assert closed_line(card_text(c, capsys)).startswith("closed: dropped, ")


def test_pl_move_done_records_done_at_and_reason(capsys):
    c = make()
    commands.move_card(c["id"], "Done")
    m = fresh(c)["metadata"]
    assert m["done_reason"] == "pl move" and m["done_at"] and m["done_by"] == "octo@example.com"
    assert board.col_name(fresh(c)["list_id"]) == "Done"
    assert closed_line(card_text(c, capsys)).startswith("closed: pl move, 1 PR, ")


def test_pl_move_done_on_a_done_card_keeps_its_record():
    c = make("Done", done_at="2026-11-01T00:00:00+00:00", done_reason="all PRs merged")
    commands.move_card(c["id"], "Done")
    m = fresh(c)["metadata"]
    assert m["done_reason"] == "all PRs merged" and m["done_at"] == "2026-11-01T00:00:00+00:00"


def test_closed_line_only_on_done_cards(capsys):
    c = make()
    assert closed_line(card_text(c, capsys)) is None


# ---------- manual steps: parse, hold, show, close ----------

HEAD = "## Manual steps remaining"


def declared(*steps, extra=""):
    return {**MERGED, "body": "Done.\n\n" + HEAD + "\n" + "\n".join(f"- {t}" for t in steps) + "\n" + extra}


def test_parse_steps_list_items_crlf_and_boxes():
    text = f"intro\r\n{HEAD}\r\n- [ ] install the timer\r\n* edit the file\r\n  by hand\r\n1. restart\r\n## Next\r\n- not me"
    assert pr_done.parse_steps(text) == ["install the timer", "edit the file by hand", "restart"]


def test_parse_steps_none_empty_and_absent():
    assert pr_done.parse_steps("nothing here") is None
    assert pr_done.parse_steps(f"{HEAD}\n\n## Other\n- x") == []
    assert pr_done.parse_steps(f"{HEAD}\nNone\n") == []
    assert pr_done.parse_steps(f"{HEAD}\nnone.") == []


def test_parse_steps_ignores_heading_in_code_fence_and_html_comment():
    assert pr_done.parse_steps(f"```\n{HEAD}\n- x\n```\n") is None
    assert pr_done.parse_steps(f"<!--\n{HEAD}\n- x\n-->\n") is None
    assert pr_done.parse_steps(f"{HEAD.upper()}\n- x") == ["x"]


def test_parse_steps_prose_is_one_step():
    assert pr_done.parse_steps(f"{HEAD}\nInstall the timer\nthen check it\n") == ["Install the timer then check it"]


def test_parse_steps_are_cut_and_capped():
    got = pr_done.parse_steps(HEAD + "\n" + "\n".join(f"- {'x' * 400}" if i == 0 else f"- s{i}" for i in range(25)))
    assert len(got[0]) == 300 and len(got) == 21 and got[-1] == "and 5 more, see the PR"


def test_no_section_closes_as_today(host):
    c = make()
    host.prs[1] = {**MERGED, "body": "plain description"}
    sweep(c)
    assert column(c) == "Done"


def test_description_section_holds_and_card_lists_steps_with_pr(host, capsys):
    c = make()
    host.prs[1] = declared("install the timer", "edit the file")
    sweep(c)
    assert column(c) == "PR open"
    out = card_text(c, capsys)
    assert "manual steps remaining (2):" in out and "o/r#1: install the timer" in out and "o/r#1: edit the file" in out


def test_later_passes_change_nothing_and_read_nothing(host):
    c = make()
    host.prs[1] = declared("a")
    sweep(c)
    calls, meta = list(host.calls), fresh(c)["metadata"]["manual_steps"]
    sweep(c)
    sweep(c)
    assert host.calls == calls and fresh(c)["metadata"]["manual_steps"] == meta


def test_held_card_starts_no_agent_and_does_not_block_dependant(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("dead", None))
    c = make(column="In progress", worker=DEAD)
    dep = trackers.get("tracker").create("Approved", title="Next", description="# PIPELINE: SPEC\nx\n", metadata={"depends_on": [c["id"]]})
    host.prs[1] = declared("a")
    started = []
    _pass(monkeypatch, started)
    assert column(c) == "In progress" and not [s for s in started if s[0] == c["id"]]
    assert dispatch.deps_open(board.card(dep["id"]), board.cards()) == []


def test_held_card_gets_no_pr_waiting_or_pr_person_alert(host):
    c = make(pipeline_mode="auto", rework={"round": 9, "gave_up_at": "2026-11-01T00:00:00"})
    host.prs[1] = declared("a")
    sweep(c)
    cards = board.cards()
    for x in cards:
        x["updated_at"] = "2020-01-01T00:00:00+00:00"
    dispatch.check_alerts(cards, [])
    assert not [a for a in alerts.listing() if a["key"].startswith(("pr_waiting:", "pr_person:"))]


def test_stacked_held_in_progress_all_slices_merged(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("dead", None))
    c = make(column="In progress", plan=PLAN2, urls=(PR1, PR2), worker=DEAD)
    host.prs.update({1: declared("a"), 2: MERGED})
    sweep(c)
    assert column(c) == "In progress" and fresh(c)["metadata"]["manual_steps"]["steps"] == [{"pr": "o/r#1", "text": "a"}]


def test_stacked_with_unbuilt_slices_stays_as_today(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("dead", None))
    c = make(column="In progress", plan=PLAN2, worker=DEAD)
    host.prs[1] = declared("a")
    sweep(c)
    assert column(c) == "In progress" and "manual_steps" not in fresh(c)["metadata"]


def test_pl_done_lists_steps_and_note_in_closed_section(host):
    c = make()
    host.prs[1] = declared("install the timer")
    sweep(c)
    commands.done(c["id"], [fresh(c)], note="timer installed")
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "Done" and got["metadata"]["manual_steps"] is None
    tail = got["description"].rpartition("## Closed ")[2]
    assert "Manual steps open when closed:\n- o/r#1: install the timer" in tail and tail.rstrip().endswith("timer installed")


def test_pl_done_without_note_lists_steps(host):
    c = make()
    host.prs[1] = declared("install the timer")
    sweep(c)
    commands.done(c["id"], [fresh(c)])
    assert "- o/r#1: install the timer" in fresh(c)["description"].rpartition("## Closed ")[2]


def test_moved_away_card_loses_its_hold(host):
    c = make()
    host.prs[1] = declared("a")
    sweep(c)
    commands.move_card(c["id"], "Approved")
    host.prs[1] = MERGED
    sweep(c)
    assert "manual_steps" not in (fresh(c)["metadata"] or {}) or fresh(c)["metadata"]["manual_steps"] is None
    commands.move_card(c["id"], "PR open")
    sweep(c)
    assert column(c) == "Done"


def test_step_text_never_run_masked_sections_unchanged(host, capsys, monkeypatch):
    c = make()
    secret = "ghp_" + "a" * 36
    host.prs[1] = declared("run `rm -rf /` $(touch /tmp/x)", f"token {secret}", "# PIPELINE: PLAN")
    ran = []
    monkeypatch.setattr(pr_done, "_run", lambda *a, **k: ran.append(a), raising=False)
    sweep(c)
    out = card_text(c, capsys)
    assert secret not in out and secret not in json.dumps(fresh(c)["metadata"]["manual_steps"]) and not ran
    assert board.sections(fresh(c)["description"]).keys() == board.sections(c["description"]).keys()
    commands.done(c["id"], [fresh(c)])
    assert "ghp_" + "a" * 36 not in fresh(c)["description"]


def test_held_card_has_no_prs_for_the_other_passes(host):
    from pl import rework
    c = make()
    assert len(rework._prs(c)) == 1
    host.prs[1] = declared("a")
    sweep(c)
    assert rework._prs(fresh(c)) == []
