"""pr_counts lists the open PRs assigned to you and the ones you authored. A fake gh answers; nothing touches GitHub."""
import json
import subprocess

import pytest

from pl import config as C
from pl import watch
from pl.trackers import github

LABELS = {"ready": "pl-ready", "failed": "pl-failed", "rework": "pl-rework", "merge_ready": "pl-merge"}


def pr(n, *labels):
    return {"number": n, "title": f"PR {n}", "url": f"https://github.com/o/r/pull/{n}", "repository": {"name": "r"},
            "labels": [{"name": l} for l in labels], "updatedAt": f"2026-10-25T10:0{n}:00Z"}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Answers `gh search prs` by flag: assigned-ready, assigned-failed and the label-free author search."""
    monkeypatch.setattr(C, "CODE_HOST", {"labels": LABELS}, raising=False)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state", raising=False)
    monkeypatch.setattr(watch, "_pr_cache", {"at": 0, "counts": None})
    monkeypatch.setattr(github, "_clock", lambda: 1000.0)
    monkeypatch.setattr(github, "limited", lambda resource="graphql": None)
    answers = {"ready": [], "failed": [], "author": [], "author_fail": None}
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if "--author" in argv:
            if answers["author_fail"]:
                return subprocess.CompletedProcess(argv, 1, "", answers["author_fail"])
            return subprocess.CompletedProcess(argv, 0, json.dumps(answers["author"]), "")
        which = "ready" if argv[argv.index("--label") + 1] == LABELS["ready"] else "failed"
        return subprocess.CompletedProcess(argv, 0, json.dumps(answers[which]), "")

    monkeypatch.setattr(watch.subprocess, "run", run)
    answers["calls"] = calls
    return answers


def test_authored_unassigned_pr_is_counted(fake):
    fake["author"] = [pr(1, "pl-ready")]
    counts = watch.pr_counts()
    assert counts["gate"] == 1
    assert [p["url"] for p in counts["prs"]] == ["https://github.com/o/r/pull/1"]


def test_assigned_pr_from_someone_else_is_still_counted(fake):
    fake["ready"] = [pr(2, "pl-ready")]
    counts = watch.pr_counts()
    assert counts["gate"] == 1 and counts["prs"][0]["number"] == 2


def test_pr_both_authored_and_assigned_appears_once(fake):
    fake["ready"] = [pr(3, "pl-ready")]
    fake["author"] = [pr(3, "pl-ready")]
    counts = watch.pr_counts()
    assert counts["gate"] == 1 and len(counts["prs"]) == 1


def test_authored_failed_pr_is_decide_and_unlabelled_authored_pr_is_ignored(fake):
    fake["author"] = [pr(4, "pl-failed"), pr(5, "bug")]
    counts = watch.pr_counts()
    assert counts["decide"] == 1 and counts["gate"] == 0 and len(counts["prs"]) == 1


def test_refresh_makes_at_most_three_searches(fake):
    fake["author"] = [pr(1, "pl-ready")]
    watch.pr_counts()
    assert sum(1 for a in fake["calls"] if a[:3] == ["gh", "search", "prs"]) == 3


def test_failed_author_search_falls_back_to_the_assigned_list(fake):
    fake["ready"] = [pr(2, "pl-ready")]
    fake["author_fail"] = "boom"
    counts = watch.pr_counts()
    assert counts["gate"] == 1


def test_rate_limited_author_search_keeps_the_cached_list(fake, monkeypatch):
    cached = {"decide": 0, "merge": 0, "rework": 0, "gate": 7, "prs": []}
    monkeypatch.setattr(watch, "_pr_cache", {"at": 0, "counts": cached})
    fake["author_fail"] = "API rate limit exceeded"
    monkeypatch.setattr(github, "back_off", lambda *a, **k: True)
    assert watch.pr_counts() is cached


def test_failed_pr_both_authored_and_assigned_appears_once(fake):
    fake["failed"] = [pr(6, "pl-failed")]
    fake["author"] = [pr(6, "pl-failed")]
    assert watch.pr_counts()["decide"] == 1
