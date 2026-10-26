"""pl tests affected: a fake repository in tmp_path and a fake git seam; no real git, HOME, network or harness."""
import json
import sys

import pytest

from pl import cli, tests_affected as T

FILES = {
    "src/app/__init__.py": "",
    "src/app/core.py": "X = 1\n",
    "src/app/mid.py": "from app import core\nx = core.X\n",
    "src/app/other.py": "Y = 2\n",
    "src/app/rel.py": "from . import core\n",
    "src/app/boom.py": "raise SystemExit(3)\n",
    "src/app/broken.py": "def (:\n",
    "tests/conftest.py": "",
    "tests/helpers.py": "",
    "tests/test_mid.py": "from app.mid import x\n",
    "tests/test_direct.py": "import app.core\n",
    "tests/test_other.py": "from app import other\n",
    "tests/test_core.py": "def test_nothing():\n    pass\n",
    "tests/test_rel.py": "from app import rel\n",
    "tests/test_skill.py": "open('skills/pl-run/SKILL.md')\n",
}


@pytest.fixture
def repo(tmp_path):
    for rel, text in FILES.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path


def fake_git(monkeypatch, root, changed=(), untracked=(), fail=False, calls=None):
    def run(argv, cwd=None):
        if calls is not None:
            calls.append(argv)
        if fail:
            return 128, "", "fatal: not a git repository"
        if "rev-parse" in argv:
            return 0, f"{root}\n", ""
        if "symbolic-ref" in argv:
            return 1, "", "not set"
        if "merge-base" in argv:
            return 0, "mergesha\n", ""
        if "diff" in argv:
            return 0, "".join(f"{p}\n" for p in changed), ""
        if "ls-files" in argv:
            return 0, "".join(f"{p}\n" for p in untracked), ""
        return 1, "", "unexpected"
    monkeypatch.setattr(T, "_run", run)


def result(monkeypatch, repo, changed, **kw):
    fake_git(monkeypatch, repo, changed, **kw)
    return T.affected(repo, "abc")


def test_a_changed_module_lists_direct_and_indirect_importers(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert r["reasons"]["tests/test_direct.py"] == "imports app.core"
    assert r["reasons"]["tests/test_mid.py"] == "imports app.core (via app.mid)"
    assert "tests/test_other.py" not in r["tests"]
    assert r["full"] is False


def test_a_name_match_is_listed_even_without_an_import(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert r["reasons"]["tests/test_core.py"] == "name match core.py"


def test_a_changed_test_is_listed(repo, monkeypatch):
    r = result(monkeypatch, repo, ["tests/test_other.py"])
    assert r["tests"] == ["tests/test_other.py"]
    assert r["reasons"]["tests/test_other.py"] == "changed test"


def test_relative_imports_are_followed(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert "tests/test_rel.py" in r["tests"]


def test_imports_are_parsed_not_executed(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/boom.py", "src/app/broken.py"])
    assert r["tests"] == [] and r["full"] is False
    assert "app.boom" not in sys.modules


@pytest.mark.parametrize("path", ["tests/conftest.py", "pyproject.toml", "uv.lock", "tests/helpers.py"])
def test_conftest_pyproject_lock_or_helper_forces_full(repo, monkeypatch, path):
    assert result(monkeypatch, repo, [path])["full"] is True


def test_a_changed_non_python_file_lists_tests_that_mention_it(repo, monkeypatch):
    r = result(monkeypatch, repo, ["skills/pl-run/SKILL.md"])
    assert r["reasons"] == {"tests/test_skill.py": "mentions skills/pl-run/SKILL.md"}


def test_a_repo_without_python_files_needs_the_full_suite(tmp_path, monkeypatch):
    r = result(monkeypatch, tmp_path, ["Program.cs"])
    assert r["full"] is True and r["tests"] == []
    assert "no Python files" in r["full_reason"]


def test_a_git_failure_prints_on_stderr_and_falls_back_to_full(repo, monkeypatch, capsys):
    fake_git(monkeypatch, repo, fail=True)
    assert T.cmd_tests(["affected", "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["full"] is True
    assert cap.err.startswith("pl tests:")


def test_default_base_is_the_merge_base_with_the_default_branch(repo, monkeypatch):
    calls = []
    fake_git(monkeypatch, repo, ["src/app/core.py"], calls=calls)
    T.affected(repo)
    assert any("symbolic-ref" in c for c in calls)
    assert any(c[-2:] == ["HEAD", "origin/main"] for c in calls if "merge-base" in c)
    assert any(c[-1] == "mergesha" for c in calls if "diff" in c)


def test_uncommitted_new_files_count(repo, monkeypatch):
    r = result(monkeypatch, repo, [], untracked=["tests/test_other.py"])
    assert r["tests"] == ["tests/test_other.py"]


def test_json_has_tests_reasons_and_full(repo, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    assert T.cmd_tests(["affected", "--json", "--base", "abc"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"tests", "reasons", "full"}
    assert out["tests"] == sorted(out["tests"])


def test_text_output_names_each_test_with_its_reason(repo, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    T.cmd_tests(["affected", "--base", "abc"])
    assert "tests/test_direct.py  imports app.core" in capsys.readouterr().out


def test_text_output_for_full_and_for_nothing(repo, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["uv.lock"])
    T.cmd_tests(["affected", "--base", "abc"])
    assert "full suite: uv.lock changed" in capsys.readouterr().out
    fake_git(monkeypatch, repo, [])
    T.cmd_tests(["affected", "--base", "abc"])
    assert "no tests affected" in capsys.readouterr().out


def test_pl_tests_runs_without_a_profile(monkeypatch, capsys):
    seen = []
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    monkeypatch.setattr(T, "cmd_tests", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["pl", "tests", "affected", "--json"])
    assert cli.main() == 0
    assert seen == [["affected", "--json"]]
