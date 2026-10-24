import json

from pl import commands, config as C, dispatch, pods


def test_a_removed_sentence_is_recorded_on_approval(monkeypatch, tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    draft = "Never use SQLite in production.\nAlways write tests first."
    approved = "Always write tests first."

    card_id = "test-card-1"
    c = {
        "id": card_id,
        "title": "Title",
        "list_id": "L1",
        "description": f"# PIPELINE: PLAN\n{draft}\n",
        "metadata": {},
    }
    monkeypatch.setattr(commands, "find_card", lambda cid: c)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Plan for review")
    monkeypatch.setattr(commands, "col_id", lambda n: "X")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a: None)

    pods.save_draft(card_id, "PLAN", draft)

    plan_file = C.STATE_DIR / "test-plan.md"
    plan_file.write_text(approved)
    monkeypatch.setattr(commands, "plan_path", lambda card: plan_file)
    monkeypatch.setattr(commands, "edited_plan", lambda card, path: approved)

    commands.cmd_approve(type("A", (), {"id": card_id, "force": False})())

    edits_file = C.STATE_DIR / "pod-edits.jsonl"
    assert edits_file.is_file()
    lines = [json.loads(line) for line in edits_file.read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["pod"] == "engineering"
    assert rec["sentence"] == "Never use SQLite in production."
    assert rec["card"] == card_id
    assert "sentence_hash" in rec
    assert "ts" in rec


def test_an_unedited_approval_records_nothing(monkeypatch, tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    plan = "Always write tests first."
    card_id = "test-card-2"
    c = {
        "id": card_id,
        "title": "Title",
        "list_id": "L1",
        "description": f"# PIPELINE: PLAN\n{plan}\n",
        "metadata": {},
    }
    monkeypatch.setattr(commands, "find_card", lambda cid: c)
    monkeypatch.setattr(commands, "col_name", lambda lid: "Plan for review")
    monkeypatch.setattr(commands, "col_id", lambda n: "X")
    monkeypatch.setattr(commands, "update", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a: None)

    pods.save_draft(card_id, "PLAN", plan)
    monkeypatch.setattr(commands, "plan_path", lambda card: C.STATE_DIR / "nope.md")
    monkeypatch.setattr(commands, "edited_plan", lambda card, path: None)

    commands.cmd_approve(type("A", (), {"id": card_id, "force": False})())

    edits_file = C.STATE_DIR / "pod-edits.jsonl"
    assert not edits_file.exists() or edits_file.read_text().strip() == ""


def test_dispatch_snapshots_draft_on_gate_column(monkeypatch, tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    card_id = "test-card-3"
    draft_plan = "Package 1: test.\nPackage 2: build."
    c = {
        "id": card_id,
        "title": "Title",
        "list_id": "L_GATE",
        "description": f"# PIPELINE: PLAN\n{draft_plan}\n",
        "metadata": {},
    }
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Plan for review" if lid == "L_GATE" else "In progress")

    st = {"last_col": {card_id: "In progress"}}
    monkeypatch.setattr(dispatch, "load_state", lambda: st)
    monkeypatch.setattr(dispatch, "save_state", lambda s: None)
    monkeypatch.setattr(dispatch, "cards", lambda: [c])
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "limited", lambda: None)
    monkeypatch.setattr(dispatch, "github_board", lambda: False)
    monkeypatch.setattr(dispatch, "paused", lambda: {})
    monkeypatch.setattr(dispatch, "pull_new", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)

    dispatch.dispatch_once(max_runs=1, dry=False, pull=False)

    snapshot = pods.draft_path(card_id, "PLAN")
    assert snapshot.is_file()
    assert snapshot.read_text() == draft_plan
    # Check permissions 0600
    assert oct(snapshot.stat().st_mode & 0o777) == "0o600"

