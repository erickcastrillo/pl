"""A card whose plan has a slice without a merged pull request does not close by itself."""
import json
import types

import pytest

from pl import alerts, board, commands, pr_done, slices, trackers
from pl import config as C
from pl.trackers import github

URL = "https://github.com/o/r/pull/{}"
PLAN = "## Slices\n\n### Slice 1: first\nx\n\n### Slice 2: second\ny\n\n### Slice 3: third\nz"


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
    monkeypatch.setattr(pr_done, "notify", lambda *a, **k: None)
    yield tmp_path
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def host(monkeypatch):
    """The fake code host: pull request number -> (state, title) or an exception; every read is logged."""
    h = types.SimpleNamespace(prs={}, calls=[], searches=[], found=[], bodies={})

    def gh(args):
        if args[0] == "search":
            h.searches.append(list(args))
            return json.dumps(h.found)
        if args[0] != "api" or not args[1].startswith("repos/o/r/pulls/"):
            return "{}"
        h.calls.append(list(args))
        state, title = h.prs[int(args[1].rsplit("/", 1)[1])]
        if isinstance(state, BaseException):
            raise state
        return json.dumps({"merged": state == "merged", "state": "open" if state == "open" else "closed", "title": title,
                           "body": h.bodies.get(int(args[1].rsplit("/", 1)[1]), ""), "merged_at": "2026-11-03T10:00:00Z", "merge_commit_sha": "abcdef0123456789"})

    monkeypatch.setattr(github, "_gh", gh)
    return h


def title(k, n=3, base="Card"):
    return f"{base} (slice {k} of {n})"


def make(column="PR open", nums=(1, 2), plan=PLAN, **meta):
    desc = "# PIPELINE: SPEC\nbody\n" + (f"\n# PIPELINE: PLAN\n{plan}\n" if plan else "")
    c = trackers.get("tracker").create(column, title="Card", description=desc,
                                       metadata={"pr_urls": [URL.format(n) for n in nums], **meta})
    return board.card(c["id"])


def fresh(c):
    trackers.reset()
    return board.card(c["id"])


def column(c):
    return board.col_name(fresh(c)["list_id"])


def sweep(dry=False):
    trackers.reset()
    pr_done.sweep(board.cards(), {}, dry)


def slice_alerts():
    return [a for a in alerts.listing() if a["key"].startswith("slices_missing:")]


def merged_two(host):
    host.prs[1] = ("merged", title(1))
    host.prs[2] = ("merged", title(2))


def test_ac1_pr_open_with_unbuilt_last_slice_stays(host):
    c = make()
    merged_two(host)
    before = fresh(c)["description"]
    sweep()
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "PR open" and got["description"] == before
    assert "done_at" not in got["metadata"] and got["metadata"]["slice_hold"]["missing"] == [3]


def test_ac2_all_slices_merged_closes_with_reason(host):
    c = make(nums=(1, 2, 3))
    merged_two(host)
    host.prs[3] = ("merged", title(3))
    sweep()
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "Done" and got["metadata"]["done_reason"] == "all PRs merged"
    assert "slice_hold" not in got["metadata"]


def test_ac6_recorded_pr_without_slice_title_counts_for_nothing_and_alert_names_it(host):
    c = make(nums=(1, 2, 9))
    merged_two(host)
    host.prs[9] = ("merged", "Everything at once")
    sweep()
    (a,) = slice_alerts()
    assert column(c) == "PR open" and "slices 3" in a["title"] and "o/r#9 names no slice" in a["title"]
    assert "Everything" not in a["title"] and "--drop-slices" in a["fix"]


def test_ac8_one_alert_notified_once_then_clears_when_card_leaves(host, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_notify", lambda *a: sent.append(a))
    monkeypatch.setattr(alerts, "_offer", lambda *a: None)
    c = make()
    merged_two(host)
    now = [1000.0]
    monkeypatch.setattr(alerts, "_clock", lambda: now[0])
    sweep()
    now[0] += 5 * 3600
    sweep()
    assert len(sent) == 1 and len(slice_alerts()) == 1 and slice_alerts()[0]["acked"]
    trackers.get("tracker").update(c["id"], list_id=board.col_id("Done"))
    sweep()
    assert slice_alerts() == []


def test_ac10_held_card_costs_no_gh_call_until_pr_urls_or_plan_change(host):
    c = make()
    merged_two(host)
    sweep()
    n = len(host.calls)
    assert n == 2
    sweep()
    assert len(host.calls) == n
    host.prs[3] = ("merged", title(3))
    trackers.get("tracker").update(c["id"], metadata={"pr_urls": [URL.format(i) for i in (1, 2, 3)]})
    sweep()
    assert column(c) == "Done"


def test_ac16_pass_with_unreadable_pr_keeps_card_and_writes_no_hold(host):
    c = make()
    host.prs[1] = ("merged", title(1))
    host.prs[2] = (SystemExit("budget"), "")
    sweep()
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "PR open" and "slice_hold" not in got["metadata"]


def test_ac17_shell_text_in_title_and_heading_is_only_text(host):
    plan = "### Slice 1: $(touch /tmp/x)\nx\n\n### Slice 2: `rm -rf ~`\ny"
    c = make(nums=(1, 9), plan=plan)
    host.prs[1] = ("merged", title(1, 2))
    host.prs[9] = ("merged", "$(reboot); echo")
    sweep()
    (a,) = slice_alerts()
    assert column(c) == "PR open" and "$(" not in a["title"] and "`" not in a["title"]


def test_plan_with_no_slices_makes_the_same_calls_as_today(host):
    c = make(nums=(1,), plan="## Summary\nnothing")
    host.prs[1] = ("merged", "Card")
    sweep()
    assert column(c) == "Done" and len(host.calls) == 1 and host.searches == []


def test_planned_reads_slice_headings_and_cleans_names():
    c = make(plan="### Slice 1: a\u0007b\n\n### Slice 2:   two")
    assert slices.planned(c) == [(1, "ab"), (2, "two")]
    assert slices.planned(make(plan="no slices")) == []


# ---------- pl done ----------

def test_ac11_pl_done_refused_lists_slices_nonzero_card_not_moved(host):
    c = make()
    merged_two(host)
    for note in (None, "evidence"):
        with pytest.raises(SystemExit) as e:
            commands.done(c["id"], [c], note=note)
        assert "slice 3 (no pull request)" in str(e.value) and "--drop-slices" in str(e.value)
        assert column(c) == "PR open"


def test_ac12_drop_slices_closes_and_records_numbers_and_reason_in_metadata_and_closed(host):
    c = make()
    merged_two(host)
    commands.done(c["id"], [c], drop_slices="not needed after #600")
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "Done"
    d = got["metadata"]["dropped_slices"]
    assert d["numbers"] == [3] and d["reason"] == "not needed after #600" and d["by"] == "octo@example.com"
    assert "Dropped slices 3: not needed after #600" in got["description"]


def test_ac12_blank_reason_refused(host):
    c = make()
    with pytest.raises(SystemExit):
        commands.done(c["id"], [c], drop_slices="   ")
    assert column(c) == "PR open" and host.calls == []


def test_done_fails_closed_when_a_pr_cannot_be_read(host):
    c = make()
    host.prs[1] = ("merged", title(1))
    host.prs[2] = (SystemExit("budget"), "")
    with pytest.raises(SystemExit) as e:
        commands.done(c["id"], [c], drop_slices="why")
    assert "could not read" in str(e.value) and column(c) == "PR open"


def test_ac13_pl_drop_unchanged(host):
    c = make()
    commands.drop(c["id"])
    assert column(c) == "Done" and host.calls == []


def test_done_without_slices_makes_no_gh_call(host):
    c = make(plan="nothing")
    commands.done(c["id"], [c])
    assert column(c) == "Done" and host.calls == []


def test_tui_finish_shows_the_refusal(host):
    c = make()
    merged_two(host)
    with pytest.raises(SystemExit) as e:
        commands.done(c["id"])
    assert "--drop-slices" in str(e.value)


# ---------- slice 2: off-card merges, pl card, pl slices ----------

def run(fn, **kw):
    import argparse
    import contextlib
    import io
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            fn(argparse.Namespace(**kw))
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code


def search_hit(n, k, total=3, base="Card"):
    return {"number": n, "title": title(k, total, base), "url": URL.format(n)}


def test_ac7_merged_pr_off_the_card_is_linked_and_counts(host):
    c = make(nums=(1, 2))
    merged_two(host)
    host.found = [search_hit(3, 3)]
    host.prs[3] = ("merged", title(3))
    sweep()
    got = fresh(c)
    assert got["metadata"]["pr_urls"][-1] == URL.format(3) and board.col_name(got["list_id"]) == "Done"


def test_adopted_pr_with_manual_steps_holds_the_card_and_is_named_in_the_note(host):
    c = make(nums=(1, 2))
    merged_two(host)
    host.found = [search_hit(3, 3)]
    host.prs[3] = ("merged", title(3))
    host.bodies[3] = "Done.\n\n## Manual steps remaining\n- install the timer\n"
    sweep()
    got = fresh(c)
    assert board.col_name(got["list_id"]) != "Done"
    assert got["metadata"]["manual_steps"]["steps"] == [{"pr": "o/r#3", "text": "install the timer"}]


def test_adopted_pr_is_listed_in_the_all_merged_note(host, monkeypatch):
    make(nums=(1, 2))
    merged_two(host)
    host.found = [search_hit(3, 3)]
    host.prs[3] = ("merged", title(3))
    notes = []
    monkeypatch.setattr(pr_done, "_close", lambda c, note, dry: notes.append(note))
    sweep()
    assert len(notes) == 1 and "o/r#3" in notes[0]


def test_ac7_other_title_same_slice_number_is_not_adopted(host):
    c = make(nums=(1, 2))
    merged_two(host)
    host.found = [{"number": 3, "title": "Other card (slice 3 of 3)", "url": URL.format(3)}]
    sweep()
    got = fresh(c)
    assert got["metadata"]["pr_urls"] == [URL.format(1), URL.format(2)] and board.col_name(got["list_id"]) == "PR open"


def test_ac17_search_passes_the_title_as_one_argv_item(host):
    c = make(nums=(1, 2))
    trackers.get("tracker").update(c["id"], title="$(reboot); `x`")
    merged_two(host)
    host.prs[1] = ("merged", title(1, 3, "$(reboot); `x`"))
    sweep()
    (args,) = host.searches
    assert args[-2:] == ["--", "$(reboot); `x`"] and args.count("$(reboot); `x`") == 1


def test_ac16_failed_search_leaves_the_card_for_the_next_pass(host, monkeypatch):
    c = make(nums=(1, 2))
    merged_two(host)
    real = github._gh

    def boom(args):
        if args[0] == "search":
            raise SystemExit("budget")
        return real(args)

    monkeypatch.setattr(github, "_gh", boom)
    sweep()
    got = fresh(c)
    assert board.col_name(got["list_id"]) == "PR open" and "slice_hold" not in got["metadata"]


def test_ac9_card_prints_one_line_per_slice_for_each_state(host):
    c = make(nums=(1, 2, 4), plan=PLAN + "\n\n### Slice 4: fourth\nw\n\n### Slice 5: fifth\nv", dropped_slices={"numbers": [5]})
    host.prs[1] = ("merged", title(1))
    host.prs[2] = ("open", title(2))
    host.prs[4] = ("closed", title(4))
    out, _ = run(commands.cmd_card, id=c["id"], delete=False, json=False)
    assert "slice 1  first: merged (o/r#1)" in out and "slice 2  second: open (o/r#2)" in out
    assert "slice 3  third: no pull request" in out and "slice 4  fourth: closed unmerged (o/r#4)" in out
    assert "slice 5  fifth: dropped" in out


def test_ac9_card_json_and_unsliced_card_make_no_pr_reads(host):
    c = make(plan="nothing")
    run(commands.cmd_card, id=c["id"], delete=False, json=False)
    assert host.calls == []


def test_ac10_pl_slices_id_checks_again(host):
    c = make()
    merged_two(host)
    sweep()
    assert fresh(c)["metadata"]["slice_hold"]
    out, code = run(slices.cmd_slices, id=c["id"])
    assert "slice 3  third: no pull request" in out and code == 0
    assert not fresh(c)["metadata"]["slice_hold"]


def done_card(nums=(1, 2), plan=PLAN, **meta):
    return make(column="Done", nums=nums, plan=plan, **meta)


def test_ac14_lists_gaps_leaves_out_clean_dropped_and_dropped_slices_and_changes_nothing(host):
    gap = done_card()
    clean = done_card(nums=(1, 2, 3))
    dropped = done_card(dropped_at="2026-11-01T00:00:00Z")
    part = done_card(dropped_slices={"numbers": [3]})
    merged_two(host)
    host.prs[3] = ("merged", title(3))
    before = [fresh(x) for x in (gap, clean, dropped, part)]
    out, code = run(slices.cmd_slices, id=None)
    assert code == 0 and short(gap) in out and "slice 3  third: no pull request" in out
    assert all(short(x) not in out for x in (clean, dropped, part))
    assert [fresh(x) for x in (gap, clean, dropped, part)] == before


def short(c):
    return c["id"][:8]


def test_ac15_audit_shape(host):
    done_card(nums=(1, 2, 3), plan=PLAN + "\n\n### Slice 4: fourth\nw\n\n### Slice 5: fifth\nv")
    for n in (1, 2, 3):
        host.prs[n] = ("merged", title(n, 5))
    host.found = [search_hit(4, 4, 5)]
    out, code = run(slices.cmd_slices, id=None)
    assert "slice 4  fourth: merged, not on the card (o/r#4)" in out and "slice 5  fifth: no pull request" in out
    assert "slice 1" not in out and code == 0


def test_ac16_unreadable_pr_named_and_exit_2(host):
    c = done_card()
    host.prs[1] = ("merged", title(1))
    host.prs[2] = (SystemExit("budget"), "")
    out, code = run(slices.cmd_slices, id=None)
    assert code == 2 and "could not check" in out and short(c) in out


# ---------- In progress ----------

def in_progress(finished, nums=(1, 2)):
    return make(column="In progress", nums=nums, worker={"stage": "run", "session_id": "s", "finished_turn": finished})


def test_ac8_in_progress_alert_after_finished_turn(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("alive", None))
    c = in_progress(True)
    host.prs[1] = ("merged", title(1))
    host.prs[2] = ("open", title(2))
    sweep()
    (a,) = slice_alerts()
    assert column(c) == "In progress" and "slices 3 have" in a["title"]


def test_ac8_no_alert_while_the_agent_is_working(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("alive", None))
    in_progress(False)
    sweep()
    assert slice_alerts() == [] and host.calls == []


def test_in_progress_open_stack_with_every_slice_built_raises_no_alert(host, monkeypatch):
    monkeypatch.setattr(pr_done.agents, "worker_status", lambda w, reg: ("alive", None))
    in_progress(True, nums=(1, 2, 3))
    host.prs[1] = ("merged", title(1))
    host.prs[2] = ("open", title(2))
    host.prs[3] = ("open", title(3))
    sweep()
    assert slice_alerts() == []
