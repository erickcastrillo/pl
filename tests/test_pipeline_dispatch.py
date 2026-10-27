"""Plan 10 slices 3 and 4: the table-driven dispatcher for config pipelines, stage sections and owner gates."""
import argparse

import pytest

from pl import accounts, board, commands, dispatch, pipeline
from pl import config as C

SALES = {"name": "sales", "template": "new-client", "columns": ["Inbox", "Working", "Review", "Done"], "stages": {
    "prepare": {"column": "Inbox", "done": "Review", "skill": "pl-stage", "section": "PROPOSAL", "gate": "owner",
                "next": "Done"}}}


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


def test_a_pipeline_table_without_stages_is_still_engineering():
    C.PIPELINE = pipeline.from_toml({"pod": "engineering"})
    assert not pipeline.is_config()


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


# ---------- slice 4: stage sections and owner gates ----------

@pytest.fixture
def store(monkeypatch):
    c = {"id": "o/r#1", "title": "Card 1", "list_id": "Inbox", "updated_at": "2026-10-01",
         "description": board.render({"INPUT": "the idea"}), "metadata": {"pipeline_mode": "auto", "worker": {"pane": "%1"}}}

    def update(cid, **f):
        meta = f.pop("metadata", None) or {}
        c.update(f)
        c["metadata"].update(meta)

    monkeypatch.setattr(commands, "find_card", lambda token: dict(c))
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "col_id", lambda name: name)
    monkeypatch.setattr(commands, "update", update)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    monkeypatch.setattr(commands.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(commands.audit, "append", lambda *a, **k: None)
    monkeypatch.setattr(commands.recall, "note", lambda *a, **k: None)
    return c


def _write(card_id, name, text, tmp_path):
    f = tmp_path / "s.md"
    f.write_text(text)
    commands.cmd_section(argparse.Namespace(id=card_id, name=name, from_=str(f)))


def test_section_from_accepts_the_stages_section_and_refuses_others(store, tmp_path):
    _sales()
    _write("o/r#1", "PROPOSAL", "the offer", tmp_path)
    assert board.sections(store["description"])["PROPOSAL"] == "the offer"
    with pytest.raises(SystemExit, match="only PROPOSAL can be written"):
        _write("o/r#1", "SPEC", "x", tmp_path)
    C.PIPELINE = {**pipeline.engineering(), "template": "engineering"}
    with pytest.raises(SystemExit, match="only SPEC, DESIGN or PLAN can be written"):
        _write("o/r#1", "PROPOSAL", "x", tmp_path)


def test_a_section_past_its_gate_is_refused_without_force(store, tmp_path):
    _sales()
    store["list_id"] = "Done"
    with pytest.raises(SystemExit, match="was approved"):
        _write("o/r#1", "PROPOSAL", "x", tmp_path)


def test_approve_at_an_owner_gate_moves_to_next(store, monkeypatch):
    _sales()
    store["list_id"] = "Review"
    commands.cmd_approve(argparse.Namespace(id="o/r#1", force=False))
    gate = store["metadata"]["gates"]["prepare"]
    assert store["list_id"] == "Done" and gate["approved_at"] and "approved_by" in gate


def test_approve_at_an_owner_gate_without_next_moves_to_the_column_after_done(store):
    C.PIPELINE = {**SALES, "stages": {"prepare": {k: v for k, v in SALES["stages"]["prepare"].items() if k != "next"}}}
    store["list_id"] = "Review"
    commands.cmd_approve(argparse.Namespace(id="o/r#1", force=False))
    assert store["list_id"] == "Done" and store["metadata"]["gates"]["prepare"]["approved_at"]


def test_approve_at_a_last_column_gate_without_next_stays_put(store):
    C.PIPELINE = {**SALES, "stages": {"prepare": {"column": "Inbox", "done": "Done", "skill": "pl-stage",
                                                  "section": "PROPOSAL", "gate": "owner"}}}
    store["list_id"] = "Done"
    commands.cmd_approve(argparse.Namespace(id="o/r#1", force=False))
    assert store["list_id"] == "Done" and store["metadata"]["gates"]["prepare"]["approved_at"]


def test_send_back_reruns_the_stage_and_it_returns_to_the_gate(store, monkeypatch, tmp_path):
    _sales()
    _write("o/r#1", "PROPOSAL", "first offer", tmp_path)
    store["list_id"] = "Review"
    commands.cmd_reject(argparse.Namespace(id="o/r#1", notes="cheaper please"))
    assert store["list_id"] == "Inbox" and store["metadata"]["worker"] is None
    assert "cheaper please" in board.sections(store["description"])["REVIEW NOTES"]
    assert _pass(monkeypatch, [dict(store)]) == [("o/r#1", "prepare")]
    _write("o/r#1", "PROPOSAL", "cheaper offer", tmp_path)
    store["list_id"] = "Review"
    assert pipeline.gate_for("Review")[0] == "prepare"


@pytest.mark.parametrize("cmd,args", [(commands.cmd_approve, {"force": False}), (commands.cmd_reject, {"notes": "n"})])
def test_approve_off_a_gate_exits_2(store, cmd, args):
    _sales()
    store["list_id"] = "Working"
    with pytest.raises(SystemExit) as e:
        cmd(argparse.Namespace(id="o/r#1", **args))
    assert e.value.code == 2 or isinstance(e.value, commands.BadInput)
    assert "Working" in str(e.value.code)


# ---------- plan 61 slice 4: the dispatcher runs the agent a stage names ----------

def _dry_line(capsys, stage):
    dispatch.start_worker(_card(1, "Inbox"), stage, 1, True)
    return capsys.readouterr().out


def test_ac6_no_agent_dry_run_line_is_unchanged_engineering(monkeypatch, capsys):
    C.PIPELINE = {**pipeline.engineering(), "template": "engineering"}
    C.STAGES = {"spec": {}}
    C.PROMPTS = {"spec": "/pl-spec {id}"}
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (dispatch.harnesses.get("claude"), "claude"))
    monkeypatch.setattr(dispatch.harnesses, "model_reason", lambda *a, **k: ("sonnet", "account claude"))
    assert _dry_line(capsys, "spec") == "  would start spec-card-1 under claude model sonnet (account claude): /pl-spec o/r#1\n"


def test_ac6_no_agent_dry_run_line_is_unchanged_generic(monkeypatch, capsys):
    _sales()
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (dispatch.harnesses.get("claude"), "claude"))
    monkeypatch.setattr(dispatch.harnesses, "model_reason", lambda *a, **k: ("sonnet", "account claude"))
    assert _dry_line(capsys, "prepare") == "  would start prepare-card-1 under claude model sonnet (account claude): /pl-stage o/r#1 stage=prepare\n"


COPYWRITER = {"name": "marketing-copywriter", "source": "built-in", "instructions": "Write warm copy.",
              "skills": ["pl-stage"], "connector_roles": ["messaging"], "model": "sonnet", "approval": [], "pod": None}


def _agent_stage(monkeypatch, tmp_path, agent=COPYWRITER, name="marketing-copywriter"):
    stage = {**SALES["stages"]["prepare"], "agent": name}
    C.PIPELINE, C.COLUMNS = {**SALES, "stages": {"prepare": stage}}, SALES["columns"]
    C.STAGES = {"prepare": {"agent": name}}
    C.PROFILES = {"claude": tmp_path / ".claude", "codex-a": tmp_path / ".codex"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}, "codex-a": {"harness": "codex"}}
    monkeypatch.setattr(dispatch.agent_defs, "load", lambda: {agent["name"]: agent} if agent else {})
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (dispatch.harnesses.get("claude"), "claude"))
    monkeypatch.setattr(dispatch.harnesses, "model_reason", lambda *a, **k: ("opus", "account claude"))


def _real_start(monkeypatch):
    """start_worker for real with tmux and the launch faked; (launch_script kwargs, update metadata) lists."""
    kws, metas = [], []
    monkeypatch.setattr(C, "TMUX_SESSION", "pl-t", raising=False)
    monkeypatch.setattr(C, "WORK_DIR", "/w", raising=False)
    monkeypatch.setattr(C, "ATTENTION", None, raising=False)
    monkeypatch.setattr(dispatch.harnesses, "unattended", lambda h: h)
    monkeypatch.setattr(dispatch.harnesses, "launch_script", lambda h, prof, p, sid, label, **kw: kws.append(kw) or "")
    monkeypatch.setattr(dispatch.context, "launch_prompt", lambda c, s, p, **kw: p)
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=0))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "%1")
    monkeypatch.setattr(dispatch, "_launch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: metas.append(f.get("metadata")))
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: None)
    return kws, metas


def test_ac2_launch_offers_exactly_the_agent_skills(monkeypatch, tmp_path):
    _agent_stage(monkeypatch, tmp_path)
    kws, _ = _real_start(monkeypatch)
    dispatch.start_worker(_card(1, "Inbox"), "prepare", 1, False)
    assert kws[0]["pod"]["skills"] == ["pl-stage"] and kws[0]["pod"]["name"] == "agent-marketing-copywriter"
    assert kws[0]["pod"]["roles"] == ["messaging"]


def test_ac3_agent_model_is_used_and_reason_names_agent(monkeypatch, tmp_path):
    _agent_stage(monkeypatch, tmp_path)
    kws, metas = _real_start(monkeypatch)
    dispatch.start_worker(_card(1, "Inbox"), "prepare", 1, False)
    w = metas[0]["worker"]
    assert (w["model"], w["model_reason"], w["agent"]) == ("sonnet", "agent marketing-copywriter", "marketing-copywriter")
    assert kws[0]["model"] == "sonnet"


def test_ac3_agent_account_moves_the_harness(monkeypatch, tmp_path):
    _agent_stage(monkeypatch, tmp_path, {**COPYWRITER, "model": "codex-a"})
    kws, metas = _real_start(monkeypatch)
    dispatch.start_worker(_card(1, "Inbox"), "prepare", 1, False)
    w = metas[0]["worker"]
    assert (w["harness"], w["profile"], w["model_reason"]) == ("codex", "codex-a", "agent marketing-copywriter")
    assert "model" not in kws[0]


def test_ac4_agent_without_model_keeps_stage_model_and_reason(monkeypatch, tmp_path):
    _agent_stage(monkeypatch, tmp_path, {**COPYWRITER, "model": None})
    kws, metas = _real_start(monkeypatch)
    dispatch.start_worker(_card(1, "Inbox"), "prepare", 1, False)
    w = metas[0]["worker"]
    assert (w["model"], w["model_reason"], w["profile"]) == ("opus", "account claude", "claude")
    assert "model" not in kws[0]


def test_a_stage_without_an_agent_passes_no_agent_keywords(monkeypatch, tmp_path):
    _agent_stage(monkeypatch, tmp_path)
    C.PIPELINE["stages"]["prepare"].pop("agent")
    C.STAGES = {"prepare": {}}
    kws, metas = _real_start(monkeypatch)
    dispatch.start_worker(_card(1, "Inbox"), "prepare", 1, False)
    assert not {"pod", "model"} & kws[0].keys() and "agent" not in metas[0]["worker"]


def test_with_model_override_wins_only_for_a_valid_model_name(monkeypatch):
    h = dispatch.harnesses.get("claude")
    flags = lambda model: dispatch.harnesses._with_model(h.interactive, h, "claude", None, model=model)[1]["model"]
    assert flags("haiku") == "haiku"
    assert flags("bad name;") != "bad name;"
