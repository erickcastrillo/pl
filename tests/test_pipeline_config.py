"""Plan 10 slice 1: [pipeline] loading and validation."""
import tomlkit

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


def _profile(home, text):
    d = home / ".pl-sales"
    d.mkdir()
    (d / "config.toml").write_text(text)
    return d


PREPARE = '''
[pipeline]
columns = ["Inbox", "Working", "Review", "Done"]
[pipeline.stages.prepare]
column = "{column}"
done = "Review"
skill = "pl-stage"
section = "PROPOSAL"
gate = "{gate}"
{extra}
'''


def test_no_pipeline_section_is_the_engineering_pipeline_from_todays_constants(fake_home):
    _profile(fake_home, "")
    C.load("sales")
    p = C.PIPELINE
    assert p["name"] == "engineering" and p["template"] == "engineering"
    assert p["columns"] == C.COLUMNS
    assert p["stages"]["spec"] == {"column": "Inbox", "done": "Spec ready", "skill": "pl-spec", "section": "SPEC",
                                   "gate": "none"}
    assert p["stages"]["plan"]["column"] == "Spec ready" and p["stages"]["plan"]["done"] == "Plan for review"
    assert p["stages"]["plan"]["gate"] == "owner"
    assert p["stages"]["run"] == {"column": "Approved", "done": "PR open", "skill": "pl-run", "gate": "auto-merge"}


def test_a_stage_naming_a_missing_column_exits_2_and_names_the_key_path(fake_home, capsys):
    d = _profile(fake_home, PREPARE.format(column="Nowhere", gate="owner", extra=""))
    with pytest.raises(SystemExit) as e:
        C.load("sales")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"pl: {d / 'config.toml'}: pipeline.stages.prepare.column" in err


def test_a_config_pipeline_replaces_the_columns(fake_home):
    _profile(fake_home, PREPARE.format(column="Inbox", gate="owner", extra=""))
    C.load("sales")
    assert C.COLUMNS == ["Inbox", "Working", "Review", "Done"] == C.PIPELINE["columns"]
    assert C.PIPELINE["stages"]["prepare"]["section"] == "PROPOSAL"


def test_an_outward_stage_with_gate_none_is_refused(fake_home, capsys):
    _profile(fake_home, PREPARE.format(column="Inbox", gate="none", extra="outward = true"))
    with pytest.raises(SystemExit) as e:
        C.load("sales")
    assert e.value.code == 2
    assert "pipeline.stages.prepare.gate: a stage that writes or sends outward needs a gate" in capsys.readouterr().err


def test_a_read_write_connector_stage_with_gate_none_is_refused(fake_home, capsys):
    text = PREPARE.format(column="Inbox", gate="none", extra='connectors = ["mail"]') + \
        '\n[connectors.mail]\naccess = "read-write"\n'
    _profile(fake_home, text)
    with pytest.raises(SystemExit) as e:
        C.load("sales")
    assert e.value.code == 2
    assert "pipeline.stages.prepare.gate: a stage that writes or sends outward needs a gate" in capsys.readouterr().err


def test_a_read_only_connector_stage_with_gate_none_is_fine(fake_home):
    text = PREPARE.format(column="Inbox", gate="none", extra='connectors = ["mail"]') + \
        '\n[connectors.mail]\naccess = "read"\n'
    _profile(fake_home, text)
    C.load("sales")
    assert C.PIPELINE["stages"]["prepare"]["gate"] == "none"


def test_bad_gate_section_and_shared_column_are_refused():
    p = {"columns": ["A", "B", "C"], "stages": {
        "one": {"column": "A", "done": "B", "gate": "maybe", "section": "input"},
        "two": {"column": "A", "done": "C", "next": "Z", "section": "REVIEW NOTES"}}}
    errs = pipeline.errors(p)
    assert any(e.startswith("pipeline.stages.one.gate") for e in errs)
    assert any(e.startswith("pipeline.stages.one.section") for e in errs)
    assert any(e.startswith("pipeline.stages.two.section") for e in errs)
    assert any(e.startswith("pipeline.stages.two.next") for e in errs)
    assert any(e.startswith("pipeline.stages.two.column") and "one" in e for e in errs)


def test_validate_returns_the_same_errors_for_the_settings_screen(fake_home):
    doc = tomlkit.parse(PREPARE.format(column="Nowhere", gate="owner", extra=""))
    errs = C.validate(doc)
    assert any(e.startswith("pipeline.stages.prepare.column") for e in errs)
    assert not any("pipeline" in e for e in C.validate(tomlkit.parse("")))
