import stat


from pl import agents, commands, config
from pl import config as C


def test_on_holds_an_approved_outward_card_and_off_releases_it(monkeypatch, tmp_path):
    from pl import outward

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    card = {
        "id": "card-1",
        "column": "Approved",
        "metadata": {
            "outward": {"kind": "email", "recipients": 1},
        },
    }

    outward.set(True, by="alex")
    assert outward.state()["on"] is True
    assert agents.hold_reason(card) == "kill switch on"

    outward.set(False, by="alex")
    assert outward.state()["on"] is False
    assert agents.hold_reason(card) is None


def test_a_card_without_outward_is_never_held(monkeypatch, tmp_path):
    from pl import outward

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    outward.set(True, by="alex")

    # Card with no outward in metadata
    card1 = {"id": "card-1", "column": "Approved", "metadata": {}}
    assert agents.hold_reason(card1) is None

    # Card with empty outward
    card2 = {"id": "card-2", "column": "Approved", "metadata": {"outward": {}}}
    assert agents.hold_reason(card2) is None

    # Card not in Approved (e.g., In progress)
    card3 = {
        "id": "card-3",
        "column": "In progress",
        "metadata": {"outward": {"kind": "email", "recipients": 1}},
    }
    assert agents.hold_reason(card3) is None


def test_risky_is_money_deletion_or_more_than_10_recipients():
    from pl import outward

    # Not outward
    assert not outward.risky({})
    assert not outward.risky({"metadata": {}})
    assert not outward.risky({"metadata": {"outward": {}}})

    # Non-risky outward
    assert not outward.risky({"metadata": {"outward": {"kind": "email", "recipients": 5}}})
    assert not outward.risky({"metadata": {"outward": {"kind": "email", "recipients": 10}}})
    assert not outward.risky({"metadata": {"outward": {"kind": "email", "recipients": ["a"] * 10}}})

    # Money (amount)
    assert outward.risky({"metadata": {"outward": {"amount": 50}}})
    assert outward.risky({"metadata": {"outward": {"amount": "$100"}}})
    assert not outward.risky({"metadata": {"outward": {"amount": 0}}})
    assert not outward.risky({"metadata": {"outward": {"amount": None}}})

    # Deletion (deletes)
    assert outward.risky({"metadata": {"outward": {"deletes": True}}})
    assert not outward.risky({"metadata": {"outward": {"deletes": False}}})

    # More than 10 recipients
    assert outward.risky({"metadata": {"outward": {"recipients": 11}}})
    assert outward.risky({"metadata": {"outward": {"recipients": 100}}})
    assert outward.risky({"metadata": {"outward": {"recipients": ["user"] * 11}}})


def test_switch_file_is_0600_and_machine_wide(monkeypatch, tmp_path):
    from pl import outward

    machine_dir = tmp_path / "machine"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(machine_dir))

    # Set switch on
    st = outward.set(True, by="alice")
    assert st["on"] is True
    assert st["by"] == "alice"

    switch_file = machine_dir / "kill-switch.json"
    assert switch_file.is_file()
    mode = stat.S_IMODE(switch_file.stat().st_mode)
    assert mode == 0o600

    # Two profiles see the same state
    prof1 = tmp_path / ".pl-prof1"
    prof2 = tmp_path / ".pl-prof2"
    prof1.mkdir(parents=True)
    prof2.mkdir(parents=True)
    (prof1 / "config.toml").write_text('[tracker]\ntype = "mcp"\n')
    (prof2 / "config.toml").write_text('[tracker]\ntype = "mcp"\n')

    # Profile 1 reads state
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof1))
    config.load("prof1")
    assert outward.state()["on"] is True
    assert outward.state()["by"] == "alice"

    # Profile 2 turns it off
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof2))
    config.load("prof2")
    outward.set(False, by="bob")

    # Profile 1 sees it turned off
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof1))
    config.load("prof1")
    assert outward.state()["on"] is False
    assert outward.state()["by"] == "bob"


def test_approve_outward_card_waits_when_kill_switch_on(monkeypatch, tmp_path, capsys):
    import argparse
    from pl import outward, trackers

    class FakeTracker:
        def __init__(self):
            self.items = {}

        def columns(self):
            return {t: f"col-{t}" for t in C.COLUMNS}

        def cards(self, query=None):
            return [dict(c) for c in self.items.values()]

        def card(self, item_id):
            return dict(self.items[item_id])

        def update(self, item_id, **fields):
            c = self.items[item_id]
            if "metadata" in fields:
                c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
            c.update(fields)
            return dict(c)

    fake = FakeTracker()
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    outward.set(True, by="alex")
    card = {
        "id": "card-app",
        "title": "Send bulk notification",
        "list_id": "col-Plan for review",
        "description": "# PIPELINE: SPEC\nspec\n# PIPELINE: PLAN\nplan",
        "metadata": {"outward": {"kind": "email", "recipients": 1}},
    }
    fake.items[card["id"]] = card

    commands.cmd_approve(argparse.Namespace(id="card-app", force=False))
    captured = capsys.readouterr().out
    assert "approved; its outward action waits: the kill switch is on" in captured
    assert fake.items["card-app"]["list_id"] == "col-Approved"


def test_cli_on_off_status_json_and_exit_codes(monkeypatch, tmp_path, capsys):
    import json
    from pl import cli, events, outward

    machine_dir = tmp_path / "machine"
    prof_dir = tmp_path / ".pl-test"
    prof_dir.mkdir(parents=True)
    (prof_dir / "config.toml").write_text('[tracker]\ntype = "mcp"\n')

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(machine_dir))
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof_dir))

    # Helper to capture events
    event_log = []
    monkeypatch.setattr(events, "emit", lambda kind, card_id=None, **detail: event_log.append((kind, detail)))

    def run_cli(*args):
        capsys.readouterr()  # clear buffer
        monkeypatch.setattr("sys.argv", ["pl", *args])
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        out = capsys.readouterr().out
        return code, out

    # 1. status when initially off
    code, out = run_cli("kill-switch", "status")
    assert code == 0
    assert "kill switch is off" in out

    # 2. status --json when off
    code, out = run_cli("kill-switch", "status", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["on"] is False
    assert data["held"] == []

    # 3. off when already off exits 0 and says so
    code, out = run_cli("kill-switch", "off")
    assert code == 0
    assert "already off" in out

    # 4. on turns the switch on, emits event
    code, out = run_cli("kill-switch", "on")
    assert code == 0
    assert "kill switch on" in out
    assert outward.state()["on"] is True
    assert ("kill_switch", {"state": "on", "action": "on"}) in [(k, {"state": d.get("state"), "action": d.get("action")}) for k, d in event_log]

    # Create an inbox.json with a held card to verify status listing
    (prof_dir / "state").mkdir(parents=True, exist_ok=True)
    (prof_dir / "state" / "inbox.json").write_text(json.dumps({
        "at": 123456,
        "items": [],
        "held": [{"id": "card-99", "pipeline": "test", "title": "Risky outward card"}],
    }))

    # 5. status when on shows state and held cards
    code, out = run_cli("kill-switch", "status")
    assert code == 0
    assert "kill switch is on" in out
    assert "card-99" in out

    # 6. status --json when on
    code, out = run_cli("kill-switch", "status", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["on"] is True
    assert len(data["held"]) == 1
    assert data["held"][0]["id"] == "card-99"

    # 7. off turns the switch off, emits event
    code, out = run_cli("kill-switch", "off")
    assert code == 0
    assert "kill switch off" in out
    assert outward.state()["on"] is False
    assert ("kill_switch", {"state": "off", "action": "off"}) in [(k, {"state": d.get("state"), "action": d.get("action")}) for k, d in event_log]

    # 8. off again exits 0 and says already off
    code, out = run_cli("kill-switch", "off")
    assert code == 0
    assert "already off" in out


