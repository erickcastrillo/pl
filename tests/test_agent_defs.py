"""Named agents: built-in definitions, profile agents, pl agents list|show."""
import json

import pytest

from pl import agent_defs, cli, connectors
from pl import config as C

TEN = {"sales-sdr", "sales-proposal-writer", "sales-follow-up", "support-triage", "support-reply-drafter",
       "marketing-copywriter", "marketing-campaign-reporter", "engineering-spec", "engineering-plan", "engineering-run"}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, name="work", text=""):
    d = home / f".pl-{name}"
    d.mkdir()
    (d / "config.toml").write_text(text)
    C.load(name)
    return d


def _agent_md(profile, name, text):
    d = profile / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.md").write_text(text)


def _pl(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "work", *argv])
    return cli.main()


def test_fresh_profile_lists_the_ten_builtin_agents(fake_home):
    _profile(fake_home)
    agents = agent_defs.load()
    assert TEN <= set(agents)
    assert {a["source"] for a in agents.values()} == {"built-in"}


def test_every_builtin_has_every_field_and_approval_is_a_subset(fake_home):
    _profile(fake_home)
    for agent in agent_defs.load().values():
        row = agent_defs.describe(agent)
        assert set(row) == {"name", "source", "role", *agent_defs.FIELDS}
        assert row["role"]
        assert set(row["approval"]) <= set(agent_defs.APPROVAL)


def test_builtin_connector_roles_are_role_names_not_vendors(fake_home):
    _profile(fake_home)
    for agent in agent_defs.load().values():
        assert set(agent["connector_roles"]) <= set(connectors.ROLES)


def test_profile_agent_is_listed_with_source_profile(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "my-closer", "---\nskills: [pl-stage]\nconnector_roles: [email]\napproval: [send]\npod: sales\n---\nYou close deals.\n")
    agent = agent_defs.load()["my-closer"]
    assert agent["source"] == "profile"
    assert agent["instructions"] == "You close deals."
    assert agent["skills"] == ["pl-stage"] and agent["model"] is None


def test_profile_agent_overrides_builtin_of_same_name(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "sales-sdr", "---\nskills: []\n---\nMine.\n")
    rows = [a for a in agent_defs.load().values() if a["name"] == "sales-sdr"]
    assert len(rows) == 1 and rows[0]["source"] == "profile" and rows[0]["instructions"] == "Mine."


def test_bad_front_matter_agent_is_skipped_not_fatal(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "broken", "---\nskills: [unclosed\n---\nx\n")
    _agent_md(d, "no-front-matter", "just text\n")
    _agent_md(d, "Bad Name", "---\n---\nx\n")
    agents = agent_defs.load()
    assert TEN <= set(agents)
    assert not {"broken", "no-front-matter", "Bad Name"} & set(agents)


def test_agents_list_json_is_a_list_with_ac1_fields_exit_0(fake_home, capsys, monkeypatch):
    _profile(fake_home)
    assert _pl(monkeypatch, "agents", "list", "--json") in (0, None)
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)
    assert TEN <= {r["name"] for r in rows}
    assert all(set(r) == {"name", "source", "role", *agent_defs.FIELDS} for r in rows)


def test_agents_list_prints_one_line_per_agent(fake_home, capsys, monkeypatch):
    _profile(fake_home)
    _pl(monkeypatch, "agents", "list")
    lines = capsys.readouterr().out.splitlines()
    assert any(l.startswith("sales-sdr") and "built-in" in l and "sales SDR" in l for l in lines)


def test_agents_show_sales_sdr_prints_full_definition(fake_home, capsys, monkeypatch):
    _profile(fake_home)
    _pl(monkeypatch, "agents", "show", "sales-sdr", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "sales-sdr" and data["instructions"].startswith("You are a sales SDR")
    assert set(data["connector_roles"]) <= set(connectors.ROLES)
    _pl(monkeypatch, "agents", "show", "sales-sdr")
    out = capsys.readouterr().out
    assert out.startswith("You are a sales SDR")
    for label in ("skills:", "connector roles:", "model: stage default", "approval:", "pod: sales"):
        assert label in out


def test_agents_show_unknown_says_no_agent_named_and_exits_nonzero(fake_home, capsys, monkeypatch):
    _profile(fake_home)
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "show", "no-such-agent")
    assert e.value.code == 2
    assert "no agent named no-such-agent" in capsys.readouterr().err
