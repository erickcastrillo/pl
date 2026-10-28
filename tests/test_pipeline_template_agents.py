"""Plan 61 slice 6: the built-in templates name default agents on their main stages."""
import json
from types import SimpleNamespace

import pytest

from pl import agent_defs
from pl import config as C
from pl import pipeline

EXPECTED = {
    "new-client": {"qualify": "sales-sdr", "proposal": "sales-proposal-writer", "onboarding": None},
    "existing-client": {"check-in": "sales-follow-up", "renewal": None, "issue-triage": None},
    "customer-support": {"triage": "support-triage", "reply": "support-reply-drafter"},
    "marketing": {"brief": None, "draft": "marketing-copywriter", "publish": None, "report": "marketing-campaign-reporter"},
}
CASES = [(t, s, want) for t, stages in EXPECTED.items() for s, want in stages.items()]


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
    return d


def _args(**kw):
    return SimpleNamespace(**{"pipelines_cmd": "show", "name": None, "json": False, "templates": False, **kw})


def _add_args(**kw):
    return _args(pipelines_cmd="add", name="sales", template="new-client", board="local", account=None, yes=True,
                 dry_run=False, **kw)


def _unknown_agents(templates):
    builtin = {n for n, a in agent_defs.load().items() if a["source"] == "built-in"}
    return [(t, s) for t, p in templates.items() for s, st in p["stages"].items()
            if "agent" in st and st["agent"] not in builtin]


@pytest.mark.parametrize("template,stage,want", CASES)
def test_template_default_agents(template, stage, want):
    assert pipeline.templates()[template]["stages"][stage].get("agent") == want


@pytest.mark.parametrize("template", EXPECTED)
def test_pipelines_show_json_names_the_agent_after_add(fake_home, capsys, template):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args(**{}) if template == "new-client" else
                           _args(pipelines_cmd="add", name="sales", template=template, board="local", account=None,
                                 yes=True, dry_run=False))
    capsys.readouterr()
    C.load("sales")
    pipeline.cmd_pipelines(_args(json=True))
    stages = json.loads(capsys.readouterr().out)["stages"]
    for stage, want in EXPECTED[template].items():
        if want is None:
            assert "agent" not in stages[stage]
        else:
            assert stages[stage]["agent"] == want


def test_every_template_agent_is_a_builtin_agent(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    templates = pipeline.templates()
    assert _unknown_agents(templates) == []
    for name, p in templates.items():
        if name != "engineering":  # its template already fails pipeline.errors on main
            assert pipeline.errors(p) == []


def test_a_misspelled_template_agent_is_reported(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    templates = pipeline.templates()
    templates["new-client"]["stages"]["qualify"]["agent"] = "no-such-agent"
    assert _unknown_agents(templates) == [("new-client", "qualify")]


def test_billing_team_engineering_templates_name_no_agent():
    templates = pipeline.templates()
    for name in ("billing", "team", "engineering"):
        assert all("agent" not in s for s in templates[name]["stages"].values())
    assert all("agent" not in s for s in pipeline.engineering()["stages"].values())


def test_existing_profile_config_is_byte_for_byte_unchanged(fake_home, capsys):
    text = '''
[pipeline]
name = "sales"
template = "new-client"
columns = ["Lead", "Qualify review"]
[pipeline.stages.qualify]
column = "Lead"
done = "Qualify review"
skill = "pl-stage"
section = "PROPOSAL"
gate = "owner"
'''
    d = _profile(fake_home, "sales", text)
    before = (d / "config.toml").read_bytes()
    C.load("sales")
    pipeline.cmd_pipelines(_args(json=True))
    stages = json.loads(capsys.readouterr().out)["stages"]
    assert (d / "config.toml").read_bytes() == before
    assert all("agent" not in s for s in stages.values())


def test_export_import_keeps_agent_keys(fake_home, capsys, tmp_path):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    capsys.readouterr()
    pipeline.cmd_pipelines(_args(pipelines_cmd="export", name="sales"))
    exported = tmp_path / "sales.toml"
    exported.write_text(capsys.readouterr().out)
    pipeline.cmd_pipelines(_args(pipelines_cmd="import", file=str(exported), name="sales2", board="local",
                                 account=None, dry_run=False, yes=True))
    C.load("sales2")
    stages = C.PIPELINE["stages"]
    assert stages["qualify"]["agent"] == "sales-sdr"
    assert stages["proposal"]["agent"] == "sales-proposal-writer"
