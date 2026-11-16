"""The CI size limit: a pull request fails over 300 non-test lines or 10 files."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "pr_size_check.py"
spec = importlib.util.spec_from_file_location("pr_size_check", SCRIPT)
psc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(psc)


def numstat(*rows):
    return "".join(f"{a}\t{d}\t{name}\n" for a, d, name in rows)


def test_small_change_is_ok():
    assert psc.check(numstat((10, 2, "src/pl/watch.py"), (40, 0, "tests/test_watch.py"))) == []


def test_empty_diff_is_ok():
    assert psc.check("") == []


def test_exactly_at_the_limits_is_ok():
    rows = [(30, 0, f"src/pl/m{i}.py") for i in range(10)]
    assert psc.check(numstat(*rows)) == []


def test_over_300_non_test_lines_fails():
    reasons = psc.check(numstat((200, 101, "src/pl/watch.py")))
    assert reasons and "301" in reasons[0] and "300" in reasons[0]


def test_lines_under_tests_do_not_count():
    assert psc.check(numstat((10, 0, "src/pl/watch.py"), (900, 50, "tests/test_watch.py"))) == []


def test_a_tests_word_elsewhere_still_counts():
    assert psc.check(numstat((301, 0, "src/pl/tests/helper.py")))


def test_over_10_files_fails_and_test_files_count():
    rows = [(1, 0, "src/pl/a.py")] + [(1, 0, f"tests/test_{i}.py") for i in range(10)]
    reasons = psc.check(numstat(*rows))
    assert reasons and "11" in reasons[0] and "10" in reasons[0]


def test_binary_files_count_as_files_with_no_lines():
    rows = [("-", "-", f"img{i}.png") for i in range(10)] + [(300, 0, "src/pl/a.py")]
    assert psc.check(numstat(*rows[:10])) == []
    assert any("files" in r for r in psc.check(numstat(*rows)))


def test_both_limits_give_two_reasons():
    rows = [(40, 0, f"src/pl/m{i}.py") for i in range(11)]
    assert len(psc.check(numstat(*rows))) == 2


def test_main_diffs_against_the_base_and_prints_ok(monkeypatch, capsys):
    calls = []

    def fake_git(args):
        calls.append(args)
        return numstat((5, 1, "src/pl/a.py"))
    monkeypatch.setattr(psc, "_git", fake_git)
    assert psc.main(["origin/main"]) == 0
    assert calls == [["diff", "--numstat", "origin/main...HEAD"]]
    assert capsys.readouterr().out.strip() == "ok"


def test_main_fails_over_the_limit(monkeypatch, capsys):
    monkeypatch.setattr(psc, "_git", lambda args: numstat((400, 0, "src/pl/a.py")))
    assert psc.main(["origin/main"]) == 1
    assert "400" in capsys.readouterr().out


def test_lockfile_lines_do_not_count():
    assert psc.check(numstat((200, 0, "src/pl/x.py"), (205, 0, "Cargo.lock"))) == []


def test_lockfile_in_a_subfolder_does_not_count():
    assert psc.check(numstat((10, 0, "src/pl/x.py"), (900, 0, "web/package-lock.json"))) == []


def test_code_over_the_limit_still_fails_with_a_lockfile():
    reasons = psc.check(numstat((320, 0, "src/pl/x.py"), (300, 0, "uv.lock")))
    assert reasons and "320" in reasons[0] and "620" not in reasons[0]


def test_a_lockfile_still_counts_as_a_file():
    rows = [(1, 0, f"src/pl/m{i}.py") for i in range(10)] + [(5, 0, "uv.lock")]
    assert "11 changed files" in psc.check(numstat(*rows))[0]


def test_a_lookalike_name_still_counts():
    assert psc.check(numstat((301, 0, "src/Cargo.lock.rs")))


def test_ci_and_auto_merge_use_the_same_lockfile_list():
    from pl import merges
    assert psc.LOCKFILE.pattern == merges.LOCKFILE.pattern
