"""Plan 10 slice 3: the table-driven dispatcher for config pipelines."""
import pytest

from pl import accounts, dispatch, pipeline
from pl import config as C

SALES = {"name": "sales", "template": "new-client", "columns": ["Inbox", "Working", "Review", "Done"], "stages": {
    "prepare": {"column": "Inbox", "done": "Review", "skill": "pl-stage", "section": "PROPOSAL", "gate": "owner"}}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _sales():
    C.PIPELINE, C.COLUMNS = SALES, SALES["columns"]
    C.STAGES = {"prepare": {}}


def _card(n, col, **extra):
    return {"id": f"o/r#{n}", "title": f"Card {n}", "list_id": col, "updated_at": f"2026-10-0{n}",
            "metadata": {"pipeline_mode": "auto"}, **extra}


def _pass(monkeypatch, cs):
    started = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append((c["id"], stage)))
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    monkeypatch.setattr(accounts, "lists", lambda: {})
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, max_prep=2, pull=False)
    return started


def test_a_config_pipeline_starts_the_stage_whose_column_the_card_is_in(monkeypatch):
    _sales()
    assert _pass(monkeypatch, [_card(1, "Inbox")]) == [("o/r#1", "prepare")]


def test_a_card_in_a_gate_column_starts_nothing(monkeypatch):
    _sales()
    assert pipeline.stage_at("Review") is None
    assert _pass(monkeypatch, [_card(1, "Review"), _card(2, "Working")]) == []


def test_a_config_pipeline_pass_touches_no_engineering_column(monkeypatch):
    _sales()
    _pass(monkeypatch, [_card(1, "Inbox"), _card(2, "Review"), _card(3, "Done")])


def test_complete_is_true_at_or_past_the_done_column():
    _sales()
    assert [pipeline.complete(col, "prepare") for col in SALES["columns"]] == [False, False, True, True]


def test_the_prompt_names_the_skill_card_and_stage():
    _sales()
    assert pipeline.prompt("prepare", "o/r#7") == "/pl-stage o/r#7 stage=prepare"


def test_a_stage_prompt_override_wins():
    _sales()
    C.PROMPTS = {**C.PROMPTS, "prepare": "/mine {id}"}
    assert pipeline.prompt("prepare", "o/r#7") == "/mine o/r#7"


def test_engineering_uses_the_existing_rules():
    c = {"id": "o/r#1", "tags": ["frontend"], "description": "", "metadata": {"spec_approved_at": "x"}}
    C.GATES = {"spec": True}
    implicit = [dispatch.stage_for(c, col) for col in ("Inbox", "Spec ready", "Approved", "PR open")]
    C.PIPELINE = {**pipeline.engineering(), "template": "engineering"}
    explicit = [dispatch.stage_for(c, col) for col in ("Inbox", "Spec ready", "Approved", "PR open")]
    assert implicit == explicit == ["spec", "design", "run", None]
    assert dispatch.stage_complete(c, "Plan for review", "plan")
