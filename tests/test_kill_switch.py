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

    outward.set(True, by="erick")
    assert outward.state()["on"] is True
    assert agents.hold_reason(card) == "kill switch on"

    outward.set(False, by="erick")
    assert outward.state()["on"] is False
    assert agents.hold_reason(card) is None


def test_a_card_without_outward_is_never_held(monkeypatch, tmp_path):
    from pl import outward

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))

    outward.set(True, by="erick")

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

    outward.set(True, by="erick")
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

