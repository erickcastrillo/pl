"""Named agents: built-in definitions, profile agents, pl agents list|show."""
import difflib
import io
import json
import subprocess

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


def test_profile_override_of_a_builtin_keeps_the_builtin_approval(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "sales-sdr", "---\napproval: []\n---\nMine.\n")
    builtin = agent_defs._builtin()["sales-sdr"]["approval"]
    assert "send" in builtin
    assert agent_defs.load()["sales-sdr"]["approval"] == sorted(builtin)


def test_bad_front_matter_agent_is_skipped_not_fatal(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "broken", "---\nskills: [unclosed\n---\nx\n")
    _agent_md(d, "no-front-matter", "just text\n")
    _agent_md(d, "Bad Name", "---\n---\nx\n")
    agents = agent_defs.load()
    assert TEN <= set(agents)
    assert not {"broken", "no-front-matter", "Bad Name"} & set(agents)


@pytest.mark.parametrize("front", [
    "approval: send",
    "approval: [1]",
    "approval: [mail-everyone]",
    "skills: [1, 2]",
    "skills: sales",
    "connector_roles: [email, 3]",
    "connector_roles: email",
    "model: [opus]",
    "pod: 7",
])
def test_badly_typed_profile_agent_is_skipped(fake_home, front):
    d = _profile(fake_home)
    _agent_md(d, "odd", f"---\n{front}\n---\nx\n")
    agents = agent_defs.load()
    assert TEN <= set(agents)
    assert "odd" not in agents


def test_well_typed_profile_agent_with_null_model_and_pod_is_kept(fake_home):
    d = _profile(fake_home)
    _agent_md(d, "ok", "---\nskills: [a]\napproval: [send]\nmodel: null\n---\nx\n")
    assert agent_defs.load()["ok"]["approval"] == ["send"]


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


@pytest.fixture
def known(monkeypatch):
    monkeypatch.setattr("pl.skills.builtin_names", lambda: ["pl-stage"])
    monkeypatch.setattr("pl.skills.scan", lambda: [{"name": "mine"}])
    monkeypatch.setattr("pl.harnesses.models", lambda name: ["opus"])


def _md(skills="[]", roles="[]", model="null", approval="[send]", extra=""):
    return (f"---\nskills: {skills}\nconnector_roles: {roles}\nmodel: {model}\napproval: {approval}\npod: null\n{extra}---\nDo it.\n")


def test_validate_reports_unknown_skill_role_and_model(fake_home, known):
    _profile(fake_home)
    got = agent_defs.validate("my-closer", {"skills": ["pl-stage", "nope"], "connector_roles": ["crm"], "model": "gpt-x"})
    assert got == ["my-closer: unknown skill nope", "my-closer: unknown connector role crm",
                   "my-closer: unknown model or account gpt-x"]


def test_validate_refuses_unknown_approval_action(fake_home, known):
    _profile(fake_home)
    assert agent_defs.validate("my-closer", {"approval": ["wire"]}) == ["my-closer: unknown approval action wire"]


def test_validate_override_cannot_drop_builtin_approval(fake_home, known):
    _profile(fake_home)
    got = agent_defs.validate("sales-sdr", {"approval": []})
    assert got == ["sales-sdr: approval cannot drop send from built-in sales-sdr"]


def test_validate_literal_secret_is_reported_without_value(fake_home, known):
    _profile(fake_home)
    d = {"connector_settings": {"api_token": "s3cr3t-value", "api_key": "${KEY}"}}
    got = agent_defs.validate("my-closer", d)
    assert got == ["my-closer: connector_settings.api_token must use a ${VAR} reference, not a literal value"]
    assert "s3cr3t" not in got[0]


def test_validate_unreadable_model_list_reports_nothing(fake_home, known, monkeypatch):
    _profile(fake_home, text='[accounts.a]\nharness = "codex"\n')
    monkeypatch.setattr("pl.harnesses.models", lambda name: [])
    assert agent_defs.validate("my-closer", {"model": "anything"}) == []


def test_show_json_lists_problems(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md(roles="[crm]"))
    _pl(monkeypatch, "agents", "show", "my-closer", "--json")
    assert json.loads(capsys.readouterr().out)["problems"] == ["my-closer: unknown connector role crm"]


def test_show_text_prints_problems(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md(roles="[crm]"))
    _pl(monkeypatch, "agents", "show", "my-closer")
    assert "problems:\n  my-closer: unknown connector role crm" in capsys.readouterr().out


def test_add_creates_placeholder_definition(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    _pl(monkeypatch, "agents", "add", "my-closer")
    front, _ = agent_defs._parse((p / "agents/my-closer/agent.md").read_text())
    assert set(front) == set(agent_defs.FIELDS)
    _pl(monkeypatch, "agents", "list", "--json")
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out.splitlines()[-1])}
    assert rows["my-closer"]["source"] == "profile"


def test_add_existing_refuses_and_keeps_file(fake_home, known, monkeypatch):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md())
    before = (p / "agents/my-closer/agent.md").read_text()
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "add", "my-closer")
    assert e.value.code == 1
    assert (p / "agents/my-closer/agent.md").read_text() == before


def test_add_builtin_copies_builtin(fake_home, known, monkeypatch):
    _profile(fake_home)
    monkeypatch.setattr("pl.skills.builtin_names", lambda: ["pl-stage", "pl-sales-proposal", "pl-sales-follow-up", "pl-support-triage"])
    _pl(monkeypatch, "agents", "add", "sales-sdr")
    agent = agent_defs.load()["sales-sdr"]
    assert agent["source"] == "profile"
    assert agent["approval"] == agent_defs._builtin()["sales-sdr"]["approval"]
    assert agent["connector_roles"] == ["contacts", "email"]


@pytest.mark.parametrize("name", ["", "a/b", "..", "a b"])
def test_add_invalid_name_creates_nothing(fake_home, known, monkeypatch, name):
    p = _profile(fake_home)
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "add", name)
    assert e.value.code == 2
    assert not (p / "agents").exists()


def test_add_refuses_invalid_definition(fake_home, known, monkeypatch):
    p = _profile(fake_home)
    monkeypatch.setattr(agent_defs, "_builtin", lambda: {"bad": {"instructions": "x", "connector_roles": ["crm"], "approval": []}})
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "add", "bad")
    assert e.value.code == 1
    assert not (p / "agents/bad").exists()


def test_remove_yes_deletes_folder(fake_home, known, monkeypatch):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md())
    _pl(monkeypatch, "agents", "remove", "my-closer", "--yes")
    assert not (p / "agents/my-closer").exists()


def test_remove_answer_no_keeps_folder(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md())
    monkeypatch.setattr("pl.util.ask", lambda cmd, q, flag="--yes": "n")
    _pl(monkeypatch, "agents", "remove", "my-closer")
    assert (p / "agents/my-closer").is_dir()
    assert "nothing changed" in capsys.readouterr().out


def test_remove_without_tty_or_yes_refuses(fake_home, known, monkeypatch):
    p = _profile(fake_home)
    _agent_md(p, "my-closer", _md())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "remove", "my-closer")
    assert e.value.code == 1
    assert (p / "agents/my-closer").is_dir()


def test_remove_builtin_without_override_refuses(fake_home, known, monkeypatch, capsys):
    _profile(fake_home)
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "agents", "remove", "sales-sdr", "--yes")
    assert e.value.code == 1
    assert "built-in agents cannot be removed" in capsys.readouterr().err


def test_remove_override_restores_builtin(fake_home, known, monkeypatch):
    p = _profile(fake_home)
    _agent_md(p, "sales-sdr", _md(approval="[send]"))
    _pl(monkeypatch, "agents", "remove", "sales-sdr", "--yes")
    assert agent_defs.load()["sales-sdr"]["source"] == "built-in"


MKT_CONFIG = '''# marketing profile
[pipeline]
name = "marketing"
columns = ["Brief", "Draft review"]

[pipeline.stages.brief]
column = "Brief"
done = "Draft review"
skill = "pl-stage"

# the writing stage
[pipeline.stages.draft]
column = "Draft review"
done = "Draft review"
skill = "pl-stage"
'''


def _use(monkeypatch, capsys, *argv):
    code = None
    try:
        _pl(monkeypatch, "agents", "use", *argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_use_sets_agent_and_changes_nothing_else(fake_home, monkeypatch, capsys):
    d = _profile(fake_home, "work", MKT_CONFIG)
    code, out, _ = _use(monkeypatch, capsys, "draft", "marketing-copywriter", "--pipeline", "marketing")
    assert code is None
    new = (d / "config.toml").read_text()
    changed = [ln for ln in difflib.ndiff(MKT_CONFIG.splitlines(), new.splitlines()) if ln[0] in "+-"]
    assert changed == ['+ agent = "marketing-copywriter"']
    assert new.index("[pipeline.stages.draft]") < new.index("agent =")
    assert out.strip() == "stage draft of pipeline marketing now uses agent marketing-copywriter"


def test_use_replaces_existing_agent_once(fake_home, monkeypatch, capsys):
    before = MKT_CONFIG + 'agent = "sales-sdr"\n'
    d = _profile(fake_home, "work", before)
    _use(monkeypatch, capsys, "draft", "marketing-copywriter")
    new = (d / "config.toml").read_text()
    assert new.count("agent =") == 1
    assert new == before.replace("sales-sdr", "marketing-copywriter")


def test_use_unknown_pipeline_refuses_and_keeps_file(fake_home, monkeypatch, capsys):
    d = _profile(fake_home, "work", MKT_CONFIG)
    code, _, err = _use(monkeypatch, capsys, "draft", "marketing-copywriter", "--pipeline", "nope")
    assert code == 1 and "no pipeline named nope" in err
    assert (d / "config.toml").read_text() == MKT_CONFIG


def test_use_unknown_stage_refuses_and_keeps_file(fake_home, monkeypatch, capsys):
    d = _profile(fake_home, "work", MKT_CONFIG)
    code, _, err = _use(monkeypatch, capsys, "ship", "marketing-copywriter", "--pipeline", "marketing")
    assert code == 1 and "pipeline marketing has no stage ship" in err
    assert (d / "config.toml").read_text() == MKT_CONFIG


def test_use_unknown_agent_refuses_with_show_message(fake_home, monkeypatch, capsys):
    d = _profile(fake_home, "work", MKT_CONFIG)
    try:
        _pl(monkeypatch, "agents", "show", "nope")
    except SystemExit as e:
        show_code = e.code
    show_err = capsys.readouterr().err
    code, _, err = _use(monkeypatch, capsys, "draft", "nope")
    assert (code, err) == (show_code, show_err) and "no agent named nope" in err
    assert (d / "config.toml").read_text() == MKT_CONFIG


def test_use_json_prints_pipeline_stage_agent(fake_home, monkeypatch, capsys):
    _profile(fake_home, "work", MKT_CONFIG)
    _, out, _ = _use(monkeypatch, capsys, "draft", "marketing-copywriter", "--json")
    assert json.loads(out) == {"pipeline": "marketing", "stage": "draft", "agent": "marketing-copywriter"}


def test_use_other_profile_by_pipeline_name(fake_home, monkeypatch, capsys):
    work = _profile(fake_home, "work", MKT_CONFIG)
    mkt = fake_home / ".pl-mkt"
    mkt.mkdir()
    (mkt / "config.toml").write_text(MKT_CONFIG.replace('name = "marketing"', 'name = "mkt"'))
    code, _, _ = _use(monkeypatch, capsys, "draft", "marketing-copywriter", "--pipeline", "mkt")
    assert code is None
    assert 'agent = "marketing-copywriter"' in (mkt / "config.toml").read_text()
    assert (work / "config.toml").read_text() == MKT_CONFIG


def test_use_on_the_engineering_pipeline_has_no_stage_to_write(fake_home, monkeypatch, capsys):
    _profile(fake_home, "work", "")
    code, _, err = _use(monkeypatch, capsys, "run", "engineering-run")
    assert code == 1 and "pipeline engineering has no stage run" in err


def _hold_profile(fake_home, agent):
    return _profile(fake_home, "work", MKT_CONFIG + f'agent = "{agent}"\n')


@pytest.mark.parametrize("var", [("PL_SOURCE", "mcp"), ("PL_ASSISTANT", "1")])
def test_use_from_an_agent_session_cannot_drop_an_approval(fake_home, monkeypatch, capsys, var):
    d = _hold_profile(fake_home, "sales-proposal-writer")
    before = (d / "config.toml").read_text()
    monkeypatch.setenv(*var)
    code, _, err = _use(monkeypatch, capsys, "draft", "support-triage")
    assert code == 1 and "cannot drop" in err and "pl agents use draft support-triage" in err
    assert (d / "config.toml").read_text() == before


def test_use_from_an_agent_session_may_keep_or_add_approval(fake_home, monkeypatch, capsys):
    d = _hold_profile(fake_home, "support-triage")
    monkeypatch.setenv("PL_SOURCE", "mcp")
    code, _, _ = _use(monkeypatch, capsys, "draft", "sales-proposal-writer")
    assert code is None and 'agent = "sales-proposal-writer"' in (d / "config.toml").read_text()


def test_use_from_the_terminal_may_drop_an_approval(fake_home, monkeypatch, capsys):
    d = _hold_profile(fake_home, "sales-proposal-writer")
    monkeypatch.delenv("PL_SOURCE", raising=False)
    monkeypatch.delenv("PL_ASSISTANT", raising=False)
    code, _, _ = _use(monkeypatch, capsys, "draft", "support-triage")
    assert code is None and 'agent = "support-triage"' in (d / "config.toml").read_text()


def _no_editor(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an editor was started")
    monkeypatch.setattr(agent_defs, "_run", boom)


def _fake_editor(monkeypatch, text=None, code=0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if text is not None:
            open(argv[-1], "w").write(text)
        return subprocess.CompletedProcess(argv, code)
    monkeypatch.setattr(agent_defs, "_run", run)
    return calls


def _edit(monkeypatch, capsys, *argv):
    try:
        _pl(monkeypatch, "agents", "edit", *argv)
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


ACCOUNT = '[accounts.a]\nharness = "codex"\n'


def _closer(fake_home, **kw):
    p = _profile(fake_home, text=ACCOUNT)
    _agent_md(p, "my-closer", _md(skills="[pl-stage, mine]", model="opus", **kw))
    return p, p / "agents/my-closer/agent.md"


def test_edit_json_flags_set_fields_and_print_show_json(fake_home, known, monkeypatch, capsys):
    _closer(fake_home)
    _no_editor(monkeypatch)
    code, out, _ = _edit(monkeypatch, capsys, "my-closer", "--json", "--skills", "pl-stage,mine", "--connector-roles", "email,contacts",
                         "--model", "opus", "--approval", "send,publish", "--pod", "sales", "--instructions", "Close the deal.")
    assert code == 0
    _pl(monkeypatch, "agents", "show", "my-closer", "--json")
    shown = capsys.readouterr().out
    assert out == shown
    got = json.loads(shown)
    assert (got["skills"], got["connector_roles"], got["model"], got["approval"], got["pod"], got["instructions"]) == \
        (["pl-stage", "mine"], ["email", "contacts"], "opus", ["send", "publish"], "sales", "Close the deal.")


def test_edit_one_flag_keeps_other_fields_and_body(fake_home, known, monkeypatch, capsys):
    _closer(fake_home)
    _no_editor(monkeypatch)
    monkeypatch.setattr("pl.harnesses.models", lambda name: ["opus", "sonnet"])
    _edit(monkeypatch, capsys, "my-closer", "--json", "--model", "sonnet")
    got = agent_defs.load()["my-closer"]
    assert got["model"] == "sonnet" and got["skills"] == ["pl-stage", "mine"]
    assert got["instructions"] == "Do it." and got["approval"] == ["send"]


def test_edit_list_flag_replaces_and_empty_clears(fake_home, known, monkeypatch, capsys):
    _closer(fake_home)
    _no_editor(monkeypatch)
    _edit(monkeypatch, capsys, "my-closer", "--json", "--skills", "mine")
    assert agent_defs.load()["my-closer"]["skills"] == ["mine"]
    _edit(monkeypatch, capsys, "my-closer", "--json", "--skills", "")
    assert agent_defs.load()["my-closer"]["skills"] == []


@pytest.mark.parametrize("agent,flags,problem", [
    ("my-closer", ["--skills", "nope"], "my-closer: unknown skill nope"),
    ("my-closer", ["--connector-roles", "crm"], "my-closer: unknown connector role crm"),
    ("my-closer", ["--model", "gpt-x"], "my-closer: unknown model or account gpt-x"),
    ("my-closer", ["--approval", "wire"], "my-closer: unknown approval action wire"),
    ("sales-sdr", ["--approval", "publish"], "sales-sdr: approval cannot drop send from built-in sales-sdr"),
])
def test_edit_invalid_flags_write_nothing(fake_home, known, monkeypatch, capsys, agent, flags, problem):
    p, path = _closer(fake_home)
    _agent_md(p, "sales-sdr", "---\nskills: [pl-stage]\napproval: [send]\n---\nMine.\n")
    target = p / "agents" / agent / "agent.md"
    before = target.read_bytes()
    _no_editor(monkeypatch)
    code, out, err = _edit(monkeypatch, capsys, agent, "--json", *flags)
    assert code == 1 and out == "" and problem in err
    assert target.read_bytes() == before


def test_edit_flags_without_json_print_human_show(fake_home, known, monkeypatch, capsys):
    _closer(fake_home)
    _no_editor(monkeypatch)
    code, out, _ = _edit(monkeypatch, capsys, "my-closer", "--pod", "sales")
    assert code == 0
    _pl(monkeypatch, "agents", "show", "my-closer")
    assert out == capsys.readouterr().out and "pod: sales" in out


def test_edit_builtin_creates_override_with_edit(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home, text=ACCOUNT)
    _no_editor(monkeypatch)
    code, _, _ = _edit(monkeypatch, capsys, "sales-sdr", "--json", "--pod", "sales")
    assert code == 0 and (p / "agents/sales-sdr/agent.md").is_file()
    _pl(monkeypatch, "agents", "list", "--json")
    rows = [r for r in json.loads(capsys.readouterr().out) if r["name"] == "sales-sdr"]
    assert len(rows) == 1 and rows[0]["source"] == "profile" and rows[0]["pod"] == "sales"
    assert agent_defs.load()["sales-sdr"]["connector_roles"] == ["contacts", "email"]


def test_edit_builtin_invalid_leaves_no_override(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    _no_editor(monkeypatch)
    code, _, _ = _edit(monkeypatch, capsys, "sales-sdr", "--json", "--skills", "nope")
    assert code == 1 and not (p / "agents/sales-sdr").exists()
    assert agent_defs.load()["sales-sdr"]["source"] == "built-in"


@pytest.mark.parametrize("flags", [[], ["--json", "--model", "opus"]])
def test_edit_unknown_agent_creates_nothing_and_opens_no_editor(fake_home, known, monkeypatch, capsys, flags):
    p = _profile(fake_home)
    monkeypatch.setenv("EDITOR", "vi")
    _no_editor(monkeypatch)
    code, _, err = _edit(monkeypatch, capsys, "no-such-agent", *flags)
    assert code == 2 and "no agent named no-such-agent" in err
    assert not (p / "agents").exists()


def test_edit_instructions_dash_reads_stdin(fake_home, known, monkeypatch, capsys):
    _closer(fake_home)
    _no_editor(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin.\n"))
    _edit(monkeypatch, capsys, "my-closer", "--json", "--instructions", "-")
    assert agent_defs.load()["my-closer"]["instructions"] == "From stdin."


def test_edit_in_editor_saves_valid_text(fake_home, known, monkeypatch, capsys):
    _, path = _closer(fake_home)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "myed --wait")
    new = _md(skills="[mine]", model="opus")
    calls = _fake_editor(monkeypatch, new)
    code, _, _ = _edit(monkeypatch, capsys, "my-closer")
    assert code == 0 and path.read_text() == new
    assert calls[0][:2] == ["myed", "--wait"] and len(calls) == 1


def test_edit_in_editor_invalid_keeps_file_byte_for_byte(fake_home, known, monkeypatch, capsys):
    _, path = _closer(fake_home)
    before = path.read_bytes()
    monkeypatch.setenv("VISUAL", "myed")
    _fake_editor(monkeypatch, _md(roles="[crm]"))
    code, _, err = _edit(monkeypatch, capsys, "my-closer")
    assert code == 1 and "my-closer: unknown connector role crm" in err
    assert path.read_bytes() == before


def test_edit_no_editor_tells_to_use_json(fake_home, known, monkeypatch, capsys):
    _, path = _closer(fake_home)
    before = path.read_bytes()
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    _no_editor(monkeypatch)
    code, _, err = _edit(monkeypatch, capsys, "my-closer")
    assert code == 2 and "no editor set" in err and "--json" in err
    assert path.read_bytes() == before


def test_edit_editor_nonzero_exit_cancels(fake_home, known, monkeypatch, capsys):
    _, path = _closer(fake_home)
    before = path.read_bytes()
    monkeypatch.setenv("EDITOR", "myed")
    _fake_editor(monkeypatch, "garbage", code=1)
    code, _, err = _edit(monkeypatch, capsys, "my-closer")
    assert code == 1 and "edit cancelled" in err and path.read_bytes() == before


def test_edit_builtin_in_editor_invalid_leaves_no_override(fake_home, known, monkeypatch, capsys):
    p = _profile(fake_home)
    monkeypatch.setenv("EDITOR", "myed")
    _fake_editor(monkeypatch, _md(skills="[nope]"))
    code, _, _ = _edit(monkeypatch, capsys, "sales-sdr")
    assert code == 1 and not (p / "agents/sales-sdr").exists()
    assert agent_defs.load()["sales-sdr"]["source"] == "built-in"

