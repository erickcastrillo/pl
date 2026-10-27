"""Auto-rework: a PR the review labeled for rework sends its card back to a run agent; after rework_rounds rounds
the PR gets the failed label for a person. The run agent of such a card is told so in its prompt."""
import json
import types

import pytest

from pl import commands, dispatch, rework
from pl import config as C

URL = "https://github.com/acme/app/pull/{}"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[code_host.labels]\nreview = "pl:auto-review"\nready = "pl:ready"\n')
    C.load("t")
    rework._LAST["at"] = None
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def gh(monkeypatch):
    """GitHub, the board and the tracker faked. ns.flagged: {repo: [(number, labels)]} the rework search returns."""
    ns = types.SimpleNamespace(flagged={}, calls=[], writes=[], moves=[], events=[], sent=[], comments={}, comments_fail=False)

    def fake_gh(args):
        ns.calls.append(args)
        if args[:1] == ["api"] and len(args) == 2 and args[1].endswith("/comments?per_page=100"):
            if ns.comments_fail:
                raise SystemExit("gh failed")
            repo, num = args[1].removeprefix("repos/").split("/issues/")[0], int(args[1].split("/issues/")[1].split("/")[0])
            return json.dumps([{"body": b} for b in ns.comments.get((repo, num), [])])
        if args[:1] == ["api"] and len(args) == 2:   # the GET of labeled issues
            repo = args[1].split("/issues")[0].removeprefix("repos/")
            return json.dumps([{"number": n, "pull_request": {"url": "u"}, "labels": [{"name": x} for x in labs]}
                               for n, labs in ns.flagged.get(repo, [])])
        return "{}"
    monkeypatch.setattr(rework.github, "_gh", fake_gh)
    monkeypatch.setattr(rework, "col_name", lambda lid: lid)
    monkeypatch.setattr(rework, "update", lambda cid, **kw: ns.writes.append((cid, kw)))
    monkeypatch.setattr(commands, "move_to", lambda ref, col, cid=None: ns.moves.append((ref, col)))
    monkeypatch.setattr(rework.events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))
    monkeypatch.setattr(rework.notifications, "send", lambda event, title, message: ns.sent.append((event, title, message)))
    return ns


def _card(cid, col="PR open", prs=(1,), **meta):
    return {"id": cid, "title": f"card {cid}", "list_id": col, "tags": [],
            "metadata": {"pipeline_mode": "auto", "pr_urls": [URL.format(n) for n in prs], **meta}}


def test_a_card_whose_pr_needs_rework_goes_back_to_in_progress_as_round_1(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework", "pl:ready"])]}
    c = _card("c1")
    assert rework.sweep([c], dry=False) == ["c1"]
    assert gh.moves == [("c1", "In progress")]
    meta = gh.writes[-1][1]["metadata"]["rework"]
    assert meta["round"] == 1 and meta["active"] is True and meta["prs"] == [URL.format(1)]
    assert gh.events[0][0] == "rework"


def test_cards_without_a_flagged_pr_and_cards_in_other_columns_stay(gh):
    gh.flagged = {"acme/app": [(2, ["pl:needs-rework"])]}
    cards = [_card("c1", prs=(1,)), _card("c2", col="In progress", prs=(2,)), _card("c3", prs=(3,), pipeline_mode="manual")]
    assert rework.sweep(cards, dry=False) == []
    assert gh.moves == []


def test_one_search_per_repo_and_only_the_flagged_prs_of_a_stack(gh):
    gh.flagged = {"acme/app": [(2, ["pl:needs-rework"])]}
    rework.sweep([_card("c1", prs=(1, 2, 3)), _card("c2", prs=(4,))], dry=False)
    gets = [a for a in gh.calls if len(a) == 2 and "/comments" not in a[1]]
    assert len(gets) == 1 and "labels=pl%3Aneeds-rework" in gets[0][1]
    assert gh.writes[-1][1]["metadata"]["rework"]["prs"] == [URL.format(2)]


def test_the_round_counts_up_and_after_the_last_round_a_person_decides(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    rework.sweep([_card("c1", rework={"round": 2, "active": False})], dry=False)
    assert gh.writes[-1][1]["metadata"]["rework"]["round"] == 3
    gh.writes.clear(), gh.moves.clear(), gh.calls.clear()
    rework._LAST["at"] = None
    rework.sweep([_card("c1", rework={"round": 3, "active": False})], dry=False)
    assert gh.moves == []
    posts = [a for a in gh.calls if "POST" in a]
    assert any(a[3] == "repos/acme/app/issues/1/labels" and a[5] == "labels[]=pl:review-failed" for a in posts)
    assert any(a[3] == "repos/acme/app/issues/1/comments" for a in posts)
    assert gh.writes[-1][1]["metadata"]["rework"]["gave_up_at"]


def test_a_pr_already_labeled_failed_is_left_to_the_person(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework", "pl:review-failed"])]}
    assert rework.sweep([_card("c1")], dry=False) == []
    assert not [a for a in gh.calls if "POST" in a]


def test_a_card_back_in_pr_open_ends_its_round(gh):
    rework.sweep([_card("c1", rework={"round": 1, "active": True})], dry=False)
    assert gh.writes == [("c1", {"metadata": {"rework": {"round": 1, "active": False}}})]


def test_dry_run_writes_nothing(gh, capsys):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    rework.sweep([_card("c1", rework={"round": 1, "active": True})], dry=True)
    assert gh.writes == [] and gh.moves == [] and not [a for a in gh.calls if "POST" in a]
    assert "back to In progress (round 2)" in capsys.readouterr().out


def test_sweeps_wait_rework_every_unless_forced(gh, monkeypatch):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    monkeypatch.setattr(rework.time, "time", lambda: 1000.0)
    rework.sweep([_card("c1")], dry=True)
    n = len(gh.calls)
    monkeypatch.setattr(rework.time, "time", lambda: 1000.0 + rework.REWORK_EVERY - 1)
    rework.sweep([_card("c1")], dry=True)
    assert len(gh.calls) == n
    rework.sweep([_card("c1")], dry=True, force=True)
    assert len(gh.calls) > n


def test_rework_rounds_0_turns_it_off(gh):
    C.DISPATCH = {**C.DISPATCH, "rework_rounds": 0}
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    assert rework.sweep([_card("c1")], dry=False) == [] and gh.calls == []


def test_the_run_prompt_of_a_reworked_card_says_rework_with_the_labels(gh):
    c = _card("c1", col="In progress", rework={"round": 2, "active": True})
    assert rework.prompt_arg(c) == " rework=2 rework_label=pl:needs-rework ready=pl:ready"
    assert rework.prompt_arg(_card("c2", col="In progress")) == ""
    assert rework.prompt_arg(_card("c3", rework={"round": 1, "active": False})) == ""


def test_start_worker_adds_the_rework_argument_to_the_built_in_run_prompt(gh, monkeypatch):
    C.PROMPTS = {**C.PROMPTS, "run": C.BUILTIN_PROMPTS["run"]}
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (types.SimpleNamespace(name="claude", interactive=[]), "acme"))
    c = {**_card("c1", col="In progress", rework={"round": 1, "active": True}), "title": "Card one"}
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    dispatch.start_worker(c, "run", 1, dry=True)
    assert "rework=1 rework_label=pl:needs-rework" in out[0] and "review=pl:auto-review" in out[0]


def _merge_only_of(gh):
    return gh.writes[-1][1]["metadata"]["rework"]["merge_only"]


def test_merge_only_review_marks_the_rework_merge_only(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    gh.comments = {("acme/app", 1): ["older", "This branch is behind main and has conflicts.\n- Merge main and fix the conflicts."]}
    rework.sweep([_card("c1")], dry=False)
    assert _merge_only_of(gh) is True


def test_merge_plus_code_fix_is_a_normal_rework(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    gh.comments = {("acme/app", 1): ["Merge main.\n- test_foo fails: fix the off-by-one."]}
    rework.sweep([_card("c1")], dry=False)
    assert _merge_only_of(gh) is False


def test_a_stack_is_merge_only_only_when_every_pr_is(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"]), (2, ["pl:needs-rework"])]}
    gh.comments = {("acme/app", 1): ["Please rebase on main."], ("acme/app", 2): ["Fix the failing test."]}
    rework.sweep([_card("c1", prs=(1, 2))], dry=False)
    assert _merge_only_of(gh) is False


@pytest.mark.parametrize("fail", [False, True])
def test_no_review_comment_is_a_normal_rework(gh, fail):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    gh.comments_fail = fail
    assert rework.sweep([_card("c1")], dry=False) == ["c1"]
    assert _merge_only_of(gh) is False
    assert gh.moves == [("c1", "In progress")]


def test_a_pass_with_no_rework_reads_no_comments(gh):
    rework.sweep([_card("c1")], dry=False)
    assert not [a for a in gh.calls if "/comments" in str(a)]


@pytest.mark.parametrize("text,expected", [
    ("Please rebase on main.", True),
    ("", False),
    (None, False),
    ("## Review\n- Merge main.\n- This is out of date with main.", True),
    ("Rebase on main. Also rename foo.", False),
])
def test_merge_only_rule(text, expected):
    assert rework.merge_only(text) is expected


@pytest.mark.parametrize("merge_only,label", [(True, "waiting: merge-only rework"), (False, "waiting")])
def test_pl_list_shows_merge_only(monkeypatch, merge_only, label):
    from pl import agents
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    c = {"id": "c1", "list_id": "In progress", "metadata": {"pipeline_mode": "auto",
                                                              "rework": {"active": True, "merge_only": merge_only}}}
    assert agents.worker_view(c, {}) == label


def test_send_back_sends_one_review_rework_naming_card_and_pr(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    rework.sweep([_card("c1")], dry=False)
    assert len(gh.sent) == 1
    event, title, _ = gh.sent[0]
    assert event == "review_rework" and "c1" in title and "#1" in title


def test_give_up_sends_no_review_rework(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    rework.sweep([_card("c1", rework={"round": 3, "active": False})], dry=False)
    assert gh.sent == []


def test_dry_run_sends_no_review_rework(gh):
    gh.flagged = {"acme/app": [(1, ["pl:needs-rework"])]}
    rework.sweep([_card("c1")], dry=True, force=True)
    assert gh.sent == []


def _live_card(prs, n):
    return {"id": "aaaa0001", "metadata": {"rework": {"active": True, "round": n, "prs": prs}}}


def test_live_message_names_the_round_and_a_review_command_per_pr():
    text = rework.live_message(_live_card(["https://github.com/o/r/pull/7", "https://github.com/o/s/pull/9"], 2))
    assert "round 2" in text
    assert "gh api repos/o/r/issues/7/comments --jq '.[-1].body'" in text
    assert "gh api repos/o/s/issues/9/comments --jq '.[-1].body'" in text


def test_live_message_leaves_out_a_pr_url_with_odd_characters():
    text = rework.live_message(_live_card(["https://github.com/o/r'x/pull/7", "https://github.com/o/r y/pull/8"], 1))
    assert "r'x" not in text and "r y" not in text
    assert "latest review comment" in text
