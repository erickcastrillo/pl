import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from pl import config as C
from pl import skill_sources as S

OLD, NEW = "c" * 40, "d" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "PROFILES", {})
    monkeypatch.setattr(C, "ACCOUNTS", {})
    monkeypatch.setattr(C, "SKILLS", {"library": str(tmp_path / "lib")}, raising=False)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    C.STATE_DIR.mkdir()
    up = tmp_path / "up"
    (up / "skills" / "review").mkdir(parents=True)
    (up / "skills" / "review" / "SKILL.md").write_text("---\nname: review\ndescription: Review it\n---\nBody\n")
    (up / "skills" / "review" / "notes.md").write_text("notes\n")
    head = {"sha": OLD, "fail": False}

    def fake(argv, cwd=None, timeout=60):
        if argv[:2] == ["git", "ls-remote"]:
            if head["fail"]:
                raise RuntimeError("could not resolve host")
            return f"{head['sha']}\trefs/heads/main\n"
        if argv[:2] == ["git", "checkout"]:
            shutil.copytree(up, cwd, dirs_exist_ok=True)
        return ""
    monkeypatch.setattr(S, "_git", fake)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert S.install("github:acme/kit/skills/review@main", False, io.StringIO()) == 0
    return {"up": up, "lib": tmp_path / "lib", "head": head}


def publish(env):
    """Upstream moves on: SKILL.md changes, notes.md goes, a script appears."""
    skill = env["up"] / "skills" / "review"
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: Review it\n---\nBetter body\n")
    (skill / "notes.md").unlink()
    (skill / "run.sh").write_text("echo hi\n")
    env["head"]["sha"] = NEW


def tree(lib):
    return {p.relative_to(lib).as_posix(): p.read_bytes() for p in sorted(lib.rglob("*")) if p.is_file()}


def check(*a, **kw):
    out = io.StringIO()
    return S.check(out, *a, **kw), out.getvalue()


def update(name="review", yes=False):
    out = io.StringIO()
    return S.update(name, yes, out), out.getvalue()


def test_check_lists_a_newer_upstream_and_changes_nothing(env):
    publish(env)
    before = tree(env["lib"])
    code, out = check()
    assert code == 0
    assert f"review  acme/kit@{OLD[:7]} -> {NEW[:7]}" in out
    assert tree(env["lib"]) == before


def test_check_says_nothing_for_an_up_to_date_skill(env):
    code, out = check()
    assert code == 0 and "review" not in out


def test_check_json_and_network_failures(env):
    publish(env)
    code, out = check(as_json=True)
    row = json.loads(out)[0]
    assert code == 0 and row["name"] == "review" and row["update"] is True and row["latest"] == NEW
    env["head"]["fail"] = True
    code, out = check()
    assert code == 1 and "review" in out and "could not resolve host" in out


def test_update_shows_the_diff_and_asks(env, monkeypatch):
    publish(env)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    code, out = update()
    assert code == 0
    assert "-Body" in out and "+Better body" in out
    assert "run.sh" in out and "notes.md" in out
    assert out.index("script") < out.index("+Better body")
    assert asked == [f"Update review to acme/kit@{NEW[:7]}? [y/N]: "]
    assert "Better body" in (env["lib"] / "review" / "SKILL.md").read_text()
    assert not (env["lib"] / "review" / "notes.md").exists()
    rec = json.loads((env["lib"] / S.SOURCES_FILE).read_text())["review"]
    assert rec["sha"] == NEW and set(rec["files"]) == {"SKILL.md", "run.sh"}


def test_declining_changes_nothing(env, monkeypatch):
    publish(env)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    before = tree(env["lib"])
    code, out = update()
    assert code == 0 and "nothing updated" in out
    assert tree(env["lib"]) == before


def test_an_edited_install_is_kept_beside_the_new_one(env):
    publish(env)
    (env["lib"] / "review" / "SKILL.md").write_text("my own wording\n")
    code, out = update(yes=True)
    assert code == 0
    kept = list((env["lib"] / ".pl-edited").glob("review-*"))
    assert len(kept) == 1 and str(kept[0]) in out
    assert (kept[0] / "SKILL.md").read_text() == "my own wording\n"
    assert "Better body" in (env["lib"] / "review" / "SKILL.md").read_text()


def test_an_untouched_install_leaves_no_edited_copy(env):
    publish(env)
    assert update(yes=True)[0] == 0
    assert not (env["lib"] / ".pl-edited").exists()


def test_update_without_a_terminal_needs_yes(env, monkeypatch):
    publish(env)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = tree(env["lib"])
    code, _ = update()
    assert code == 2 and tree(env["lib"]) == before
    assert update(yes=True)[0] == 0


def test_update_of_an_unknown_skill_is_refused(env):
    assert update("nope", yes=True)[0] == 2


def test_nothing_calls_update_on_its_own():
    src = Path(__file__).parent.parent / "src" / "pl"
    callers = {p.name for p in src.rglob("*.py") if "skill_sources.update(" in p.read_text()}
    assert callers <= {"skills.py"}
