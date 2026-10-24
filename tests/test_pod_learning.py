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


def test_the_third_removal_in_one_pod_suggests_a_rule(monkeypatch, tmp_path, capsys):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    draft = "Never use SQLite in production.\nAlways write tests first."
    approved = "Always write tests first."

    # First removal
    pods.learn("card-1", "PLAN", draft, approved, pod="engineering")
    sugg_file = C.STATE_DIR / "pod-suggestions.json"
    assert not sugg_file.exists()

    # Second removal
    pods.learn("card-2", "PLAN", draft, approved, pod="engineering")
    assert not sugg_file.exists()

    # Third removal
    pods.learn("card-3", "PLAN", draft, approved, pod="engineering")
    assert sugg_file.is_file()
    data = json.loads(sugg_file.read_text())
    suggs = list(data.values()) if isinstance(data, dict) else data
    assert len(suggs) == 1
    s = suggs[0]
    assert s["pod"] == "engineering"
    assert s["rule"] == "Never write: Never use SQLite in production."
    assert s["seen"] == 3

    # Check alert was opened
    from pl import alerts
    live_alerts = [k for k, a in alerts._load().items() if alerts._live(a)]
    assert f"pod_rule:{s['id']}" in live_alerts

    out = capsys.readouterr().out
    assert f"accept with pl pods rule accept {s['id']}" in out


def test_removals_in_two_pods_do_not_add_up(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    draft = "Never offer discounts.\nBe polite."
    approved = "Be polite."

    # Two removals in engineering
    pods.learn("card-1", "PLAN", draft, approved, pod="engineering")
    pods.learn("card-2", "PLAN", draft, approved, pod="engineering")

    # Two removals in sales
    pods.learn("card-3", "PLAN", draft, approved, pod="sales")
    pods.learn("card-4", "PLAN", draft, approved, pod="sales")

    sugg_file = C.STATE_DIR / "pod-suggestions.json"
    assert not sugg_file.exists()


def test_accepting_adds_the_rule_with_a_version_note(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    draft = "Never use SQLite in production.\nAlways write tests first."
    approved = "Always write tests first."

    pods.learn("card-1", "PLAN", draft, approved, pod="engineering")
    pods.learn("card-2", "PLAN", draft, approved, pod="engineering")
    pods.learn("card-3", "PLAN", draft, approved, pod="engineering")

    data = json.loads((C.STATE_DIR / "pod-suggestions.json").read_text())
    sugg_id = next(iter(data.keys())) if isinstance(data, dict) else data[0]["id"]

    import argparse
    pods.cmd_pods_rule_accept(argparse.Namespace(id=sugg_id, yes=True))

    pod_file = pods.pod_file("engineering")
    assert pod_file.is_file()
    assert "Never write: Never use SQLite in production." in pod_file.read_text()

    # Check version history and note
    history = pods.pod_history("engineering")
    assert len(history) >= 1
    assert history[-1]["note"] == "from 3 edited drafts"

    # Check alert was resolved
    from pl import alerts
    live_alerts = [k for k, a in alerts._load().items() if alerts._live(a)]
    assert f"pod_rule:{sugg_id}" not in live_alerts


def test_a_dismissed_rule_is_not_suggested_again(tmp_path, capsys):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "engineering"\n')
    C.load("test", config_dir=str(prof))

    draft = "Never use SQLite in production.\nAlways write tests first."
    approved = "Always write tests first."

    pods.learn("card-1", "PLAN", draft, approved, pod="engineering")
    pods.learn("card-2", "PLAN", draft, approved, pod="engineering")
    pods.learn("card-3", "PLAN", draft, approved, pod="engineering")

    data = json.loads((C.STATE_DIR / "pod-suggestions.json").read_text())
    sugg_id = next(iter(data.keys())) if isinstance(data, dict) else data[0]["id"]

    import argparse
    pods.cmd_pods_rule_dismiss(argparse.Namespace(id=sugg_id))

    # Alert resolved
    from pl import alerts
    live_alerts = [k for k, a in alerts._load().items() if alerts._live(a)]
    assert f"pod_rule:{sugg_id}" not in live_alerts

    capsys.readouterr()  # clear buffer

    # A 4th removal happens
    pods.learn("card-4", "PLAN", draft, approved, pod="engineering")
    out = capsys.readouterr().out
    assert "pl suggests a pod rule" not in out
    live_alerts = [k for k, a in alerts._load().items() if alerts._live(a)]
    assert f"pod_rule:{sugg_id}" not in live_alerts


def test_cli_pods_rule_add_and_accept(monkeypatch, tmp_path, capsys):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True)
    (prof / "config.toml").write_text('[pipeline]\npod = "sales"\n')
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof))
    C.load("test", config_dir=str(prof))

    import sys
    from pl import cli

    # Test pl pods rule add sales "Always qualify leads" --yes
    monkeypatch.setattr(sys, "argv", ["pl", "pods", "rule", "add", "sales", "Always qualify leads", "--yes"])
    cli.main()
    out = capsys.readouterr().out
    assert "added rule to sales (version 1): Always qualify leads" in out

    pod_file = pods.pod_file("sales")
    assert pod_file.is_file()
    assert "- Always qualify leads" in pod_file.read_text()

    # Create a suggestion
    draft = "Always qualify leads.\nOffer 50% discount."
    approved = "Always qualify leads."
    pods.learn("card-1", "PLAN", draft, approved, pod="sales")
    pods.learn("card-2", "PLAN", draft, approved, pod="sales")
    pods.learn("card-3", "PLAN", draft, approved, pod="sales")

    data = json.loads((C.STATE_DIR / "pod-suggestions.json").read_text())
    sugg_id = next(iter(data.keys()))

    # Test pl pods rule accept ID --yes
    monkeypatch.setattr(sys, "argv", ["pl", "pods", "rule", "accept", sugg_id, "--yes"])
    cli.main()
    out = capsys.readouterr().out
    assert "accepted rule for sales: Never write: Offer 50% discount." in out
    assert "- Never write: Offer 50% discount." in pod_file.read_text()



