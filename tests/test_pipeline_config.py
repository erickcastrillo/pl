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


def test_engineering_template_equals_the_builtin_pipeline():
    assert pipeline.templates()["engineering"] == pipeline.engineering()


def test_every_template_passes_validation():
    templates = pipeline.templates()
    assert {"engineering", "new-client", "existing-client", "billing", "team", "marketing", "customer-support"} <= set(templates)
    for name, p in templates.items():
        if name != "engineering":   # engineering keeps its own code path, where design and plan share a column
            assert pipeline.errors(p) == [], name


def test_marketing_template_columns_and_stages():
    p = pipeline.templates()["marketing"]
    assert p["columns"] == ["Brief", "Brief review", "Draft", "Draft review", "Publish", "Publish review", "Report", "Done"]
    wanted = {
        "brief": ("Brief", "Brief review", "Draft", "BRIEF", ("audience", "success measure")),
        "draft": ("Draft", "Draft review", "Publish", "DRAFT", ("channel",)),
        "publish": ("Publish", "Publish review", "Report", "PUBLISH", ("approves",)),
        "report": ("Report", "Done", None, "REPORT", ("success measure",)),
    }
    assert list(p["stages"]) == list(wanted)
    for name, (column, done, nxt, section, words) in wanted.items():
        s = p["stages"][name]
        assert (s["column"], s["done"], s.get("next"), s["section"]) == (column, done, nxt, section), name
        assert s["skill"] == "pl-stage" and s["gate"] == "owner", name
        for w in words:
            assert w in s["instructions"].lower(), (name, w)


def test_marketing_only_publish_is_outward():
    p = pipeline.templates()["marketing"]
    assert [n for n, s in p["stages"].items() if pipeline._outward(s, {})] == ["publish"]
    p["stages"]["publish"]["gate"] = "none"
    errs = pipeline.errors(p)
    assert len([e for e in errs if pipeline.OUTWARD_REFUSAL in e]) == 1 and len(errs) == 1


def test_billing_outward_stages_are_gated():
    billing = pipeline.templates()["billing"]
    assert set(billing["stages"]) == {"prepare-invoice", "chase-overdue"}
    for name, stage in billing["stages"].items():
        assert stage["outward"] is True and stage["gate"] == "owner", name
        assert stage["connectors"] == [], name
    ungated = {**billing, "stages": {n: {**s, "gate": "none"} for n, s in billing["stages"].items()}}
    assert len(pipeline.errors(ungated)) == 2


def test_customer_support_columns_in_order():
    assert pipeline.templates()["customer-support"]["columns"] == [
        "Inbox", "Triage", "Reply draft", "Reply review", "Waiting on customer", "Resolved"]


def test_customer_support_stages_triage_and_reply():
    stages = pipeline.templates()["customer-support"]["stages"]
    assert set(stages) == {"triage", "reply"}
    for name, stage in stages.items():
        assert stage["skill"] == "pl-stage" and stage["instructions"], name
    triage, reply = stages["triage"], stages["reply"]
    assert (triage["column"], triage["done"], triage["gate"], triage["section"], triage["next"]) == (
        "Inbox", "Triage", "owner", "TRIAGE", "Reply draft")
    assert (reply["column"], reply["done"], reply["gate"], reply["section"], reply["next"]) == (
        "Reply draft", "Reply review", "owner", "REPLY", "Waiting on customer")
    assert reply["outward"] is True


def test_customer_support_ui_words_and_fields():
    ui = pipeline.templates()["customer-support"]["ui"]
    assert ui["item"] == "ticket" and ui["items"] == "tickets"
    assert {"customer", "priority", "response time"} <= set(ui["fields"])


def test_customer_support_ungated_reply_is_refused():
    p = pipeline.templates()["customer-support"]
    ungated = {**p, "stages": {**p["stages"], "reply": {**p["stages"]["reply"], "gate": "none"}}}
    assert pipeline.errors(ungated) == ["pipeline.stages.reply.gate: " + pipeline.OUTWARD_REFUSAL]


def test_other_templates_use_the_generic_stage_skill():
    stages = {n: set(pipeline.templates()[n]["stages"]) for n in ("existing-client", "team")}
    assert stages == {"existing-client": {"check-in", "renewal", "issue-triage"}, "team": {"hiring", "onboarding"}}
    for name in ("existing-client", "billing", "team"):
        assert {s["skill"] for s in pipeline.templates()[name]["stages"].values()} == {"pl-stage"}


UI = '''
[pipeline.ui]
{ui}
'''


def test_pipeline_ui_words_and_fields_are_read_from_the_template(fake_home):
    ui = 'item = "lead"\nitems = "leads"\nfields = ["company", "value", "next step"]'
    _profile(fake_home, PREPARE.format(column="Inbox", gate="owner", extra="") + UI.format(ui=ui))
    C.load("sales")
    assert pipeline.ui() == {"item": "lead", "items": "leads", "fields": ["company", "value", "next step"]}


def test_no_pipeline_ui_keeps_the_engineering_words(fake_home):
    words = {"item": "card", "items": "cards", "fields": []}
    _profile(fake_home, "")
    C.load("sales")
    assert pipeline.ui() == words
    (fake_home / ".pl-sales" / "config.toml").write_text(PREPARE.format(column="Inbox", gate="owner", extra=""))
    C.load("sales")
    assert pipeline.ui() == words


@pytest.mark.parametrize("ui", ['fields = "company"', 'item = ""', 'items = 3', 'fields = ["company", ""]', 'fields = ["owner", "owner"]'])
def test_bad_pipeline_ui_is_refused_with_its_key_path(fake_home, capsys, ui):
    _profile(fake_home, PREPARE.format(column="Inbox", gate="owner", extra="") + UI.format(ui=ui))
    with pytest.raises(SystemExit) as e:
        C.load("sales")
    assert e.value.code == 2
    assert "pipeline.ui." in capsys.readouterr().err
