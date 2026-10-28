"""The built-in pl-run skill bootstraps fresh worktrees; the library upgrade, what's new and README announce it."""
import re
import shutil
from pathlib import Path

import pytest

from pl import config as C
from pl import skills, whatsnew

BOOTSTRAP = 'pl worktrees bootstrap . --card <id>'
README = Path(__file__).resolve().parent.parent / "README.md"
SHIPPED = skills.BUILTIN_DIR


def _run_skill():
    return (SHIPPED / "pl-run" / "SKILL.md").read_text()


CURRENT = int(re.search(r"^pl-builtin-version: (\d+)$", _run_skill(), re.M).group(1))   # later changes to pl-run bump it


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "SKILLS", {}, raising=False)
    monkeypatch.setattr(C, "PROFILES", {}, raising=False)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state", raising=False)
    return tmp_path / ".local" / "share" / "pl" / "skills"


@pytest.fixture
def shipped_v4(tmp_path, monkeypatch):
    """The package's built-ins with pl-run as it was at version 4."""
    d = tmp_path / "pkg4"
    shutil.copytree(SHIPPED, d)
    f = d / "pl-run" / "SKILL.md"
    f.write_text(f.read_text().replace(f"pl-builtin-version: {CURRENT}", "pl-builtin-version: 4").replace(BOOTSTRAP, "git status"))
    monkeypatch.setattr(skills, "BUILTIN_DIR", d)
    return d


def test_pl_run_builtin_version_is_at_least_5():
    assert CURRENT >= 5


def test_pl_run_line_count_within_150_lines():
    assert len(_run_skill().splitlines()) <= 150


def test_pl_run_invokes_worktree_bootstrap_in_step_2():
    text = _run_skill()
    step2 = text[text.index("2. pl started you"):text.index("3. For each work package")]
    assert BOOTSTRAP in step2


def test_pl_run_instructs_proceeding_on_bootstrap_diagnostics():
    assert re.search(r"reports a problem.*go on", _run_skill(), re.S | re.I)


def test_install_builtins_updates_unedited_pl_run_from_v4_to_current(lib, shipped_v4, monkeypatch):
    skills.install_builtins()
    monkeypatch.setattr(skills, "BUILTIN_DIR", SHIPPED)
    assert skills.install_builtins() == [f"updated built-in pl-run to version {CURRENT}"]
    assert BOOTSTRAP in (lib / "pl-run" / "SKILL.md").read_text()


def test_install_builtins_preserves_custom_edited_pl_run(lib, shipped_v4, monkeypatch):
    skills.install_builtins()
    mine = lib / "pl-run" / "SKILL.md"
    mine.write_text(mine.read_text() + "\nMy own step.\n")
    monkeypatch.setattr(skills, "BUILTIN_DIR", SHIPPED)
    skills.install_builtins()
    assert mine.read_text().endswith("My own step.\n")


def test_install_builtins_idempotent_on_v5(lib):
    skills.install_builtins()
    assert skills.install_builtins() == []


def test_whatsnew_contains_worktrees_bootstrap_entry():
    entry = next(e for e in whatsnew.ENTRIES if e["title"] == "Bootstrap fresh worktrees via .pl/worktree.yaml")
    assert entry["try"] == "pl worktrees bootstrap <path> --dry-run"


def test_whatsnew_entry_key_is_sorted_and_valid():
    keys = [e["key"] for e in whatsnew.ENTRIES]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_readme_worktrees_section_documents_bootstrap():
    text = README.read_text()
    section = text[text.index("**Worktrees folder.**"):]
    section = section[:section.index("\n\n")]
    assert ".pl/worktree.yaml" in section and "pl worktrees bootstrap" in section and "pl-run" in section
