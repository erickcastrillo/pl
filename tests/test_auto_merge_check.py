"""The auto-merge gate: only small, reviewed, low-risk pull requests by allowed authors pass."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "auto_merge_check.py"
spec = importlib.util.spec_from_file_location("auto_merge_check", SCRIPT)
amc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amc)

LABEL = "pl:merge-ready"


def pr(**kw):
    base = {"draft": False, "user": {"login": "owner"}, "labels": [{"name": LABEL}],
            "additions": 10, "deletions": 2, "changed_files": 2,
            "base": {"ref": "main", "repo": {"full_name": "owner/pl"}},
            "head": {"sha": "abc123", "repo": {"full_name": "owner/pl"}}}
    base.update(kw)
    return base


def f(name, patch="@@ -1 +1 @@\n-a = 1\n+a = 2", **kw):
    return {"filename": name, "status": "modified", "additions": 1, "deletions": 1, "patch": patch, **kw}


GOOD_FILES = [f("src/pl/watch.py"), f("tests/test_watch.py")]


COMMITTED = "2026-10-09T10:00:00Z"
LABELED = "2026-10-09T11:00:00Z"


def check(p=None, files=None, action="labeled", labeled_at=LABELED, committed_at=COMMITTED):
    return amc.check(p or pr(), GOOD_FILES if files is None else files, action, LABEL, {"owner"},
                     labeled_at=labeled_at, committed_at=committed_at)


def test_small_reviewed_change_with_tests_passes():
    assert check() == []


@pytest.mark.parametrize("p, why", [
    (pr(draft=True), "draft"),
    (pr(labels=[]), LABEL),
    (pr(user={"login": "someone"}), "author"),
    (pr(head={"repo": {"full_name": "fork/pl"}}), "fork"),
    (pr(head={"repo": None}), "fork"),
    (pr(base={"ref": "dev", "repo": {"full_name": "owner/pl"}}), "main"),
    (pr(changed_files=7), "files"),
])
def test_pull_request_level_rules_block(p, why):
    reasons = check(p)
    assert reasons and any(why in r for r in reasons)


def test_over_150_non_test_lines_blocks():
    files = [f("src/pl/watch.py", additions=140, deletions=11), f("tests/test_watch.py")]
    assert any("151 changed lines" in r for r in check(files=files))


def test_lines_under_tests_do_not_count_toward_the_limit():
    files = [f("src/pl/watch.py", additions=100, deletions=50), f("tests/test_watch.py", additions=900, deletions=5)]
    assert check(p=pr(additions=1055, deletions=55), files=files) == []


def test_new_commits_after_the_review_block():
    assert any("new commits" in r for r in check(action="synchronize"))


@pytest.mark.parametrize("name", [
    ".github/workflows/ci.yml", ".github/scripts/auto_merge_check.py", "pyproject.toml", "uv.lock",
    "src/pl/setup.py", "src/pl/assistant.py", "src/pl/assistant.md", "src/pl/update.py", "src/pl/accounts.py",
    "src/pl/skills_builtin/pl-review/SKILL.md", "AGENTS.md", "INSTALL.md", "CLAUDE.md",
    "db/migrations/0001_init.py", "schema.sql",
])
def test_sensitive_paths_block(name):
    assert any(name in r for r in check(files=[f(name), f("tests/test_x.py")]))


def test_renaming_a_sensitive_file_blocks():
    moved = f("src/pl/setup_old.py", status="renamed", previous_filename="src/pl/setup.py")
    assert any("src/pl/setup.py" in r for r in check(files=[moved, f("tests/test_x.py")]))


@pytest.mark.parametrize("line", [
    "subprocess.run(cmd, shell=True)", "eval(text)", "exec(code)", "os.system(cmd)", "import subprocess",
    "pickle.loads(blob)", "yaml.load(s)", "os.chmod(p, 0o777)", "urllib.request.urlopen(u)", "import socket",
    "token = 'abc'", "API_KEY = x", "password = y", "secret = z", "__import__('os')",
])
def test_risky_added_lines_block(line):
    files = [f("src/pl/watch.py", patch=f"@@ -1 +1,2 @@\n a = 1\n+{line}"), f("tests/test_watch.py")]
    assert any("risky" in r for r in check(files=files))


def test_risky_text_on_removed_or_context_lines_is_fine():
    files = [f("src/pl/watch.py", patch="@@ -1,2 +1 @@\n x = 1\n-eval(text)"), f("tests/test_watch.py")]
    assert check(files=files) == []


def test_file_without_a_diff_blocks():
    assert any("no text diff" in r for r in check(files=[f("src/pl/logo.png", patch=None), f("tests/test_x.py")]))


def test_file_list_shorter_than_the_pull_request_blocks():
    assert any("file list" in r for r in check(p=pr(changed_files=3)))


def test_code_change_without_a_test_change_blocks():
    assert any("tests" in r for r in check(files=[f("src/pl/watch.py")]))


def test_docs_only_change_needs_no_tests():
    assert check(p=pr(changed_files=1), files=[f("README.md")]) == []


def test_review_label_added_after_the_last_commit_passes():
    assert check(labeled_at="2026-10-09T10:00:01Z") == []


def test_review_label_added_before_the_last_commit_blocks():
    reasons = check(labeled_at="2026-10-09T09:59:59Z")
    assert reasons and any("before the last commit" in r for r in reasons)


def test_review_label_at_the_same_time_as_the_commit_blocks():
    assert any("before the last commit" in r for r in check(labeled_at=COMMITTED))


def test_missing_label_event_blocks():
    assert any("no record of when" in r for r in check(labeled_at=None))


def test_missing_commit_time_blocks():
    assert any("no record of when" in r for r in check(committed_at=None))


def events(*rows):
    return [{"event": e, "label": {"name": n}, "created_at": t} for e, n, t in rows]


def test_label_time_is_the_newest_labeled_event_for_that_label():
    evs = [events(("labeled", LABEL, "2026-10-09T08:00:00Z"), ("unlabeled", LABEL, "2026-10-09T08:30:00Z")),
           events(("labeled", "other", "2026-10-09T12:00:00Z"), ("labeled", LABEL, "2026-10-09T09:00:00Z")),
           [{"event": "assigned", "created_at": "2026-10-09T13:00:00Z"}]]
    assert amc.labeled_at(evs, LABEL) == "2026-10-09T09:00:00Z"
    assert amc.labeled_at([events(("labeled", "other", LABEL))], LABEL) is None


def fake_gh(p=None, evs=None, commit_date=COMMITTED, calls=None):
    def run(args):
        if calls is not None:
            calls.append(args)
        url = args[-1]
        if "/events" in url:
            return json.dumps([events(("labeled", LABEL, LABELED))] if evs is None else evs)
        if "/commits/" in url:
            return json.dumps({"commit": {"committer": {"date": commit_date}}})
        if "/files" in url:
            return json.dumps(GOOD_FILES)
        return json.dumps(p or pr())
    return run


def test_main_prints_the_verdict_and_reasons(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(amc, "_gh", fake_gh(calls=calls))
    monkeypatch.setenv("AUTO_MERGE_LABEL", LABEL)
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    assert amc.main(["owner/pl", "12", "labeled"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "eligible=true"
    assert calls == [["api", "repos/owner/pl/pulls/12"],
                     ["api", "repos/owner/pl/pulls/12/files?per_page=100"],
                     ["api", "--paginate", "--slurp", "repos/owner/pl/issues/12/events?per_page=100"],
                     ["api", "repos/owner/pl/commits/abc123"]]


def test_main_says_not_eligible_with_reasons(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", fake_gh(p=pr(draft=True)))
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    amc.main(["owner/pl", "12", "labeled"])
    out = capsys.readouterr().out
    assert out.startswith("eligible=false") and "draft" in out


def test_main_blocks_a_label_older_than_the_last_commit(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", fake_gh(commit_date="2026-10-09T12:00:00Z"))
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    amc.main(["owner/pl", "12", "labeled"])
    out = capsys.readouterr().out
    assert out.startswith("eligible=false") and "before the last commit" in out


def test_main_blocks_when_there_is_no_label_event(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", fake_gh(evs=[[]]))
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    amc.main(["owner/pl", "12", "labeled"])
    out = capsys.readouterr().out
    assert out.startswith("eligible=false") and "no record of when" in out


def test_main_with_no_allowed_authors_blocks_everyone(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", fake_gh())
    monkeypatch.delenv("AUTO_MERGE_AUTHORS", raising=False)
    amc.main(["owner/pl", "12", "labeled"])
    assert capsys.readouterr().out.startswith("eligible=false")
