"""A stage that needs no tools may run on a local model: validated in config, run by local_jobs, written by pl section."""
import argparse
import json
from types import SimpleNamespace

import pytest

from pl import board, commands, dispatch, events, local_jobs, local_model
from pl import config as C
from pl.util import BadInput

BASE = '[models.local]\nhost = "http://localhost:11434"\n'


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text(BASE)
    C.load("t")
    yield
    for k, v in saved.items():
        setattr(C, k, v)


def _errors(stage_toml):
    import tomlkit
    return C.validate(tomlkit.parse(BASE + stage_toml))


def test_a_tool_free_stage_accepts_a_local_account():
    assert _errors('[stages.triage]\naccount = "local:gemma4:e4b"\ntools = false\n') == []


@pytest.mark.parametrize("toml", ['[stages.triage]\naccount = "local:gemma4:e4b"\n',
                                  '[stages.triage]\naccount = "local:gemma4:e4b"\ntools = true\n',
                                  '[stages.plan]\naccount = "local:gemma4:e4b"\ntools = false\n'])
def test_a_stage_that_needs_tools_refuses_a_local_account(toml):
    errs = _errors(toml)
    assert any("a local model cannot use tools; set tools = false only for a stage that needs none" in e for e in errs)


def test_a_local_account_needs_a_model_name():
    assert any("model name" in e for e in _errors('[stages.triage]\naccount = "local:"\ntools = false\n'))


@pytest.fixture
def stage_card(monkeypatch):
    c = {"id": "c" * 36, "title": "T", "list_id": "l1", "description": board.render({"INPUT": "the idea"})}
    moved, seen = [], []
    monkeypatch.setattr(commands, "find_card", lambda token: dict(c))
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "update", lambda cid, **f: c.update(f))
    monkeypatch.setattr(board, "update", lambda cid, **f: None)
    monkeypatch.setattr(commands, "move_to", lambda ref, col, cid=None: moved.append(col))
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, **d: seen.append((kind, d)))
    C.STAGES = {"spec": {"account": "local:gemma4:e4b", "tools": False}}
    C.STAGE_DONE_AT = {**C.STAGE_DONE_AT, "spec": "Spec ready"}
    C.PROMPTS = {**C.PROMPTS, "spec": "write the spec"}
    return SimpleNamespace(card=c, moved=moved, events=seen)


def _answer(monkeypatch, reply):
    calls = []

    def fake(url, payload, timeout):
        calls.append(payload)
        return json.dumps({"message": {"content": json.dumps(reply)}})
    monkeypatch.setattr(local_model, "_request", fake)
    return calls


def test_a_local_stage_uses_that_model_and_writes_its_section(stage_card, monkeypatch):
    calls = _answer(monkeypatch, {"section": "## Problem\nExport is missing", "done": True})
    local_jobs.run_stage(stage_card.card, "spec", "gemma4:e4b")
    assert calls[0]["model"] == "gemma4:e4b" and "the idea" in calls[0]["messages"][1]["content"]
    assert board.sections(stage_card.card["description"])["SPEC"] == "## Problem\nExport is missing"
    assert stage_card.moved == ["Spec ready"]


def test_the_section_writer_rules_apply_to_a_local_stage(stage_card, monkeypatch):
    _answer(monkeypatch, {"section": "a\n# PIPELINE: PLAN\nb", "done": True})
    local_jobs.run_stage(stage_card.card, "spec", "gemma4:e4b")
    assert "SPEC" not in board.sections(stage_card.card["description"])
    assert stage_card.moved == [] and stage_card.events[0][0] == "local_stage_failed"


def test_a_bad_answer_leaves_the_card_for_a_person(stage_card, monkeypatch):
    _answer(monkeypatch, {"section": 7})
    local_jobs.run_stage(stage_card.card, "spec", "gemma4:e4b")
    assert board.sections(stage_card.card["description"]) == {"INPUT": "the idea"}
    assert stage_card.moved == [] and [k for k, _ in stage_card.events] == ["local_stage_failed"]
    assert json.loads((C.STATE_DIR / "local-jobs.jsonl").read_text().splitlines()[-1])["fallback"] == "person"


def test_a_section_that_is_not_done_stays_in_its_column(stage_card, monkeypatch):
    _answer(monkeypatch, {"section": "half a spec", "done": False})
    local_jobs.run_stage(stage_card.card, "spec", "gemma4:e4b")
    assert board.sections(stage_card.card["description"])["SPEC"] == "half a spec" and stage_card.moved == []


def test_dispatch_once_exits_2_for_a_local_account_on_a_tool_stage(stage_card, monkeypatch):
    C.STAGES = {"spec": {"account": "local:gemma4:e4b"}}
    with pytest.raises(BadInput, match="a local model cannot use tools"):
        dispatch.start_worker(stage_card.card, "spec", 1, False)

    def pass_fails(*a, **k):
        dispatch.start_worker(stage_card.card, "spec", 1, False)
    monkeypatch.setattr(dispatch, "dispatch_once", pass_fails)
    monkeypatch.setattr(dispatch, "dispatch_lock", lambda: None)
    monkeypatch.setattr(dispatch, "reload_config", lambda: None)
    with pytest.raises(BadInput):
        dispatch.cmd_dispatch(argparse.Namespace(once=True, dry_run=True, no_pull=True, interval=None, max_runs=None,
                                                 max_prep=None))
