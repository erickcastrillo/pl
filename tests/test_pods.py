import json
import sys
import pytest
import tomlkit

from pl import config as C
from pl import cli, harnesses, skills


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    prof = home / ".pl-test"
    prof.mkdir()
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof))
    cfg = prof / "config.toml"
    cfg.write_text('[tracker]\ntype = "github-project"\nowner = "org"\nboard_id = "b1"\n')
    state = prof / "state"
    state.mkdir()
    lib = home / ".local" / "share" / "pl" / "skills"
    lib.mkdir(parents=True)
    monkeypatch.setattr(C, "CONFIG_DIR", prof)
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "SKILLS", {"library": str(lib)})
    monkeypatch.setattr(C, "PODS", {})
    monkeypatch.setattr(C, "POD", None)
    return {"home": home, "profile": prof, "library": lib, "state": state, "config": cfg}


def test_a_plugin_with_hooks_false_has_its_skills_and_no_hooks(fake_env):
    lib = fake_env["library"]
    plug_dir = lib / ".pl-plugins" / "plug-alpha@1a2b3c4"
    plug_dir.mkdir(parents=True)
    skill_dir = plug_dir / "skills" / "alpha-tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: alpha-tool\ndescription: Plugin skill\n---\n# alpha\n")
    (plug_dir / "hooks.json").write_text(json.dumps({"hooks": [{"command": "echo dangerous"}]}))
    (plug_dir / "settings.json").write_text(json.dumps({"permission": "all"}))
    (plug_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))

    pod = {"name": "testpod", "skills": [], "plugins": [{"name": "plug-alpha", "hooks": False}]}

    pdir = skills.plugin_dir(pod)
    assert pdir is not None
    assert (pdir / "skills" / "alpha-tool" / "SKILL.md").is_file()

    # No hook, setting, or MCP files are copied or linked
    assert not (pdir / "hooks.json").exists()
    assert not (pdir / "hooks").exists()
    assert not (pdir / "settings.json").exists()
    assert not (pdir / ".mcp.json").exists()

    # Launch does not add the plugin directory
    claude = harnesses.get("claude")
    lp = skills.launch_plugin(claude, plugin=True, pod=pod)
    argv = skills.with_plugin(["claude", "run"], lp)
    assert str(plug_dir) not in argv
    assert str(pdir) in argv


def test_hooks_true_adds_the_plugin_folder_on_claude_only(fake_env):
    lib = fake_env["library"]
    plug_dir = lib / ".pl-plugins" / "plug-beta@abcdef1"
    plug_dir.mkdir(parents=True)
    skill_dir = plug_dir / "skills" / "beta-tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: beta-tool\ndescription: Beta skill\n---\n# beta\n")

    pod = {"name": "testpod", "skills": [], "plugins": [{"name": "plug-beta", "hooks": True}]}

    claude = harnesses.get("claude")
    lp_claude = skills.launch_plugin(claude, plugin=True, pod=pod)
    argv_claude = skills.with_plugin(["claude", "run"], lp_claude)
    assert str(plug_dir) in argv_claude

    for other_name in ("codex", "antigravity"):
        other = harnesses.get(other_name)
        lp_other = skills.launch_plugin(other, plugin=True, pod=pod)
        argv_other = skills.with_plugin([other_name, "run"], lp_other)
        assert str(plug_dir) not in argv_other
        assert "--plugin-dir" not in argv_other


def test_enabling_hooks_without_a_terminal_needs_yes(fake_env, monkeypatch, capsys):
    lib = fake_env["library"]
    plug_dir = lib / ".pl-plugins" / "plug-gamma@fedcba9"
    plug_dir.mkdir(parents=True)
    (plug_dir / "hooks").mkdir(parents=True)
    (plug_dir / "hooks" / "hooks.json").write_text(json.dumps({"hooks": [{"command": "echo pre-run"}]}))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "enable", "--plugin", "plug-gamma", "--pod", "engineering", "--hooks"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "yes" in str(exc.value).lower()

    # With --yes, it succeeds without a terminal
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "enable", "--plugin", "plug-gamma", "--pod", "engineering", "--hooks", "--yes"])
    cli.main()
    out = capsys.readouterr().out
    assert "echo pre-run" in out

    doc = tomlkit.parse(fake_env["config"].read_text())
    plugins = doc.get("pods", {}).get("engineering", {}).get("plugins", [])
    assert any(p.get("name") == "plug-gamma" and p.get("hooks") is True for p in plugins)
