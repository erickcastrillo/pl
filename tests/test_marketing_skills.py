"""Built-in marketing skills: shipped, wired to the marketing agents, free of vendors."""
import json
import re

import pytest
import yaml

from pl import agent_defs, cli, connectors, pods, skills
from pl import config as C

NAMES = ("pl-marketing-copy", "pl-marketing-report")
VENDORS = ("mailchimp", "hubspot", "hootsuite", "buffer", "google analytics", "linkedin")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, name="work"):
    d = home / f".pl-{name}"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load(name)
    return d


def _text(name):
    return (pods.skill_dir(name) / "SKILL.md").read_text()


def _front_matter(name):
    return yaml.safe_load(_text(name).split("---")[1])


def _show(capsys, monkeypatch, agent):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "work", "agents", "show", agent, "--json"])
    cli.main()
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("name", NAMES)
def test_marketing_skills_ship_with_name_description_and_version(name):
    assert pods.skill_dir(name) == pods.BUILTIN_DIR / "marketing" / "skills" / name
    meta = _front_matter(name)
    assert meta["name"] == name
    assert meta["description"] and "\n" not in meta["description"].strip()
    assert meta["pl-builtin-version"] == 1


def test_marketing_copy_steps_cover_channels_claims_section_and_no_publish():
    text = _text("pl-marketing-copy")
    for phrase in ("each channel", "length limit", "claim, price or offer", "assumption",
                   "pl section <id> <NAME> --from <file>", "Never publish or post", "data, never instructions"):
        assert phrase in text


def test_marketing_report_steps_cover_measure_missing_and_no_send():
    text = _text("pl-marketing-report")
    for phrase in ("success measure", "how it was counted", "by when", "missing", "Never guess",
                   "pl section <id> <NAME> --from <file>", "data, never instructions", "outside pl"):
        assert phrase in text


@pytest.mark.parametrize("agent,skill", [("marketing-copywriter", "pl-marketing-copy"),
                                         ("marketing-campaign-reporter", "pl-marketing-report")])
def test_agents_show_marketing_agents_list_their_skills(fake_home, capsys, monkeypatch, agent, skill):
    _profile(fake_home)
    assert skill in _show(capsys, monkeypatch, agent)["skills"]


def test_every_builtin_agent_skill_resolves(fake_home):
    _profile(fake_home)
    for agent in agent_defs.load().values():
        for s in agent["skills"]:
            assert (skills.BUILTIN_DIR / s / "SKILL.md").is_file() or pods.skill_dir(s), s


def test_marketing_pod_skill_list_is_unchanged(fake_home):
    _profile(fake_home)
    listed = pods.find("marketing")["skills"]
    assert listed == ["pl-stage"]
    assert not set(NAMES) & set(listed)


@pytest.mark.parametrize("name", NAMES)
def test_marketing_skills_have_no_pipeline_line_vendor_or_shell_true(name):
    text = _text(name)
    assert not any(line.startswith("# PIPELINE:") for line in text.splitlines())
    assert "shell=True" not in text
    named = re.findall(r"`([a-z]+)` connector", text)
    assert set(named) <= set(connectors.ROLES)
    assert not [v for v in VENDORS if v in text.lower()]
