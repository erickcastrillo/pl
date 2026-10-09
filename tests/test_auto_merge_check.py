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
            "head": {"repo": {"full_name": "owner/pl"}}}
    base.update(kw)
    return base


def f(name, patch="@@ -1 +1 @@\n-a = 1\n+a = 2", **kw):
    return {"filename": name, "status": "modified", "additions": 1, "deletions": 1, "patch": patch, **kw}


GOOD_FILES = [f("src/pl/watch.py"), f("tests/test_watch.py")]


def check(p=None, files=None, action="labeled"):
    return amc.check(p or pr(), GOOD_FILES if files is None else files, action, LABEL, {"owner"})


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


def test_main_prints_the_verdict_and_reasons(monkeypatch, capsys):
    calls = []

    def fake_gh(args):
        calls.append(args)
        return json.dumps(GOOD_FILES if "files" in args[-1] else pr())
    monkeypatch.setattr(amc, "_gh", fake_gh)
    monkeypatch.setenv("AUTO_MERGE_LABEL", LABEL)
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    assert amc.main(["owner/pl", "12", "labeled"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "eligible=true"
    assert calls[0] == ["api", "repos/owner/pl/pulls/12"]
    assert calls[1] == ["api", "repos/owner/pl/pulls/12/files?per_page=100"]


def test_main_says_not_eligible_with_reasons(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", lambda args: json.dumps(GOOD_FILES if "files" in args[-1] else pr(draft=True)))
    monkeypatch.setenv("AUTO_MERGE_AUTHORS", "owner")
    amc.main(["owner/pl", "12", "labeled"])
    out = capsys.readouterr().out
    assert out.startswith("eligible=false") and "draft" in out


def test_main_with_no_allowed_authors_blocks_everyone(monkeypatch, capsys):
    monkeypatch.setattr(amc, "_gh", lambda args: json.dumps(GOOD_FILES if "files" in args[-1] else pr()))
    monkeypatch.delenv("AUTO_MERGE_AUTHORS", raising=False)
    amc.main(["owner/pl", "12", "labeled"])
    assert capsys.readouterr().out.startswith("eligible=false")
