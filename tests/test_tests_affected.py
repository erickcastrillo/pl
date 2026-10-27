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


def fake_git(monkeypatch, root, changed=(), untracked=(), fail=False, calls=None, cmd=None, cwds=None):
    def run(argv, cwd=None, timeout=None):
        if calls is not None:
            calls.append(argv)
        if cwds is not None:
            cwds.append(cwd)
        if cmd is not None and argv[0] != "git":
            return cmd
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


def settings(repo, text):
    f = repo / ".pl" / "tests.toml"
    f.parent.mkdir(exist_ok=True)
    f.write_text(text)


TS_TOOL = (0, "tests/a_test.ts\ntests/b_test.ts\n", "")


def test_affected_cmd_list_replaces_the_python_finder(repo, monkeypatch):
    settings(repo, '[tests]\naffected_cmd = ["tool", "--x"]\n')
    calls, cwds = [], []
    r = result(monkeypatch, repo, ["src/app/core.py"], cmd=TS_TOOL, calls=calls, cwds=cwds)
    assert ["tool", "--x", "abc"] in calls
    assert cwds[calls.index(["tool", "--x", "abc"])] == repo
    assert r["tests"] == ["tests/a_test.ts", "tests/b_test.ts"]
    assert set(r["reasons"].values()) == {"from affected_cmd"}
    assert "tests/test_direct.py" not in r["tests"]


def test_affected_cmd_string_is_split_and_output_cleaned(repo, monkeypatch):
    settings(repo, '[tests]\naffected_cmd = "scripts/affected.sh --fast"\n')
    calls = []
    r = result(monkeypatch, repo, ["x.py"], cmd=(0, "a\n\n  \nb\na\n", ""), calls=calls)
    assert ["scripts/affected.sh", "--fast", "abc"] in calls
    assert r["tests"] == ["a", "b"]


@pytest.mark.parametrize("cmd_line", ["", 'affected_cmd = ["tool"]\n'])
def test_always_is_listed_with_the_finder_and_with_affected_cmd(repo, monkeypatch, cmd_line):
    settings(repo, f'[tests]\n{cmd_line}always = ["tests/test_x.py"]\n')
    r = result(monkeypatch, repo, ["src/app/other.txt"], cmd=(0, "", ""))
    assert r["reasons"]["tests/test_x.py"] == "always rule"
    assert "tests/test_x.py" in r["tests"]


@pytest.mark.parametrize("changed, patterns, full", [
    ("src/App/App.csproj", '["*.csproj"]', True),
    ("package.json", '["*.csproj", "package.json"]', True),
    ("web/package.json", '["package.json"]', False),
])
def test_full_on_matches_changed_paths_with_fnmatch(repo, monkeypatch, changed, patterns, full):
    settings(repo, f"[tests]\nfull_on = {patterns}\n")
    r = result(monkeypatch, repo, [changed])
    assert r["full"] is full
    if full:
        assert changed in r["full_reason"]


def test_non_python_repo_works_from_settings_alone(tmp_path, monkeypatch, capsys):
    (tmp_path / "README.md").write_text("hi\n")
    settings(tmp_path, '[tests]\nfull_on = ["*.csproj"]\n')
    assert result(monkeypatch, tmp_path, ["A.csproj"])["full"] is True
    settings(tmp_path, '[tests]\naffected_cmd = ["tool"]\n')
    fake_git(monkeypatch, tmp_path, ["Program.cs"], cmd=TS_TOOL)
    assert T.cmd_tests(["affected", "--json"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["tests"] == ["tests/a_test.ts", "tests/b_test.ts"] and out["full"] is False
    assert "Python" not in cap.err


@pytest.mark.parametrize("cmd", [
    (2, "", "boom"),
    (1, "", "timed out after 120 seconds"),
    (1, "", "[Errno 2] No such file or directory: 'tool'"),
])
def test_failing_affected_cmd_falls_back_to_full(repo, monkeypatch, capsys, cmd):
    settings(repo, '[tests]\naffected_cmd = ["tool"]\nalways = ["tests/test_x.py"]\n')
    fake_git(monkeypatch, repo, ["src/app/core.py"], cmd=cmd)
    assert T.cmd_tests(["affected", "--json"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["full"] is True and out["tests"] == ["tests/test_x.py"]
    assert cap.err.startswith("pl tests: affected_cmd failed:") and len(cap.err.splitlines()) == 1


@pytest.mark.parametrize("text", ["[tests\nbroken", '[tests]\nalways = "tests/a.py"\n'])
def test_bad_settings_file_falls_back_to_full(repo, monkeypatch, capsys, text):
    settings(repo, text)
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    assert T.cmd_tests(["affected", "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["full"] is True
    assert ".pl/tests.toml" in cap.err


def test_no_settings_or_no_tests_table_matches_slice_1(repo, monkeypatch):
    plain = result(monkeypatch, repo, ["src/app/core.py"])
    settings(repo, "[other]\nx = 1\n")
    assert result(monkeypatch, repo, ["src/app/core.py"]) == plain


def test_pl_repo_settings_list_the_always_tests():
    from pathlib import Path
    always = T._settings(Path(__file__).parents[1])["always"]
    for name in ("tests/test_whatsnew.py", "tests/test_cli_parity.py", "tests/test_worktrees_skill_integration.py"):
        assert name in always
