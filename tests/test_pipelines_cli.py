"""Plan 10 slice 5: pl pipelines list|show."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pl import config as C
from pl import pipeline


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


def test_list_templates_names_all_five(capsys):
    pipeline.cmd_pipelines(_args(pipelines_cmd="list", templates=True, json=True))
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert names == {"engineering", "new-client", "existing-client", "billing", "team"}
