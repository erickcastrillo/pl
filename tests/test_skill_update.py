import io
import json
import os
import sys

import pytest

from pl import config as C
from pl import skill_sources as S


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    acct = tmp_path / "acct"
    (acct / "skills").mkdir(parents=True)
    monkeypatch.setattr(C, "PROFILES", {"work": str(acct)})
    monkeypatch.setattr(C, "ACCOUNTS", {})
    monkeypatch.setattr(C, "SKILLS", {"library": str(tmp_path / "lib")}, raising=False)
    monkeypatch.setattr(C, "PROMPTS", {"spec": "/pl-spec {id}", "plan": "/pl:review {id}", "run": "/pl-run {id}"})
    monkeypatch.setattr(C, "SERVICES", {"nightly": {"prompt": "/loop 1h /review"}, "other": {"prompt": "/pl-review"}})
    monkeypatch.setattr(C, "LOOPS_OFF", {"weekly": {"skill": "review"}})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    lib = tmp_path / "lib"
    for n in ("review", "own"):
        (lib / n).mkdir(parents=True)
        (lib / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: d\n---\n")
    (lib / S.SOURCES_FILE).write_text(json.dumps({"review": {"source": "github", "repo": "acme/kit", "sha": "a" * 40}}))
    os.symlink(lib / "review", acct / "skills" / "review", target_is_directory=True)
    return {"lib": lib, "acct": acct}


def remove(name="review", yes=False):
    out = io.StringIO()
    return S.remove(name, yes, out), out.getvalue()


def test_remove_deletes_the_files_and_the_account_links(env):
    code, out = remove()
    assert code == 0
    assert not (env["lib"] / "review").exists()
    assert not os.path.lexists(env["acct"] / "skills" / "review")
    assert len(list((env["lib"] / ".pl-trash").glob("review-*"))) == 1
    assert "review" not in S.records()


def test_remove_reports_the_loops_and_stages_that_used_it(env):
    code, out = remove()
    assert "used by: stage plan, loop nightly, loop weekly" in out


def test_remove_refuses_a_builtin_or_an_own_skill(env):
    for name in ("pl-spec", "own"):
        code, out = remove(name, yes=True)
        assert code == 2
    assert (env["lib"] / "own").is_dir()


def test_remove_without_a_terminal_needs_yes(env, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert remove()[0] == 2
    assert (env["lib"] / "review").is_dir()
    assert remove(yes=True)[0] == 0
