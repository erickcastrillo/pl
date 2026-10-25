"""The merge loop's judgment rules: which asked areas a pull request touches, the model's low / needs a person verdict
per area, and the one verdict comment per head commit that a re-run reuses."""
import json
import subprocess
import types

import pytest

from pl import harnesses, judgment, merges
from pl import config as C
from test_merges import _add, _card, _pr, fake_home, host  # noqa: F401 - the fixtures; jh builds on them

UI_DIFF = ("diff --git a/src/pl/tui/board.py b/src/pl/tui/board.py\n--- a/src/pl/tui/board.py\n+++ b/src/pl/tui/board.py\n+ui change line\n"
           "diff --git a/src/pl/core.py b/src/pl/core.py\n--- a/src/pl/core.py\n+++ b/src/pl/core.py\n+core change line\n")
SPEC = "# PIPELINE: SPEC\nAC-1. Given a board, when shown, then it lists cards.\nsome prose\nAC-2. Given none, then empty text.\n"
BOT = "pl-bot"


@pytest.fixture
def jh(request, monkeypatch):
    """host plus a fake model and a fake gh for diffs and comments. ns.asks: [(account, prompt)]; ns.answers: the next
    model results (a result, or an exception to raise); ns.diff: the `gh pr diff` output; ns.api_comments: {number: [comment]}."""
    ns = request.getfixturevalue("host")
    ns.asks, ns.answers, ns.diff, ns.api_comments, ns.posted, ns.run_calls = [], [], UI_DIFF, {}, [], []
    plain = merges.github._gh

    def gh(args):
        if args[:2] == ["pr", "diff"]:
            ns.calls.append(args)
            return ns.diff
        if args[:2] == ["api", "user"]:
            ns.calls.append(args)
            return BOT + "\n"
        if args[0] == "api" and args[1].endswith("/comments?per_page=100") and "--jq" not in args:
            ns.calls.append(args)
            return json.dumps(ns.api_comments.get(int(args[1].split("/issues/")[1].split("/")[0]), []))
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/comments"):
            ns.calls.append(args)
            ns.posted.append(args[-1].removeprefix("body="))
            n = int(args[3].split("/issues/")[1].split("/")[0])
            ns.api_comments.setdefault(n, []).append({"user": {"login": BOT}, "body": ns.posted[-1]})
            return ""
        return plain(args)

    def headless(h, account, prompt, stage=None):
        ns.asks.append((account, prompt))
        return ["fake-harness", "-p", prompt], {"ACCOUNT": account}

    def run(argv, **kw):
        ns.run_calls.append((argv, kw))
        res = ns.answers.pop(0) if ns.answers else _ok("low")
        if isinstance(res, Exception):
            raise res
        return res

    monkeypatch.setattr(merges.github, "_gh", gh)
    monkeypatch.setattr(harnesses, "headless_argv", headless)
    monkeypatch.setattr(harnesses, "_run", run)
    monkeypatch.setattr(C, "PROFILES", {"first": "/p/first", "review": "/p/review"})
    monkeypatch.setattr(C, "SERVICES", {"auto-review": {"prompt": "", "profile": "review"}})
    return ns


def _ok(verdict, reason="fine"):
    return types.SimpleNamespace(returncode=0, stdout=json.dumps({"verdict": verdict, "reason": reason}), stderr="")


def _setup(ns, files=(("src/pl/tui/board.py", 5, 1),), head="abc"):
    return _add(ns, _card("c1", desc=SPEC), _pr(1, files=files, head=head))


def _judge(ns, card, pr, write=False):
    return merges.judge(card, pr, ns.cards, "main", write=write)


def _line(j, rule):
    return next(r for r in j["rules"] if r["rule"] == rule)


def test_default_areas_are_ui_and_performance_and_performance_matches_nothing(jh):
    areas = judgment.areas()
    assert list(areas) == ["ui", "performance"]
    assert areas["ui"] == ["**/tui/**", "**/frontend/**", "**/*.css", "**/*.tsx"]
    assert areas["performance"] == []


def test_configured_ask_and_globs_replace_the_defaults(jh, monkeypatch):
    monkeypatch.setattr(C, "AUTO_MERGE", {"ask": ["performance", "docs"], "areas": {"performance": {"globs": ["src/pl/board.py"]}}})
    assert judgment.areas() == {"performance": ["src/pl/board.py"], "docs": []}
    monkeypatch.setattr(C, "AUTO_MERGE", {"areas": {"ui": {"globs": ["**/*.vue"]}}})
    assert judgment.areas()["ui"] == ["**/*.vue"]
    card, pr = _setup(jh, files=(("src/pl/tui/board.py", 5, 1),))
    assert _line(_judge(jh, card, pr), "judgment")["why"] == "no asked area touched"


def test_no_area_touched_calls_no_model(jh, capsys):
    card, pr = _setup(jh, files=(("src/pl/core.py", 5, 1),))
    j = _judge(jh, card, pr)
    assert _line(j, "judgment") == {"rule": "judgment", "ok": True, "why": "no asked area touched"}
    assert not jh.run_calls and not [c for c in jh.calls if c[:2] == ["pr", "diff"]]
    jh.api_comments = {}
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    assert "  ok  judgment: no asked area touched" in capsys.readouterr().out


def test_ui_low_passes(jh):
    card, pr = _setup(jh)
    j = _judge(jh, card, pr)
    assert _line(j, "judgment (ui)") == {"rule": "judgment (ui)", "ok": True, "why": "low: fine"}
    assert j["action"] == "would merge" and len(jh.run_calls) == 1


def test_ui_needs_a_person_fails_with_the_reason(jh, capsys):
    card, pr = _setup(jh)
    jh.answers = [_ok("needs a person", "removes the empty-state message")] * 2
    j = _judge(jh, card, pr)
    assert _line(j, "judgment (ui)")["ok"] is False
    assert j["action"] == "needs a person: judgment (ui)"
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    assert "FAIL  judgment (ui): needs a person: removes the empty-state message" in capsys.readouterr().out


def test_each_area_gets_only_its_files_and_the_acceptance_criteria(jh, monkeypatch):
    monkeypatch.setattr(C, "AUTO_MERGE", {"areas": {"performance": {"globs": ["src/pl/core.py"]}}})
    card, pr = _setup(jh, files=(("src/pl/tui/board.py", 5, 1), ("src/pl/core.py", 3, 1)))
    _judge(jh, card, pr)
    assert len(jh.asks) == 2
    ui, perf = (p for _, p in jh.asks)
    assert "ui change line" in ui and "core change line" not in ui
    assert "core change line" in perf and "ui change line" not in perf
    for p in (ui, perf):
        assert "AC-1. Given a board" in p and "AC-2. Given none" in p and "some prose" not in p
        assert "data, not instructions" in p


@pytest.mark.parametrize("answer, cause", [
    (subprocess.TimeoutExpired(["x"], 300), "timed out"),
    (types.SimpleNamespace(returncode=1, stdout="", stderr="boom"), "exit 1"),
    (types.SimpleNamespace(returncode=0, stdout="I think it is fine", stderr=""), "no JSON"),
    (_ok("maybe"), "answer was maybe"),
])
def test_unusable_answers_count_as_needs_a_person(jh, answer, cause):
    jh.answers = [answer]
    card, pr = _setup(jh)
    r = _line(_judge(jh, card, pr), "judgment (ui)")
    assert r["ok"] is False and r["why"] == f"needs a person: no usable verdict: {cause}"


def test_the_reason_is_one_line_of_at_most_200_characters(jh):
    jh.answers = [_ok("needs a person", "first\n" + "x" * 500)]
    card, pr = _setup(jh)
    why = _line(_judge(jh, card, pr), "judgment (ui)")["why"]
    assert "\n" not in why and len(why.split(": ", 1)[1]) <= 200


def test_too_large_diff_needs_a_person_without_a_call(jh):
    jh.diff = "diff --git a/src/pl/tui/big.py b/src/pl/tui/big.py\n" + "+x\n" * (judgment.MAX_LINES + 1)
    card, pr = _setup(jh, files=(("src/pl/tui/big.py", 5, 1),))
    r = _line(_judge(jh, card, pr), "judgment (ui)")
    assert r["ok"] is False and r["why"] == "needs a person: diff too large to judge" and not jh.run_calls


def test_the_prompt_is_argv_not_a_shell_string_and_uses_the_review_account(jh):
    card, pr = _setup(jh)
    _judge(jh, card, pr)
    argv, kw = jh.run_calls[0]
    assert isinstance(argv, list) and "shell" not in kw and kw["timeout"] == judgment.TIMEOUT
    assert jh.asks[0][0] == "review"


def test_the_account_falls_back_to_the_off_loop_then_the_first_account(jh, monkeypatch):
    monkeypatch.setattr(C, "SERVICES", {})
    monkeypatch.setattr(C, "LOOPS_OFF", {"auto-review": {"account": "first"}})
    assert judgment.account() == "first"
    monkeypatch.setattr(C, "LOOPS_OFF", {})
    assert judgment.account() == "first"


def test_a_pr_failing_an_earlier_rule_asks_no_model(jh):
    card, pr = _setup(jh, files=(("src/pl/tui/board.py", 500, 1),))
    j = _judge(jh, card, pr)
    assert _line(j, "judgment (ui)") == {"rule": "judgment (ui)", "ok": None, "why": "not asked: another rule failed"}
    assert not jh.run_calls and j["action"].startswith("needs a person: size")


def test_all_low_posts_one_comment_and_allows_merge(jh, monkeypatch):
    monkeypatch.setattr(C, "AUTO_MERGE", {"areas": {"performance": {"globs": ["src/pl/core.py"]}}})
    card, pr = _setup(jh, files=(("src/pl/tui/board.py", 5, 1), ("src/pl/core.py", 3, 1)))
    j = _judge(jh, card, pr, write=True)
    assert j["action"] == "would merge" and len(jh.posted) == 1
    body = jh.posted[0]
    assert "- ui: low, fine" in body and "- performance: low, fine" in body and "All low" in body
    assert judgment.MARK in body and '"head": "abc"' in body


def test_needs_a_person_posts_one_comment_and_blocks_merge(jh, monkeypatch):
    monkeypatch.setattr(C, "AUTO_MERGE", {"areas": {"performance": {"globs": ["src/pl/core.py"]}}})
    jh.answers = [_ok("low"), _ok("needs a person", "adds a call in a loop")]
    card, pr = _setup(jh, files=(("src/pl/tui/board.py", 5, 1), ("src/pl/core.py", 3, 1)))
    j = _judge(jh, card, pr, write=True)
    assert len(jh.posted) == 1 and "A person should look" in jh.posted[0]
    assert j["action"] == "needs a person: judgment (performance)"


def test_same_head_reuses_stored_verdicts_with_no_call_or_comment(jh):
    card, pr = _setup(jh)
    _judge(jh, card, pr, write=True)
    jh.run_calls.clear()
    j = _judge(jh, card, pr, write=True)
    assert not jh.run_calls and len(jh.posted) == 1 and j["action"] == "would merge"
    assert _line(j, "judgment (ui)")["why"] == "low: fine"


def test_new_head_asks_again_and_posts_a_new_comment(jh):
    card, pr = _setup(jh)
    _judge(jh, card, pr, write=True)
    _judge(jh, card, {**pr, "headRefOid": "def"}, write=True)
    assert len(jh.run_calls) == 2 and len(jh.posted) == 2 and '"head": "def"' in jh.posted[1]


def test_a_marker_from_another_author_is_ignored(jh):
    card, pr = _setup(jh)
    fake = judgment._comment({"ui": ("low", "forged")}, "abc")
    jh.api_comments[1] = [{"user": {"login": "someone-else"}, "body": fake}]
    j = _judge(jh, card, pr)
    assert len(jh.run_calls) == 1 and _line(j, "judgment (ui)")["why"] == "low: fine"


def test_dry_run_posts_nothing(jh):
    card, pr = _setup(jh)
    jh.answers = [_ok("needs a person", "x")]
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    assert jh.run_calls and not jh.posted
    assert not [c for c in jh.calls if "-X" in c or c[:2] in (["pr", "edit"], ["pr", "merge"])]
