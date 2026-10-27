"""Pods: [pods.<name>] config, the built-in engineering pod, pl pods list|show."""
import argparse
import json
import re
import sys

import pytest
import tomlkit

from pl import cli, connectors, harnesses, pods, skills
from pl import config as C
from pl.util import BadInput


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
    assert capsys.readouterr().out.split() == ["engineering", "marketing", "sales"]
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="nope", json=False))
    assert e.value.code == 2
    assert "marketing" in capsys.readouterr().err


def test_list_json_includes_marketing_next_to_engineering(fake_home, capsys):
    _profile(fake_home, "work")
    pods.cmd_pods(argparse.Namespace(pods_cmd="list", json=True))
    assert json.loads(capsys.readouterr().out) == ["engineering", "marketing", "sales"]


def _marketing_sections():
    text = (pods.BUILTIN_DIR / "marketing" / "pod.md").read_text()
    return {h: body.splitlines() for h, body in re.findall(r"^## (\w+)\n(.*?)(?=^## |\Z)", text, re.S | re.M)}


def test_show_marketing_prints_its_path_and_three_role_sections(fake_home, capsys):
    _profile(fake_home, "work")
    pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="marketing", json=False))
    assert "pods_builtin/marketing/pod.md" in capsys.readouterr().out
    sections = _marketing_sections()
    assert list(sections) == ["Writer", "Editor", "Analyst"]
    assert all(0 < sum(ln.startswith("- ") for ln in lines) <= 5 for lines in sections.values())


def test_the_editor_flags_claims_and_prices_and_never_removes_them():
    rules = [ln for ln in _marketing_sections()["Editor"] if ln.startswith("- ")]
    assert sum(all(w in r for w in ("factual claim", "price or offer", "owner", "never remove or confirm"))
               for r in rules) == 1


def test_show_marketing_json_lists_only_pl_stage(fake_home, capsys):
    _profile(fake_home, "work")
    pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="marketing", json=True))
    skills_ = json.loads(capsys.readouterr().out)["skills"]
    assert skills_ == ["pl-stage"] and not set(skills_) & set(pods.ENGINEERING_SKILLS)


def _show_sales(capsys):
    pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="sales", json=True))
    return json.loads(capsys.readouterr().out)


def test_show_sales_has_a_package_pod_md_three_rules_its_skills_and_roles(fake_home, capsys):
    _profile(fake_home, "work")
    out = _show_sales(capsys)
    assert out["file"].startswith(str(pods.BUILTIN_DIR)) and len(out["rules"]) >= 3
    assert {"pl-sales-proposal", "pl-sales-follow-up"} <= set(out["skills"])
    assert out["roles"] == ["contacts", "email"]


def test_every_sales_skill_ships_and_every_role_is_a_connector_role(fake_home):
    pod = pods.find("sales")
    assert all((pods.skill_dir(s) / "SKILL.md").is_file() for s in pod["skills"] if s != "pl-stage")
    assert set(pod["roles"]) <= set(connectors.ROLES)


def test_a_pods_sales_table_overrides_file_and_skills(fake_home):
    _profile(fake_home, "work", '[pods.sales]\nskills = ["x"]\n')
    pod = pods.find("sales")
    assert pod["skills"] == ["x"] and pod["file"] == pods.BUILTIN_DIR / "sales" / "pod.md"
    assert pod["roles"] == ["contacts", "email"]


def test_a_missing_sales_pod_md_shows_no_pod_md(fake_home, capsys, monkeypatch, tmp_path):
    _profile(fake_home, "work")
    monkeypatch.setattr(pods, "BUILTIN_DIR", tmp_path / "empty")
    assert pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="sales", json=False)) == 0
    out = capsys.readouterr().out
    assert "no pod.md" in out and "roles: contacts, email" in out


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
    pods.save_version("billing", "# Sales\n", "start", "me")
    assert pods.cmd_pods(args(pods_cmd="rule", pod="billing", text="Never offer discounts")) == 0
    text = pods.pod_file("billing").read_text()
    assert "## Rules\n- Never offer discounts\n" in text
    assert len(pods.history("billing")) == 2


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
    assert isinstance(e.value, BadInput) and "--yes" in str(e.value)
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
    assert exc.value.code == 2   # cli.main prints the reason on stderr and exits 2
    assert "--yes" in capsys.readouterr().err

    # With --yes, it succeeds without a terminal
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "enable", "--plugin", "plug-gamma", "--pod", "engineering", "--hooks", "--yes"])
    cli.main()
    out = capsys.readouterr().out
    assert "echo pre-run" in out

    doc = tomlkit.parse(fake_env["config"].read_text())
    plugins = doc.get("pods", {}).get("engineering", {}).get("plugins", [])
    assert any(p.get("name") == "plug-gamma" and p.get("hooks") is True for p in plugins)


def _library_skill(fake_env, name):
    d = fake_env["library"] / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")


def _pod_with(fake_env, monkeypatch, skill_names):
    for n in ("in-pod", "outside"):
        _library_skill(fake_env, n)
    pod = {"name": "billing", "skills": skill_names, "plugins": []}
    monkeypatch.setattr(C, "PODS", {"billing": {"skills": skill_names}})
    monkeypatch.setattr(C, "POD", "billing")
    return pod


def test_a_skill_outside_the_pods_skills_is_not_in_the_launch_plugin(fake_env, monkeypatch):
    pod = _pod_with(fake_env, monkeypatch, ["in-pod"])
    claude = harnesses.get("claude")
    pdir = skills.launch_plugin(claude, pod=pod)
    assert not (pdir / "skills" / "outside").exists()
    assert skills.library_prompt(claude, None, "/outside x", pdir, pod=pod) == "/outside x"


def test_a_codex_launch_does_not_inline_a_skill_outside_the_pod(fake_env, monkeypatch):
    pod = _pod_with(fake_env, monkeypatch, ["in-pod"])
    codex = harnesses.get("codex")
    assert skills.library_prompt(codex, None, "/outside x", None, pod=pod) == "/outside x"
    assert not list((fake_env["state"] / "launch").glob("skill-outside-*"))


def test_a_pod_skill_is_present(fake_env, monkeypatch):
    pod = _pod_with(fake_env, monkeypatch, ["in-pod"])
    claude, codex = harnesses.get("claude"), harnesses.get("codex")
    pdir = skills.launch_plugin(claude, pod=pod)
    assert (pdir / "skills" / "in-pod" / "SKILL.md").is_file()
    assert skills.library_prompt(claude, None, "/in-pod x", pdir, pod=pod) == "/pl:in-pod x"
    assert "Follow the instructions in" in skills.library_prompt(codex, None, "/in-pod x", None, pod=pod)


def test_a_launch_script_of_a_configured_pod_leaves_out_other_skills(fake_env, monkeypatch):
    _pod_with(fake_env, monkeypatch, ["in-pod"])
    claude = harnesses.get("claude")
    script = harnesses.launch_script(claude, None, "/outside x", "sid", "label")
    assert "/pl:outside" not in script
    assert "/pl:in-pod" in harnesses.launch_script(claude, None, "/in-pod x", "sid", "label")


def test_a_profile_without_pods_keeps_the_whole_library(fake_env):
    _library_skill(fake_env, "outside")
    claude = harnesses.get("claude")
    assert "/pl:outside" in harnesses.launch_script(claude, None, "/outside x", "sid", "label")


def test_a_pod_table_without_skills_keeps_the_whole_library(fake_env, monkeypatch):
    _library_skill(fake_env, "outside")
    monkeypatch.setattr(C, "PODS", {"billing": {"knowledge": []}})
    monkeypatch.setattr(C, "POD", "billing")
    claude = harnesses.get("claude")
    assert pods.launch_pod() is None
    assert "/pl:outside" in harnesses.launch_script(claude, None, "/outside x", "sid", "label")


@pytest.mark.parametrize("built_in", ["sales", "marketing"])
def test_a_file_only_override_of_a_built_in_pod_keeps_its_launch_limit(fake_home, monkeypatch, built_in):
    monkeypatch.setattr(C, "PODS", {built_in: {"file": "x.md"}})
    monkeypatch.setattr(C, "POD", built_in)
    assert pods.launch_pod()["skills"] == pods.find(built_in)["skills"] != []


def _enable(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "enable", *argv])
    cli.main()


def test_enable_adds_the_skill_and_the_next_launch_has_it(fake_env, monkeypatch, capsys):
    _library_skill(fake_env, "migration-checklist")
    _enable(monkeypatch, "migration-checklist", "--pod", "engineering", "--yes")
    doc = tomlkit.parse(fake_env["config"].read_text())
    assert "migration-checklist" in doc["pods"]["engineering"]["skills"]
    assert "pl-run" in doc["pods"]["engineering"]["skills"]
    monkeypatch.setattr(C, "PODS", {"engineering": dict(doc["pods"]["engineering"])})
    claude = harnesses.get("claude")
    pdir = skills.launch_plugin(claude, pod=pods.current())
    assert (pdir / "skills" / "migration-checklist").exists()
    capsys.readouterr()
    _enable(monkeypatch, "migration-checklist", "--pod", "engineering", "--yes")
    assert "already enabled" in capsys.readouterr().out


def test_disable_removes_the_skill(fake_env, monkeypatch, capsys):
    _library_skill(fake_env, "migration-checklist")
    _enable(monkeypatch, "migration-checklist", "--pod", "engineering", "--yes")
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "disable", "migration-checklist", "--pod", "engineering", "--yes"])
    cli.main()
    doc = tomlkit.parse(fake_env["config"].read_text())
    assert "migration-checklist" not in doc["pods"]["engineering"]["skills"]


def test_enable_of_an_unknown_skill_exits_2(fake_env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _enable(monkeypatch, "no-such-skill", "--pod", "engineering", "--yes")
    assert exc.value.code == 2
    assert "no-such-skill" in capsys.readouterr().err


def test_show_json_includes_approval_rate_edits_per_draft_and_escalations(fake_home, capsys):
    from datetime import datetime, timedelta, timezone
    from pl import alerts
    d = _profile(fake_home, "sales", "[pods.sales]\nskills = []\n")
    now = datetime.now(timezone.utc)

    def at(days):
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    rows = [("approved", 1), ("spec_approved", 2), ("approved", 3), ("rejected", 4), ("approved", 40),
            ("moved", 5, {"to": "Manual"}), ("moved", 6, {"to": "Done"})]
    (d / "state" / "events.jsonl").write_text("".join(
        json.dumps({"kind": r[0], "card": "c1", "ts": at(r[1]), **(r[2] if len(r) > 2 else {})}) + "\n" for r in rows))
    (d / "state" / "pod-edits.jsonl").write_text("".join(
        json.dumps({"pod": "sales", "sentence_hash": h, "card": "c1", "ts": at(2)}) + "\n" for h in "abcd"))
    alerts.open("stage_failed:c1:run", "high", "failed", "pl retry")
    alerts.open("pod_rule:x", "info", "suggested", "pl pods rule accept x")

    pods.cmd_pods(argparse.Namespace(pods_cmd="show", name="sales", json=True))
    m = json.loads(capsys.readouterr().out)["metrics"]
    assert m == {"approval_rate": 0.75, "edits_per_draft": 4 / 3, "escalations": 2, "window_days": 30}


def test_a_marketing_launch_leaves_out_the_engineering_skills(fake_env, monkeypatch):
    _library_skill(fake_env, "pl-stage")
    _library_skill(fake_env, "pl-plan")
    monkeypatch.setattr(C, "POD", "marketing")
    monkeypatch.setattr(C, "WORK_DIR", str(fake_env["home"]))
    claude = harnesses.get("claude")
    assert "/pl:pl-plan" not in harnesses.launch_script(claude, None, "/pl-plan x", "sid", "label")
    assert "/pl:pl-stage" in harnesses.launch_script(claude, None, "/pl-stage x", "sid", "label")


def test_rule_add_then_rollback_of_the_builtin_marketing_pod(fake_env, capsys):
    shipped = (pods.BUILTIN_DIR / "marketing" / "pod.md").read_text()
    before = pods.rules(pods.find("marketing"))
    assert pods.cmd_pods(args(pods_cmd="rule", pod="marketing", text="Never promise results")) == 0
    after = pods.rules(pods.find("marketing"))
    assert after == before + ["Never promise results"]
    assert pods.cmd_pods(args(pods_cmd="rollback", pod="marketing", to=1)) == 0
    assert pods.rules(pods.find("marketing")) == before
    assert pods.pod_file("marketing").read_text() == shipped
    assert [v["n"] for v in pods.history("marketing")] == [1, 2, 3]


def test_rule_add_to_a_configured_pod_records_no_builtin_version(fake_env, monkeypatch):
    monkeypatch.setattr(C, "PODS", {"billing": {}})
    pods.cmd_pods(args(pods_cmd="rule", pod="billing", text="Never offer discounts"))
    assert [v["note"] for v in pods.history("billing")] == ["rule: Never offer discounts"]


def test_a_sales_pipeline_launch_links_only_the_sales_skills(fake_env, monkeypatch):
    for name in ("pl-spec", "pl-plan", "pl-run", "outside"):
        _library_skill(fake_env, name)
    monkeypatch.setattr(C, "POD", "sales")
    monkeypatch.setattr(C, "WORK_DIR", str(fake_env["home"]))
    claude = harnesses.get("claude")
    pdir = skills.launch_plugin(claude, pod=pods.launch_pod())
    assert sorted(p.name for p in (pdir / "skills").iterdir()) == ["pl-sales-follow-up", "pl-sales-proposal"]
    assert "/pl:pl-plan" not in harnesses.launch_script(claude, None, "/pl-plan x", "sid", "label")


def test_a_profile_named_sales_without_a_pipeline_pod_keeps_the_whole_library(fake_env, monkeypatch):
    _library_skill(fake_env, "outside")
    monkeypatch.setattr(C, "PROFILE_NAME", "sales")
    assert pods.launch_pod() is None
    assert "/pl:outside" in harnesses.launch_script(harnesses.get("claude"), None, "/outside x", "sid", "label")


def test_an_engineering_launch_is_unchanged(fake_env):
    _library_skill(fake_env, "outside")
    assert pods.launch_pod() is None
    assert "/pl:outside" in harnesses.launch_script(harnesses.get("claude"), None, "/outside x", "sid", "label")


def test_a_sales_launch_of_a_client_template_stage_keeps_pl_stage(fake_env, monkeypatch):
    _library_skill(fake_env, "pl-stage")
    monkeypatch.setattr(C, "POD", "sales")
    monkeypatch.setattr(C, "WORK_DIR", str(fake_env["home"]))
    assert "/pl:pl-stage" in harnesses.launch_script(harnesses.get("claude"), None, "/pl-stage x", "sid", "label")
