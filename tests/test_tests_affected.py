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


def test_a_changed_module_lists_only_direct_importers(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert r["reasons"]["tests/test_direct.py"] == "imports app.core"
    assert "tests/test_mid.py" not in r["tests"]
    assert "tests/test_other.py" not in r["tests"]
    assert r["full"] is False


def test_reasons_never_name_a_via_chain(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert not any("(via" in reason for reason in r["reasons"].values())


def test_each_direct_import_form_is_matched(repo, monkeypatch):
    for rel, text in {
        "tests/test_f1.py": "import app.other\n",
        "tests/test_f3.py": "from app.other import Y\n",
        "tests/test_f4.py": "from app.mid import x\n",
    }.items():
        (repo / rel).write_text(text)
    r = result(monkeypatch, repo, ["src/app/other.py"])
    for t in ("tests/test_f1.py", "tests/test_f3.py", "tests/test_other.py"):
        assert r["reasons"][t] == "imports app.other"
    assert "tests/test_f4.py" not in r["tests"]
    assert r["full"] is False


def test_a_changed_test_keeps_its_reason_when_it_also_imports_the_changed_module(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py", "tests/test_direct.py"])
    assert r["reasons"]["tests/test_direct.py"] == "changed test"


def test_merge_check_still_runs_the_full_suite_and_review_uses_the_test_set(repo):
    import inspect

    from pl import merge_check, skills

    text = (skills.BUILTIN_DIR / "pl-review" / "SKILL.md").read_text()
    assert "Run the tests the change needs" in text and "`pl tests review`" in text
    assert "tests_affected.full_command" in inspect.getsource(merge_check._tests_rule)
    assert "tests" not in T.test_command(repo)[T.test_command(repo).index("-q") + 1:]


def test_a_name_match_is_listed_even_without_an_import(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert r["reasons"]["tests/test_core.py"] == "name match core.py"


def test_a_changed_test_is_listed(repo, monkeypatch):
    r = result(monkeypatch, repo, ["tests/test_other.py"])
    assert r["tests"] == ["tests/test_other.py"]
    assert r["reasons"]["tests/test_other.py"] == "changed test"


def test_an_importer_through_a_relative_import_is_not_listed(repo, monkeypatch):
    r = result(monkeypatch, repo, ["src/app/core.py"])
    assert "tests/test_rel.py" not in r["tests"]
    r = result(monkeypatch, repo, ["src/app/rel.py"])
    assert r["reasons"]["tests/test_rel.py"] == "imports app.rel"


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
    assert set(out) == {"tests", "reasons", "full", "command"}
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


@pytest.fixture
def budget(tmp_path_factory, monkeypatch):
    from pl import worker_budget
    machine = tmp_path_factory.mktemp("machine")
    monkeypatch.setenv("PL_MACHINE_DIR", str(machine))
    monkeypatch.setattr(worker_budget, "_cpus", lambda: 12)
    return machine


def _lease(machine, workers):
    import os
    d = machine / "test-workers"
    d.mkdir(exist_ok=True)
    (d / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "workers": workers, "at": "now"}))


def test_full_prints_a_pytest_command_with_half_the_cores(repo, budget, monkeypatch, capsys):
    fake_git(monkeypatch, repo)
    assert T.cmd_tests(["full"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("pl tests run -- ") and "pytest -n 6" in out


def test_full_takes_running_workers_off_the_machine_budget(repo, budget, monkeypatch, capsys):
    (budget / "machine.toml").write_text("[tests]\nmax_workers = 6\n")
    _lease(budget, 4)
    fake_git(monkeypatch, repo)
    T.cmd_tests(["full"])
    assert "pytest -n 2" in capsys.readouterr().out


@pytest.mark.parametrize("workers,expected", [(3, 3), (9, 6)])
def test_repo_workers_is_a_ceiling(repo, budget, monkeypatch, capsys, workers, expected):
    settings(repo, f"[tests]\nworkers = {workers}\n")
    fake_git(monkeypatch, repo)
    T.cmd_tests(["full"])
    assert f"pytest -n {expected}" in capsys.readouterr().out


def test_workers_auto_leaves_the_budget_to_decide(repo, budget, monkeypatch, capsys):
    settings(repo, '[tests]\nworkers = "auto"\n')
    fake_git(monkeypatch, repo)
    T.cmd_tests(["full"])
    assert "pytest -n 6" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["full", "affected"])
@pytest.mark.parametrize("value", ['"many"', "0", "true"])
def test_bad_workers_is_an_error_for_full_and_affected(repo, budget, monkeypatch, capsys, action, value):
    settings(repo, f"[tests]\nworkers = {value}\n")
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    assert T.cmd_tests([action]) == 2
    err = capsys.readouterr().err
    assert ".pl/tests.toml" in err and "workers" in err


def test_uv_lock_picks_uv_run_pytest(repo, budget, monkeypatch, capsys):
    (repo / "uv.lock").write_text("")
    fake_git(monkeypatch, repo)
    T.cmd_tests(["full"])
    assert "-- uv run pytest -n 6 -q" in capsys.readouterr().out
    (repo / "uv.lock").unlink()
    T.cmd_tests(["full"])
    assert "-- python -m pytest -n 6 -q" in capsys.readouterr().out


def test_affected_text_ends_with_the_command_for_its_files(repo, budget, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    T.cmd_tests(["affected", "--base", "abc"])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("pl tests run -- ") and "-n 6 -q tests/test_core.py tests/test_direct.py" in last


def test_affected_full_ends_with_the_full_suite_command(repo, budget, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["uv.lock"])
    T.cmd_tests(["affected", "--base", "abc"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "full suite: uv.lock changed"
    assert lines[-1].endswith("pytest -n 6 -q") or lines[-1].endswith("-n 6 -q")


def test_affected_with_no_tests_prints_no_command(repo, budget, monkeypatch, capsys):
    fake_git(monkeypatch, repo, [])
    T.cmd_tests(["affected", "--base", "abc"])
    assert capsys.readouterr().out.strip() == "no tests affected"
    T.cmd_tests(["affected", "--base", "abc", "--json"])
    assert json.loads(capsys.readouterr().out)["command"] is None


def test_json_adds_command_and_keeps_its_fields(repo, budget, monkeypatch, capsys):
    fake_git(monkeypatch, repo, ["src/app/core.py"])
    T.cmd_tests(["affected", "--json", "--base", "abc"])
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"tests", "reasons", "full", "command"}
    assert out["command"].startswith("pl tests run -- ") and "tests/test_direct.py" in out["command"]


def test_run_holds_the_workers_named_by_n_while_the_command_runs(budget, monkeypatch):
    from pl import worker_budget
    (budget / "machine.toml").write_text("[tests]\nmax_workers = 8\n")
    seen = []

    def run(argv, cwd, timeout=60, capture=True):
        seen.append((argv, capture, worker_budget.in_use()))
        return 7, "", ""

    monkeypatch.setattr(T, "_run", run)
    assert T.cmd_tests(["run", "--", "pytest", "-n", "4", "-q"]) == 7
    assert seen == [(["pytest", "-n", "4", "-q"], False, 4)]
    assert worker_budget.in_use() == 0


def test_run_without_n_holds_one_worker(budget, monkeypatch):
    from pl import worker_budget
    (budget / "machine.toml").write_text("[tests]\nmax_workers = 8\n")
    seen = []
    monkeypatch.setattr(T, "_run", lambda argv, cwd, timeout=60, capture=True: seen.append(worker_budget.in_use()) or (0, "", ""))
    assert T.cmd_tests(["run", "--", "pytest", "-n8", "x"]) == 0
    assert T.cmd_tests(["run", "--", "pytest"]) == 0
    assert seen == [8, 1]


# ---------- pl tests full: the command that runs the whole suite ----------

def _tests_toml(root, body):
    (root / ".pl").mkdir(exist_ok=True)
    (root / ".pl" / "tests.toml").write_text(body)


def test_full_command_is_serial_without_tests_toml(tmp_path):
    assert T.full_command(tmp_path) == "uv run pytest -q"


def test_full_command_is_serial_without_workers_key(tmp_path):
    _tests_toml(tmp_path, '[tests]\nalways = ["tests/test_a.py"]\n')
    assert T.full_command(tmp_path) == "uv run pytest -q"


def test_full_command_adds_auto_workers(tmp_path):
    _tests_toml(tmp_path, '[tests]\nworkers = "auto"\n')
    assert T.full_command(tmp_path) == "uv run pytest -q -n auto"


def test_full_command_adds_numeric_workers(tmp_path):
    _tests_toml(tmp_path, "[tests]\nworkers = 4\n")
    assert T.full_command(tmp_path) == "uv run pytest -q -n 4"


@pytest.mark.parametrize("value", ['"many"', "0", "-2", "true"])
def test_full_command_rejects_bad_workers(tmp_path, value):
    _tests_toml(tmp_path, f"[tests]\nworkers = {value}\n")
    with pytest.raises(T.TestsConfigError) as e:
        T.full_command(tmp_path)
    assert str(tmp_path / ".pl" / "tests.toml") in str(e.value) and "workers" in str(e.value)


def test_full_command_rejects_unparsable_file(tmp_path):
    _tests_toml(tmp_path, "[tests\n")
    with pytest.raises(T.TestsConfigError, match="tests.toml"):
        T.full_command(tmp_path)


def test_full_command_leaves_non_pytest_commands(tmp_path):
    _tests_toml(tmp_path, '[tests]\nworkers = "auto"\n')
    assert T.full_command(tmp_path, "make test") == "make test"


@pytest.mark.parametrize("cmd", ["uv run pytest -q -n 2", "pytest -n4", "pytest --numprocesses=3"])
def test_full_command_leaves_commands_that_set_n(tmp_path, cmd):
    _tests_toml(tmp_path, '[tests]\nworkers = "auto"\n')
    assert T.full_command(tmp_path, cmd) == cmd


def test_full_command_does_not_read_the_file_for_other_commands(tmp_path):
    _tests_toml(tmp_path, "[tests\n")
    assert T.full_command(tmp_path, "make test") == "make test"


def test_pl_repo_tests_toml_sets_a_capped_worker_count():
    # several builds and reviews share one machine, so the repo caps workers instead of "auto"
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).parents[1] / ".pl" / "tests.toml").read_text())
    assert isinstance(data["tests"]["workers"], int) and 1 < data["tests"]["workers"] <= 4


# ---------- select(): which set review and the merge check run ----------

def select(monkeypatch, repo, changed, labels=(), **kw):
    fake_git(monkeypatch, repo, changed, **kw)
    return T.select(repo, "abc", labels)


def test_select_one_module_runs_its_affected_tests(repo, monkeypatch):
    r = select(monkeypatch, repo, ["src/app/core.py"])
    assert r == {"full": False, "tests": ["tests/test_core.py", "tests/test_direct.py"], "reason": "affected: 2 files", "e2e_ui": False}


@pytest.mark.parametrize("changed,reason", [
    (["pyproject.toml"], "pyproject.toml changed"),
    (["uv.lock"], "uv.lock changed"),
    (["tests/conftest.py"], "tests/conftest.py changed"),
    (["docs/x.md"], "docs/x.md matches full_on docs/*"),
])
def test_select_pyproject_or_full_on_forces_full_with_its_reason(repo, monkeypatch, changed, reason):
    settings(repo, '[tests]\nfull_on = ["docs/*"]\n')
    r = select(monkeypatch, repo, changed)
    assert r["full"] is True and r["reason"] == f"full suite: {reason}"


def test_select_git_failure_or_bad_affected_cmd_is_full_with_that_reason(repo, monkeypatch):
    r = select(monkeypatch, repo, [], fail=True)
    assert r["full"] and r["reason"].startswith("full suite: git ") and r["e2e_ui"] is False
    settings(repo, '[tests]\naffected_cmd = ["finder"]\n')
    r = select(monkeypatch, repo, ["src/app/core.py"], cmd=(1, "", "boom"))
    assert r["full"] and r["reason"] == "full suite: affected_cmd failed: boom"


def _many_modules(repo, n):
    names = [f"src/app/m{i}.py" for i in range(n)]
    for p in names:
        (repo / p).write_text("")
    return names


def test_select_over_full_over_modules_is_full(repo, monkeypatch):
    settings(repo, "[tests]\nfull_over = 3\n")
    r = select(monkeypatch, repo, _many_modules(repo, 4))
    assert r["full"] and r["reason"] == "full suite: 4 modules changed (over 3)"
    assert not select(monkeypatch, repo, _many_modules(repo, 3))["full"]


def test_select_default_full_over_is_20(repo, monkeypatch):
    assert not select(monkeypatch, repo, _many_modules(repo, 20))["full"]
    assert select(monkeypatch, repo, _many_modules(repo, 21))["reason"] == "full suite: 21 modules changed (over 20)"


@pytest.mark.parametrize("value", ["0", '"x"', "true", "1.5"])
def test_select_bad_full_over_is_an_error_naming_tests_toml(repo, monkeypatch, value):
    settings(repo, f"[tests]\nfull_over = {value}\n")
    with pytest.raises(T.TestsConfigError, match=r"\.pl/tests\.toml.*full_over"):
        select(monkeypatch, repo, ["src/app/core.py"])


def test_select_label_forces_full_regardless_of_diff(repo, monkeypatch):
    r = select(monkeypatch, repo, ["src/app/core.py"], labels=["pl:full-tests"])
    assert r["full"] and r["reason"] == "full suite: pl:full-tests label"


@pytest.mark.parametrize("path", ["src/pl/tui/app.py", "tests/e2e_ui/test_x.py"])
def test_select_tui_or_e2e_ui_change_adds_e2e_ui(repo, monkeypatch, path):
    assert select(monkeypatch, repo, [path])["e2e_ui"] is True
    assert select(monkeypatch, repo, [path], labels=["pl:full-tests"])["e2e_ui"] is True


def test_select_other_change_has_no_e2e_ui(repo, monkeypatch):
    assert select(monkeypatch, repo, ["src/app/core.py"])["e2e_ui"] is False


def test_select_docs_only_change_selects_zero_files(repo, monkeypatch):
    r = select(monkeypatch, repo, ["README.md"])
    assert r == {"full": False, "tests": [], "reason": "affected: 0 files", "e2e_ui": False}


# ---------- pl tests review ----------

def review(monkeypatch, repo, capsys, changed, *flags, **kw):
    fake_git(monkeypatch, repo, changed, **kw)
    code = T.cmd_tests(["review", "--base", "abc", *flags])
    return code, capsys.readouterr()


def test_review_prints_reason_then_affected_command(repo, budget, monkeypatch, capsys):
    code, cap = review(monkeypatch, repo, capsys, ["src/app/core.py"])
    lines = cap.out.strip().splitlines()
    assert code == 0 and lines[0] == "affected: 2 files" and len(lines) == 2
    assert lines[1].endswith("-q tests/test_core.py tests/test_direct.py")


def test_review_full_prints_full_suite_reason_and_full_command(repo, budget, monkeypatch, capsys):
    _, cap = review(monkeypatch, repo, capsys, ["uv.lock"])
    lines = cap.out.strip().splitlines()
    assert lines[0] == "full suite: uv.lock changed" and lines[1].endswith("-q") and len(lines) == 2


def test_review_zero_files_prints_no_command(repo, budget, monkeypatch, capsys):
    _, cap = review(monkeypatch, repo, capsys, ["README.md"])
    assert cap.out.strip() == "affected: 0 files"


def test_review_adds_e2e_ui_command_for_tui_changes(repo, budget, monkeypatch, capsys):
    _, cap = review(monkeypatch, repo, capsys, ["src/pl/tui/app.py"])
    lines = cap.out.strip().splitlines()
    assert lines[0] == "affected: 0 files; plus e2e_ui"
    assert lines[-1].endswith("-n 6 -m e2e_ui -q tests/e2e_ui")


def test_review_label_flag_forces_full(repo, budget, monkeypatch, capsys):
    _, cap = review(monkeypatch, repo, capsys, ["src/app/core.py"], "--label", "x", "--label", "pl:full-tests")
    assert cap.out.splitlines()[0] == "full suite: pl:full-tests label"


def test_review_uses_budget_workers_like_full(repo, budget, monkeypatch, capsys):
    (budget / "machine.toml").write_text("[tests]\nmax_workers = 6\n")
    _lease(budget, 4)
    _, cap = review(monkeypatch, repo, capsys, ["src/app/core.py"])
    assert "pytest -n 2 -q" in cap.out


def test_review_bad_full_over_exits_2_naming_tests_toml(repo, budget, monkeypatch, capsys):
    settings(repo, "[tests]\nfull_over = 0\n")
    code, cap = review(monkeypatch, repo, capsys, ["src/app/core.py"])
    assert code == 2 and ".pl/tests.toml" in cap.err and "full_over" in cap.err


def test_review_json_fields(repo, budget, monkeypatch, capsys):
    _, cap = review(monkeypatch, repo, capsys, ["src/pl/tui/app.py"], "--json")
    out = json.loads(cap.out)
    assert set(out) == {"full", "tests", "reason", "e2e_ui", "command", "e2e_command"}
    assert out["command"] is None
    assert out["e2e_command"].endswith("-m e2e_ui -q tests/e2e_ui")
