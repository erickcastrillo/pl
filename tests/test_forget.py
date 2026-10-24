"""pl forget --contact <id|email> --yes [--dry-run] [--json] removes contact from cards, logs, memory, records audit."""
import copy
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from pl import cli, commands, config as C, trackers
from pl.board import sections

CONTACT_A = "alice@example.test"
CONTACT_B = "bob@example.test"


class FakeTracker:
    def __init__(self, items):
        self.items = {c["id"]: c for c in items}
        self.writes = []

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        self.writes.append((item_id, copy.deepcopy(fields)))
        c = self.items[item_id]
        if "column" in fields:
            fields["list_id"] = self.columns()[fields.pop("column")]
        if "metadata" in fields:
            if verify:
                c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
            else:
                c["metadata"] = dict(fields.pop("metadata"))
        c.update(fields)
        return copy.deepcopy(c)

    def url(self, item_id):
        return f"https://example.test/{item_id}"


def mk_card(cid, col="Inbox", desc="", **meta):
    return {
        "id": cid,
        "title": f"Card {cid}",
        "description": desc,
        "tags": [],
        "list_id": f"col-{col}",
        "updated_at": "",
        "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta},
    }


@pytest.fixture(autouse=True)
def fake_env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".pl-t"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text("")
    monkeypatch.setenv("PL_CONFIG_DIR", str(d))
    monkeypatch.delenv("PL_TMUX_SESSION", raising=False)
    C.load("t")
    C.USER_EMAIL = "me@example.test"
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake_board(monkeypatch):
    fake = FakeTracker([])
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    return fake


def test_forget_removes_the_contact_from_cards_logs_and_memory_and_prints_counts(fake_board, monkeypatch):
    desc_1 = (
        "# PIPELINE: INPUT\n"
        "```email\n"
        f"From: {CONTACT_A}\n"
        "Need help with setup\n"
        "```\n"
        "Some details\n"
        "# PIPELINE: FIELDS\n"
        "custom_field = 123\n"
    )
    c1 = mk_card("c1", desc=desc_1, contact_emails=[CONTACT_A, CONTACT_B])
    c2 = mk_card("c2", desc="regular card", **{"from": CONTACT_A})
    c3 = mk_card("c3", desc="another card", refs={"contacts": [CONTACT_A]})
    fake_board.items = {c["id"]: c for c in (c1, c2, c3)}

    # Logs setup
    (C.STATE_DIR / "events.jsonl").write_text(
        f'{{"kind": "idea", "contact": "{CONTACT_A}"}}\n'
        f'{{"kind": "idea", "contact": "{CONTACT_B}"}}\n'
    )
    (C.STATE_DIR / "mcp-server.log").write_text(
        f"2026-10-23 info processing {CONTACT_A}\n"
        f"2026-10-23 info processing {CONTACT_B}\n"
    )

    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", CONTACT_A, "--yes"])
    with redirect_stdout(buf):
        cli.main()

    out = buf.getvalue()
    assert "cards: 3" in out
    assert "logs: 2" in out
    assert "memory: 0" in out
    assert "audit: 1" in out

    # Verify cards cleaned
    c1_after = fake_board.items["c1"]
    assert c1_after["metadata"]["contact_emails"] == [CONTACT_B]
    assert "FIELDS" not in sections(c1_after["description"])
    assert "```email" not in c1_after["description"]
    assert CONTACT_A not in c1_after["description"]

    c2_after = fake_board.items["c2"]
    assert "from" not in c2_after["metadata"]

    c3_after = fake_board.items["c3"]
    assert "refs" not in c3_after["metadata"] or "contacts" not in c3_after["metadata"].get("refs", {})

    # Verify logs rewritten
    events_text = (C.STATE_DIR / "events.jsonl").read_text()
    assert CONTACT_A not in events_text
    assert CONTACT_B in events_text

    mcp_text = (C.STATE_DIR / "mcp-server.log").read_text()
    assert CONTACT_A not in mcp_text
    assert CONTACT_B in mcp_text

    # Verify audit log
    audit_text = (C.STATE_DIR / "audit.jsonl").read_text()
    assert CONTACT_A not in audit_text
    expected_hash = hashlib.sha256(CONTACT_A.encode("utf-8")).hexdigest()
    assert expected_hash in audit_text


def test_dry_run_counts_and_changes_nothing(fake_board, monkeypatch):
    c1 = mk_card("c1", desc="# PIPELINE: FIELDS\nx=1\n", contact_emails=[CONTACT_A])
    fake_board.items = {"c1": c1}

    ev_path = C.STATE_DIR / "events.jsonl"
    ev_path.write_text(f'{{"contact": "{CONTACT_A}"}}\n')

    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", CONTACT_A, "--yes", "--dry-run", "--json"])
    with redirect_stdout(buf):
        cli.main()

    res = json.loads(buf.getvalue())
    assert res["cards"] == 1
    assert res["logs"] == 1
    assert res["memory"] == 0
    assert res["audit"] == 0

    # Ensure nothing was updated or rewritten
    assert len(fake_board.writes) == 0
    assert ev_path.read_text() == f'{{"contact": "{CONTACT_A}"}}\n'
    assert not (C.STATE_DIR / "audit.jsonl").exists()


def test_other_contacts_are_untouched(fake_board, monkeypatch):
    c1 = mk_card("c1", desc="# PIPELINE: FIELDS\nx=1\n", contact_emails=[CONTACT_B])
    fake_board.items = {"c1": c1}

    ev_path = C.STATE_DIR / "events.jsonl"
    ev_path.write_text(f'{{"contact": "{CONTACT_B}"}}\n')

    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", CONTACT_A, "--yes", "--json"])
    with redirect_stdout(buf):
        cli.main()

    res = json.loads(buf.getvalue())
    assert res["cards"] == 0
    assert res["logs"] == 0
    assert res["memory"] == 0
    assert res["audit"] == 1

    assert len(fake_board.writes) == 0
    assert ev_path.read_text() == f'{{"contact": "{CONTACT_B}"}}\n'
    assert fake_board.items["c1"]["metadata"]["contact_emails"] == [CONTACT_B]


def test_without_yes_and_no_tty_exits_2(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", CONTACT_A])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_interactive_confirm_yes_and_no(fake_board, monkeypatch):
    c1 = mk_card("c1", desc="card", contact_emails=[CONTACT_A])
    fake_board.items = {"c1": c1}

    # Rejecting: "n"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", CONTACT_A])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main()
    assert "nothing changed" in buf.getvalue()
    assert len(fake_board.writes) == 0

    # Confirming: "y"
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main()
    assert "cards: 1" in buf.getvalue()
    assert len(fake_board.writes) == 1


def test_contact_id_in_refs_and_case_insensitivity(fake_board, monkeypatch):
    c1 = mk_card("c1", desc="card", refs={"contacts": ["ID_999"]})
    fake_board.items = {"c1": c1}

    buf = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["pl", "forget", "--contact", "id_999", "--yes", "--json"])
    with redirect_stdout(buf):
        cli.main()

    res = json.loads(buf.getvalue())
    assert res["cards"] == 1
    assert "contacts" not in fake_board.items["c1"]["metadata"].get("refs", {})
