"""Plan 10 slice 5: pl pipelines list|show."""
import json
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest
import tomlkit

from pl import config as C
from pl import pipeline, profiles


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


NEW_CLIENT_CONFIG = '''
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


def test_show_json_prints_columns_stages_gates(fake_home, capsys):
    _profile(fake_home, "sales", NEW_CLIENT_CONFIG)
    C.load("sales")
    pipeline.cmd_pipelines(_args(json=True))
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"name", "template", "columns", "stages", "gates"}
    assert out["name"] == "sales" and out["template"] == "new-client"
    assert out["columns"] == ["Lead", "Qualify review"]
    assert out["stages"]["qualify"]["section"] == "PROPOSAL"
    assert out["gates"] == {"qualify": "owner"}


def test_show_text_names_the_stage_skill_section_and_gate(fake_home, capsys):
    _profile(fake_home, "sales", NEW_CLIENT_CONFIG)
    C.load("sales")
    pipeline.cmd_pipelines(_args())
    out = capsys.readouterr().out
    assert "Lead" in out and "qualify" in out and "pl-stage" in out and "PROPOSAL" in out and "owner" in out


def test_show_another_profile_runs_it_as_a_subprocess_and_loads_nothing(fake_home, capsys, monkeypatch):
    _profile(fake_home, "sales", NEW_CLIENT_CONFIG)
    _profile(fake_home, "dev")
    C.load("dev")
    calls = []
    shown = {"name": "sales", "template": "new-client", "columns": ["Lead"], "stages": {}, "gates": {}}

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(shown), "")

    monkeypatch.setattr(pipeline, "_run", fake_run)
    pipeline.cmd_pipelines(_args(name="sales", json=True))
    assert calls == [[sys.executable, "-m", "pl", "--profile", "sales", "pipelines", "show", "--json"]]
    assert json.loads(capsys.readouterr().out) == shown
    assert C.PROFILE_NAME == "dev"


def test_list_templates_json(capsys):
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", templates=True, json=True))
    rows = {r["name"]: r["stages"] for r in json.loads(capsys.readouterr().out)}
    assert rows["new-client"] == 3 and "engineering" in rows


def test_list_reads_the_pipeline_name_and_summary_without_loading_the_profile(fake_home, capsys):
    d = _profile(fake_home, "sales", NEW_CLIENT_CONFIG)
    (d / "state").mkdir()
    (d / "state" / "summary.json").write_text(json.dumps({"running": True, "at": 1}))
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", json=True))
    rows = {r["profile"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["sales"]["pipeline"] == "sales" and rows["sales"]["summary"]["running"] is True
    assert rows["dev"]["pipeline"] == "engineering" and rows["dev"]["summary"] is None


# ---------- Plan 10 slice 6: add, export, import ----------

def _add_args(**kw):
    return _args(pipelines_cmd="add", name="sales", template="new-client", board="local", account=None, yes=True,
                 dry_run=False, **kw)


def _shown(name):
    C.load(name)
    return pipeline.shown(C.PIPELINE)


def test_add_sales_from_new_client_creates_a_profile_with_the_templates_pipeline(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    template = pipeline.templates()["new-client"]
    assert _shown("sales") == pipeline.shown({**template, "name": "sales"})
    assert C.TRACKER["type"] == "local" and "code_host" not in (fake_home / ".pl-sales" / "config.toml").read_text()


def test_add_support_from_customer_support_copies_columns_stages_gates_and_ui(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(pipelines_cmd="add", name="support", template="customer-support", board="local",
                                 account=None, yes=True, dry_run=False))
    template = pipeline.templates()["customer-support"]
    assert _shown("support") == pipeline.shown({**template, "name": "support"})
    assert C.PIPELINE["ui"] == template["ui"]


def test_add_existing_is_unchanged_and_exits_0(fake_home, capsys):
    d = _profile(fake_home, "sales", "# mine\n")
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    assert "already exists, unchanged" in capsys.readouterr().out
    assert (d / "config.toml").read_text() == "# mine\n"


def test_add_without_yes_and_no_tty_exits_2(fake_home, capsys, monkeypatch):
    _profile(fake_home, "dev")
    C.load("dev")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "yes": False}))
    assert e.value.code == 2 and "--yes" in capsys.readouterr().err
    assert not (fake_home / ".pl-sales").exists()


def test_add_unknown_template_exits_2(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "template": "nope"}))
    assert e.value.code == 2


def test_add_mkt_from_marketing_template(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "name": "mkt", "template": "marketing"}))
    capsys.readouterr()
    pipeline.cmd_pipelines(_args(pipelines_cmd="show", name="mkt", json=True))
    out = json.loads(capsys.readouterr().out)
    want = pipeline.shown({**pipeline.templates()["marketing"], "name": "mkt"})
    assert (out["columns"], out["stages"], out["gates"]) == (want["columns"], want["stages"], want["gates"])
    assert set(out["gates"].values()) == {"owner"} and len(out["gates"]) == 4


def test_add_unknown_template_marketin(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "name": "mkt", "template": "marketin"}))
    assert e.value.code == 2 and "pl pipelines list --templates" in capsys.readouterr().err


def test_add_dry_run_writes_nothing(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "dry_run": True}))
    assert "[pipeline]" in capsys.readouterr().out
    assert not (fake_home / ".pl-sales").exists()


def test_add_copies_the_accounts_of_the_current_profile(fake_home):
    _profile(fake_home, "dev", '[accounts.main]\nconfig_dir = "~/.claude"\n')
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    C.load("sales")
    assert list(C.ACCOUNTS) == ["main"]


def test_add_with_accounts_writes_only_those_accounts(fake_home):
    _profile(fake_home, "dev", '[accounts.main]\nconfig_dir = "~/.claude"\n[accounts.alt]\nconfig_dir = "~/.claude-alt"\n')
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "accounts": ["alt"]}))
    C.load("sales")
    assert list(C.ACCOUNTS) == ["alt"]
    assert {s.get("account") for s in C.PIPELINE["stages"].values()} == {"alt"}


def test_add_with_an_accounts_dict_writes_those_settings_even_if_not_in_this_profile(fake_home):
    _profile(fake_home, "dev", '[accounts.main]\nconfig_dir = "~/.claude"\n')
    C.load("dev")
    codex = {"harness": "codex", "config_dir": "~/.codex-x", "model": "gpt-x", "effort": "high"}
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "accounts": {"codex1": codex}}))
    C.load("sales")
    assert C.ACCOUNTS == {"codex1": codex}
    assert {s.get("account") for s in C.PIPELINE["stages"].values()} == {"codex1"}


def test_add_with_an_unknown_account_in_accounts_exits_2(fake_home):
    _profile(fake_home, "dev", '[accounts.main]\nconfig_dir = "~/.claude"\n')
    C.load("dev")
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "accounts": ["ghost"]}))
    assert e.value.code == 2
    assert not (fake_home / ".pl-sales").exists()


def test_add_without_accounts_still_copies_every_account(fake_home):
    _profile(fake_home, "dev", '[accounts.main]\nconfig_dir = "~/.claude"\n[accounts.alt]\nconfig_dir = "~/.claude-alt"\n')
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    C.load("sales")
    assert list(C.ACCOUNTS) == ["main", "alt"]


def test_export_then_import_gives_the_same_columns_stages_gates_and_skills(fake_home, capsys, tmp_path):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    capsys.readouterr()
    pipeline.cmd_pipelines(_args(pipelines_cmd="export", name="sales"))
    exported = tmp_path / "sales.toml"
    exported.write_text(capsys.readouterr().out)
    pipeline.cmd_pipelines(_args(pipelines_cmd="import", file=str(exported), name="sales2", board="local", account=None, dry_run=False, yes=True))
    again = _shown("sales2")
    first = _shown("sales")
    assert {**again, "name": "sales"} == first


def test_export_has_no_tracker_or_secrets(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    capsys.readouterr()
    pipeline.cmd_pipelines(_args(pipelines_cmd="export", name="sales"))
    out = capsys.readouterr().out
    assert out.startswith("[pipeline]") and "tracker" not in out


def test_import_of_an_invalid_file_exits_2_naming_the_key(fake_home, capsys, tmp_path):
    _profile(fake_home, "dev")
    C.load("dev")
    bad = tmp_path / "bad.toml"
    bad.write_text(NEW_CLIENT_CONFIG.replace('column = "Lead"', 'column = "Nowhere"'))
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_args(pipelines_cmd="import", file=str(bad), name="x", board="local", yes=True))
    assert e.value.code == 2 and "pipeline.stages.qualify.column" in capsys.readouterr().err
    assert not (fake_home / ".pl-x").exists()


def test_list_templates_names_all_six(capsys):
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", templates=True, json=True))
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows} == {"engineering", "new-client", "existing-client", "billing", "team",
                                         "marketing", "customer-support"}
    assert next(r for r in rows if r["name"] == "customer-support")["stages"] == 2


def test_list_templates_marketing_has_four_stages(capsys):
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", templates=True, json=True))
    assert {"name": "marketing", "stages": 4, "pod": None} in json.loads(capsys.readouterr().out)


# ---------- Plan 35 slice 2: add takes a ready tracker table ----------

GH_TRACKER = {"type": "github-project", "owner": "acme", "number": 7, "status_field": "pl stage", "repo": "acme/web"}


def test_add_with_a_tracker_table_writes_it_as_is(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "dry_run": True, "tracker": GH_TRACKER}))
    assert 'type = "github-project"' in capsys.readouterr().out
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "tracker": GH_TRACKER}))
    written = tomllib.loads((fake_home / ".pl-sales" / "config.toml").read_text())
    assert written["tracker"] == GH_TRACKER


def test_add_with_board_mcp_still_writes_type_mcp(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "board": "mcp:boards"}))
    written = tomllib.loads((fake_home / ".pl-sales" / "config.toml").read_text())
    assert written["tracker"] == {"type": "mcp", "server": "boards"}


def test_a_template_pod_is_written_to_the_new_pipeline(fake_home, monkeypatch):
    from pl import pods
    _profile(fake_home, "dev")
    C.load("dev")
    fake = {**pipeline.templates()["new-client"], "name": "marketing", "pod": "marketing"}
    monkeypatch.setattr(pipeline, "templates", lambda: {"marketing": fake})
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "name": "mkt", "template": "marketing"}))
    assert tomllib.loads((fake_home / ".pl-mkt" / "config.toml").read_text())["pipeline"]["pod"] == "marketing"
    C.load("mkt")
    assert pods.current()["name"] == "marketing"


def test_an_engineering_template_writes_no_pod_key(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "template": "engineering"}))
    assert "pod" not in tomllib.loads((fake_home / ".pl-sales" / "config.toml").read_text())["pipeline"]


# ---------- Plan 44 slice 9: templates choose their pod ----------

def _import_args(path, name, **kw):
    return _args(pipelines_cmd="import", file=str(path), name=name, board="local", account=None, dry_run=False,
                 yes=True, **kw)


def test_client_templates_carry_the_sales_pod_and_engineering_none(fake_home, capsys):
    t = pipeline.templates()
    assert t["new-client"]["pod"] == "sales" and t["existing-client"]["pod"] == "sales"
    assert t["engineering"].get("pod") is None
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", templates=True, json=True))
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["new-client"]["pod"] == rows["existing-client"]["pod"] == "sales"
    assert rows["engineering"]["pod"] is None


@pytest.mark.parametrize("template", ["new-client", "existing-client"])
def test_add_from_a_client_template_writes_pod_sales(fake_home, template):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "template": template}))
    assert tomllib.loads((fake_home / ".pl-sales" / "config.toml").read_text())["pipeline"]["pod"] == "sales"


def test_add_dry_run_prints_pod_sales_and_creates_nothing(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "yes": False, "dry_run": True}))
    assert tomllib.loads(capsys.readouterr().out)["pipeline"]["pod"] == "sales"
    assert not (fake_home / ".pl-sales").exists()


def test_import_of_a_support_template_writes_pod_support(fake_home, tmp_path):
    _profile(fake_home, "dev")
    C.load("dev")
    f = tmp_path / "support.toml"
    f.write_text(NEW_CLIENT_CONFIG.replace('name = "sales"', 'name = "support"\npod = "support"'))
    pipeline.cmd_pipelines(_import_args(f, "helpdesk"))
    assert tomllib.loads((fake_home / ".pl-helpdesk" / "config.toml").read_text())["pipeline"]["pod"] == "support"


def test_add_engineering_writes_the_same_config_as_before(fake_home):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_args(**{**vars(_add_args()), "name": "eng2", "template": "engineering"}))
    text = (fake_home / ".pl-eng2" / "config.toml").read_text()
    template = {k: v for k, v in pipeline.templates()["engineering"].items() if k != "pod"}
    expected = tomlkit.dumps(profiles.pipeline_config({**template, "name": "eng2"}, {"type": "local"}, C.ACCOUNTS))
    assert text == expected and "pod" not in text


def test_a_template_without_pod_writes_no_pod_key(fake_home, tmp_path):
    _profile(fake_home, "dev")
    C.load("dev")
    f = tmp_path / "t.toml"
    f.write_text(NEW_CLIENT_CONFIG)
    pipeline.cmd_pipelines(_import_args(f, "x"))
    assert "pod" not in tomllib.loads((fake_home / ".pl-x" / "config.toml").read_text())["pipeline"]


def test_export_keeps_pod_sales(fake_home, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    pipeline.cmd_pipelines(_add_args())
    capsys.readouterr()
    pipeline.cmd_pipelines(_args(pipelines_cmd="export", name="sales"))
    assert tomllib.loads(capsys.readouterr().out)["pipeline"]["pod"] == "sales"


def test_import_of_a_non_string_pod_exits_2(fake_home, tmp_path, capsys):
    _profile(fake_home, "dev")
    C.load("dev")
    f = tmp_path / "bad.toml"
    f.write_text(NEW_CLIENT_CONFIG.replace('name = "sales"', 'name = "sales"\npod = 3'))
    with pytest.raises(SystemExit) as e:
        pipeline.cmd_pipelines(_import_args(f, "x"))
    assert e.value.code == 2 and "pl: pipeline.pod:" in capsys.readouterr().err
    assert not (fake_home / ".pl-x").exists()


def test_show_prints_stage_agent_text_and_json(fake_home, capsys):
    _profile(fake_home, "sales", NEW_CLIENT_CONFIG + 'agent = "marketing-copywriter"\n')
    C.load("sales")
    pipeline.cmd_pipelines(_args())
    assert "agent marketing-copywriter" in capsys.readouterr().out
    pipeline.cmd_pipelines(_args(json=True))
    assert json.loads(capsys.readouterr().out)["stages"]["qualify"]["agent"] == "marketing-copywriter"


def test_show_stage_without_agent_is_unchanged(fake_home, capsys):
    _profile(fake_home, "sales", NEW_CLIENT_CONFIG)
    C.load("sales")
    pipeline.cmd_pipelines(_args())
    assert "  qualify: Lead → Qualify review; skill pl-stage, section PROPOSAL, gate owner\n" in capsys.readouterr().out
    pipeline.cmd_pipelines(_args(json=True))
    assert json.loads(capsys.readouterr().out)["stages"]["qualify"] == {
        "column": "Lead", "done": "Qualify review", "skill": "pl-stage", "section": "PROPOSAL", "gate": "owner"}
