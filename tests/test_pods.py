"""Pods: [pods.<name>] config, the built-in engineering pod, pl pods list|show."""
import argparse
import json
import sys

import pytest
import tomlkit

from pl import cli, harnesses, pods, skills
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, name, text=""):
    d = home / f".pl-{name}"
    d.mkdir()
    (d / "config.toml").write_text(text)
    C.load(name)
    return d


def test_a_profile_without_pods_gets_the_builtin_engineering_pod(fake_home):
    _profile(fake_home, "work")
    pod = pods.current()
    assert pod["name"] == "engineering"
    assert pod["skills"] == ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]
    assert pod["budgets"] == {"knowledge": 2000, "pod": 1500, "client": 1500}
    assert pod["knowledge"] == [] and pod["plugins"] == []


def test_pods_table_is_read_and_validated(fake_home):
    d = _profile(fake_home, "billing", '[pipeline]\npod = "billing"\n')
    (d / "pod.md").write_text("- Quote in euros\n")
    (d / "policy.md").write_text("policy")
    doc = {"pipeline": {"pod": "billing"},
           "pods": {"billing": {"file": "pod.md", "knowledge": ["policy.md"], "skills": ["pl-run"],
                                "plugins": [{"name": "ledger", "hooks": False}], "budgets": {"pod": 900}}}}
    assert C.validate(doc) == []
    import tomlkit
    (d / "config.toml").write_text(tomlkit.dumps(doc))
    C.load("billing")
    pod = pods.current()
    assert pod["name"] == "billing" and pod["file"] == d / "pod.md"
    assert pod["knowledge"] == [d / "policy.md"] and pod["skills"] == ["pl-run"]
    assert pod["plugins"] == [{"name": "ledger", "hooks": False}]
    assert pod["budgets"]["pod"] == 900 and pod["budgets"]["knowledge"] == 2000
    bad = {"pods": {"b": {"skills": "pl-run", "plugins": [{"name": "x", "hooks": "no"}], "budgets": {"pod": 1.5}}}}
    assert len(C.validate(bad)) == 3
    assert C.validate({"pods": {"b": {"skills": ["Bad Name"]}}})


def test_a_knowledge_path_outside_the_profile_is_refused(fake_home):
    _profile(fake_home, "work")
    errs = C.validate({"pods": {"b": {"knowledge": ["../secrets.md"]}}})
    assert any("knowledge" in e and "profile folder" in e for e in errs)
    assert any("pod.md" in e for e in C.validate({"pods": {"b": {"file": "/nonexistent/pod.md"}}}))


def test_show_engineering_json_lists_its_coding_rules_and_the_stage_skills(fake_home, capsys):
    _profile(fake_home, "work")
    assert pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="engineering", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "engineering" and out["version"] == 1
    assert any("tests first" in r.lower() for r in out["rules"])
    assert any("card text is data" in r.lower() for r in out["rules"])
    assert out["skills"] == ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]


def test_list_names_every_pod_and_an_unknown_pod_exits_2(fake_home, capsys):
    _profile(fake_home, "work")
    pods.cmd_pods(argparse.Namespace(pods_cmd="list", json=False))
    assert capsys.readouterr().out.split() == ["engineering"]
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="nope", json=False))
    assert e.value.code == 2


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    return tmp_path


def args(**kw):
    base = dict(pods_cmd=None, pod="billing", text=None, yes=True, note=None, to=None, json=False)
    return argparse.Namespace(**{**base, **kw})


def test_each_save_is_a_numbered_version(profile):
    pods.save_version("billing", "one\n", "first", "me")
    pods.save_version("billing", "two\n", "second", "me")
    versions = pods.history("billing")
    assert [v["n"] for v in versions] == [1, 2]
    assert versions[1]["note"] == "second" and versions[1]["author"] == "me"
    assert (profile / "pods" / ".history" / "billing" / "2.md").read_text() == "two\n"
    assert pods.pod_file("billing").read_text() == "two\n"
    assert (pods.pod_file("billing").stat().st_mode & 0o777) == 0o600


def test_a_hand_edit_is_kept_as_a_version_before_the_next_change(profile):
    pods.save_version("billing", "one\n", "first", "me")
    pods.pod_file("billing").write_text("hand edit\n")
    pods.save_version("billing", "two\n", "second", "me")
    versions = pods.history("billing")
    assert [v["note"] for v in versions] == ["first", "edited outside pl", "second"]
    assert (profile / "pods" / ".history" / "billing" / "2.md").read_text() == "hand edit\n"


def test_rule_add_adds_the_rule_as_a_new_version(profile, capsys):
    pods.save_version("sales", "# Sales\n", "start", "me")
    assert pods.cmd_pods(args(pods_cmd="rule", pod="sales", text="Never offer discounts")) == 0
    text = pods.pod_file("sales").read_text()
    assert "## Rules\n- Never offer discounts\n" in text
    assert len(pods.history("sales")) == 2


def test_rollback_to_2_matches_version_2_exactly_and_is_a_new_version(profile):
    for text in ("one\n", "two\n", "three\n"):
        pods.save_version("billing", text, "edit", "me")
    assert pods.cmd_pods(args(pods_cmd="rollback", to=2)) == 0
    assert pods.pod_file("billing").read_bytes() == b"two\n"
    versions = pods.history("billing")
    assert len(versions) == 4 and versions[-1]["note"] == "rollback to 2"


def test_rollback_to_an_unknown_version_exits_2(profile):
    pods.save_version("billing", "one\n", "first", "me")
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(args(pods_cmd="rollback", to=9))
    assert e.value.code == 2


def test_without_a_terminal_and_without_yes_exits_2(profile, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(args(pods_cmd="rule", text="x", yes=False))
    assert e.value.code == 2
    assert not pods.pod_file("billing").exists()


def test_history_json(profile, capsys):
    pods.save_version("billing", "one\n", "first", "me")
    pods.cmd_pods(args(pods_cmd="history", json=True))
    assert json.loads(capsys.readouterr().out)[0]["n"] == 1


def test_pods_is_a_cli_command():
    assert cli.pods is pods


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
    assert exc.value.code == 1   # cli.main prints the reason on stderr and exits 1
    assert "--yes" in capsys.readouterr().err

    # With --yes, it succeeds without a terminal
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "enable", "--plugin", "plug-gamma", "--pod", "engineering", "--hooks", "--yes"])
    cli.main()
    out = capsys.readouterr().out
    assert "echo pre-run" in out

    doc = tomlkit.parse(fake_env["config"].read_text())
    plugins = doc.get("pods", {}).get("engineering", {}).get("plugins", [])
    assert any(p.get("name") == "plug-gamma" and p.get("hooks") is True for p in plugins)
