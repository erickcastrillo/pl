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
    h = types.SimpleNamespace(prs={}, calls=[], searches=[], found=[])

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
                           "merged_at": "2026-11-03T10:00:00Z", "merge_commit_sha": "abcdef0123456789"})

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
