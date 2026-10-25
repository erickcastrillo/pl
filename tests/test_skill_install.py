import io
import json
import shutil
import stat
import sys

import pytest

from pl import config as C
from pl import skill_sources as S
from pl import skills

SHA = "c" * 40


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
    (up / "skills" / "review" / "check.py").write_text("print(1)\n")
    (up / "skills" / "review" / "notes.md").write_text("notes\n")
    calls = []

    def fake(argv, cwd=None, timeout=60):
        calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            return f"{SHA}\trefs/heads/main\n"
        if argv[:2] == ["git", "checkout"]:
            shutil.copytree(up, cwd, dirs_exist_ok=True, symlinks=True)
        return ""
    monkeypatch.setattr(S, "_git", fake)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    return {"up": up, "lib": tmp_path / "lib", "calls": calls, "state": C.STATE_DIR}


def run(text="github:acme/kit/skills/review@main", yes=False, **kw):
    out = io.StringIO()
    return S.install(text, yes, out, **kw), out.getvalue()


def test_install_prints_every_file_and_asks_first(env, monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    code, out = run()
    assert code == 0
    for f in ("SKILL.md", "check.py", "notes.md"):
        assert f in out
    assert "print(1)" in out
    assert asked == [f"Install review from acme/kit@{SHA[:7]}? [y/N]: "]
    assert (env["lib"] / "review" / "SKILL.md").is_file()
    assert not (env["lib"] / "review" / ".git").exists()


def test_declining_installs_nothing(env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out = run()
    assert code == 0 and "nothing installed" in out
    assert not (env["lib"] / "review").exists()
    assert list(env["state"].iterdir()) == []


def test_flags_come_first_by_kind(env):
    (env["up"] / "skills" / "review" / "hooks").mkdir()
    (env["up"] / "skills" / "review" / "hooks" / "hooks.json").write_text("{}")
    code, out = run(dry_run=True)
    assert code == 0
    assert "Runs code: script x1, hook x1" in out
    assert out.index("Runs code") < out.index("SKILL.md") < out.index("print(1)")
    assert out.index("script  check.py") < out.index("hook  hooks/hooks.json")
    assert not env["lib"].exists()


def test_no_terminal_and_no_yes_exits_2_and_installs_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code, out = run()
    assert code == 2 and out == ""
    assert "no terminal to confirm on; add --yes" in capsys.readouterr().err
    assert not env["lib"].exists() and env["calls"] == []
    code, out = run(yes=True)
    assert code == 0 and (env["lib"] / "review" / "SKILL.md").is_file()
    assert "print(1)" not in out


def test_an_existing_library_skill_is_never_overwritten(env, capsys):
    (env["lib"] / "review").mkdir(parents=True)
    (env["lib"] / "review" / "SKILL.md").write_text("mine")
    code, _ = run(yes=True)
    assert code == 2
    assert "already exists; pl never overwrites" in capsys.readouterr().err
    assert (env["lib"] / "review" / "SKILL.md").read_text() == "mine"


def test_a_source_that_is_not_github_or_has_no_path_exits_2(env, capsys):
    assert run("https://gitlab.com/acme/kit", yes=True)[0] == 2
    assert "GitHub" in capsys.readouterr().err
    assert run("github:acme/kit/nope", yes=True)[0] == 2
    assert "no such path" in capsys.readouterr().err


def test_the_temp_checkout_is_removed_after_a_failure(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(S, "inventory", boom)
    with pytest.raises(OSError):
        run(yes=True)
    assert list(env["state"].iterdir()) == []


def test_a_failing_fetch_leaves_no_temp_checkout(env, monkeypatch):
    real = S._git

    def fail_on_fetch(argv, cwd=None, timeout=60):
        if argv[:2] == ["git", "fetch"]:
            raise RuntimeError("fatal: not found")
        return real(argv, cwd, timeout)
    monkeypatch.setattr(S, "_git", fail_on_fetch)
    assert run(yes=True)[0] == 1
    assert list(env["state"].iterdir()) == []


def test_a_failed_copy_leaves_no_half_installed_skill(env, monkeypatch):
    real = shutil.copytree

    def boom(*a, **k):
        if "/.install-" in str(a[1]):
            raise OSError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(S.shutil, "copytree", boom)
    with pytest.raises(OSError):
        run(yes=True)
    assert [p.name for p in env["lib"].iterdir() if p.name.startswith(".install-")] == []
    assert not (env["lib"] / "review").exists()


def test_install_of_main_records_repo_path_and_resolved_sha(env):
    run(yes=True)
    path = env["lib"] / ".pl-sources.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rec = json.loads(path.read_text())["review"]
    assert rec["source"] == "github" and rec["repo"] == "acme/kit" and rec["path"] == "skills/review"
    assert rec["ref"] == "main" and rec["sha"] == SHA and rec["plugin"] is None and rec["installed_at"]
    assert set(rec["files"]) == {"SKILL.md", "check.py", "notes.md"}
    assert rec["files"]["notes.md"] == skills._sha256(b"notes\n")


def test_list_json_shows_the_source(env, capsys):
    run(yes=True)
    capsys.readouterr()
    import argparse
    a = argparse.Namespace(skills_cmd="list", json=True)
    assert skills.cmd_skills(a) == 0
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["name"] == "review")
    assert row["source"]["repo"] == "acme/kit" and row["source"]["sha"] == SHA


def test_a_link_that_leaves_the_skill_folder_but_stays_in_the_checkout_is_refused(env, capsys):
    (env["up"] / ".ssh").mkdir()
    (env["up"] / ".ssh" / "id_rsa").write_text("harmless")
    (env["up"] / "skills" / "review" / "notes.md").unlink()
    (env["up"] / "skills" / "review" / "notes.md").symlink_to("../../.ssh/id_rsa")
    code, _ = run(yes=True)
    assert code == 2
    assert "link out of the source" in capsys.readouterr().err
    assert not (env["lib"] / "review").exists()


def test_a_link_inside_the_skill_folder_is_still_installed(env):
    (env["up"] / "skills" / "review" / "notes.md").unlink()
    (env["up"] / "skills" / "review" / "notes.md").symlink_to("check.py")
    code, _ = run(yes=True)
    assert code == 0
    assert (env["lib"] / "review" / "notes.md").is_symlink()


def test_a_path_component_that_is_a_link_out_of_the_checkout_is_refused(env, capsys, tmp_path):
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
    (outside / "note.txt").write_text("secret")
    (env["up"] / "linked").symlink_to(outside)
    code, _ = run("github:acme/kit/linked@main", yes=True)
    assert code == 2
    assert "outside the source" in capsys.readouterr().err
    assert not (env["lib"] / "linked").exists()


def test_ls_remote_ends_options_before_the_url(env):
    run(yes=True)
    ls = next(a for a in env["calls"] if a[:2] == ["git", "ls-remote"])
    assert ls[2] == "--"
