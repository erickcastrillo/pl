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
            shutil.copytree(up, cwd, dirs_exist_ok=True)
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


def _plugin(up, name="kit"):
    (up / ".claude-plugin").mkdir()
    (up / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    (up / "hooks").mkdir()
    (up / "hooks" / "hooks.json").write_text('{"hooks": {}}')
    (up / ".mcp.json").write_text('{"mcpServers": {}}')


def _marketplace(up, plugins):
    (up / ".claude-plugin").mkdir()
    (up / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": plugins}))


def test_a_plugin_installs_its_skills_and_lists_its_hooks_separately(env):
    _plugin(env["up"])
    code, out = run("github:acme/kit@main", yes=False)
    assert code == 0
    assert out.index("Skills (installed)") < out.index("Hooks and MCP servers (listed, not enabled)")
    held = out[out.index("Hooks and MCP servers"):out.index("Runs code")]
    assert "hooks/hooks.json" in held and ".mcp.json" in held and "review" not in held
    assert (env["lib"] / "review" / "SKILL.md").is_file()
    assert json.loads((env["lib"] / ".pl-sources.json").read_text())["review"]["plugin"] == "kit"
    kept = env["lib"] / ".pl-plugins" / f"kit@{SHA[:7]}"
    assert (kept / "hooks" / "hooks.json").is_file() and (kept / "skills" / "review" / "SKILL.md").is_file()
    assert [i["name"] for i in skills.scan() if i["account"] == skills.LIBRARY] == ["review"]


def test_a_marketplace_entry_needs_plugin_and_lists_the_choices(env, capsys):
    _marketplace(env["up"], [{"name": "kit", "source": "./"}, {"name": "extra", "source": "./extra"}])
    assert run("github:acme/kit@main", yes=True)[0] == 2
    err = capsys.readouterr().err
    assert "--plugin" in err and "extra" in err and "kit" in err
    assert not env["lib"].exists()
    assert run("github:acme/kit@main", yes=True, plugin="nope")[0] == 2
    assert "nope" in capsys.readouterr().err
    assert run("github:acme/kit@main", yes=True, plugin="kit")[0] == 0
    assert json.loads((env["lib"] / ".pl-sources.json").read_text())["review"]["plugin"] == "kit"


def test_a_marketplace_entry_in_another_github_repo_is_fetched_there(env):
    _marketplace(env["up"], [{"name": "kit", "source": {"source": "github", "repo": "other/tools"}}])
    assert run("github:acme/kit@main", yes=True, plugin="kit")[0] == 0
    assert ["git", "remote", "add", "origin", "https://github.com/other/tools"] in env["calls"]
    assert json.loads((env["lib"] / ".pl-sources.json").read_text())["review"]["repo"] == "other/tools"


def test_plugin_hooks_are_never_installed_into_an_account(env):
    _plugin(env["up"])
    run("github:acme/kit@main", yes=True)
    assert not (env["lib"] / "review" / "hooks").exists()
    assert [p.name for p in env["lib"].iterdir() if not p.name.startswith(".")] == ["review"]
    plugin = skills.plugin_dir()
    assert plugin is not None and not list(plugin.rglob("hooks.json")) and not list(plugin.rglob(".mcp.json"))
    assert not list((env["lib"].parent).glob(".claude*"))


def test_a_marketplace_source_off_github_exits_2(env, capsys):
    _marketplace(env["up"], [{"name": "kit", "source": {"source": "url", "url": "https://gitlab.com/acme/kit.git"}}])
    assert run("github:acme/kit@main", yes=True, plugin="kit")[0] == 2
    assert "GitHub" in capsys.readouterr().err
    assert not env["lib"].exists()
