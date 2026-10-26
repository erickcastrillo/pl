"""pl mcp serve --stdio: the read tools run `pl ... --json`; actions through it are marked as an agent session."""
import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import time

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pl import cli, commands, confirm, events, mcp_serve, trackers
from pl import config as C
from pl.util import BadInput
from pl.tui.loops import event_text


@pytest.fixture(autouse=True)
def _no_dry_run_after():
    """A test that runs `pl ... --dry-run` in process turns on the module-wide dry-run flag; later tests would write nothing."""
    yield
    trackers.set_dry_run(False)

READ_TOOLS = {"pl_inbox", "pl_list", "pl_card", "pl_section", "pl_alerts", "pl_standup", "pl_usage"}
WRITE_TOOLS = {"pl_idea", "pl_section_write", "pl_approve"}
ALLOWED = READ_TOOLS | WRITE_TOOLS
CARD = {"id": "acme/app#7", "title": "A card", "list_id": "L1", "tags": [], "updated_at": "2026-10-25T10:00:00+00:00",
        "metadata": {}, "description": "# PIPELINE: SPEC\nthe spec\n"}


@pytest.fixture(autouse=True)
def fake_board(monkeypatch):
    monkeypatch.setattr(commands, "find_card", lambda cid: CARD)
    monkeypatch.setattr(commands, "sections", lambda d: {"SPEC": "the spec"})
    from pl import inbox
    monkeypatch.setattr(inbox, "listing", lambda: [])


@pytest.fixture
def in_process(monkeypatch):
    calls = []

    def run(argv):
        calls.append(argv)
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "argv", ["pl", *argv])
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())

    monkeypatch.setattr(mcp_serve, "_pl", run)
    return calls


async def call(name, args):
    async with create_connected_server_and_client_session(mcp_serve.build()._mcp_server) as client:
        return await client.call_tool(name, args)


async def test_pl_inbox_tool_returns_the_same_json_as_pl_inbox_json(in_process, capsys):
    res = await call("pl_inbox", {})
    assert not res.isError
    assert json.loads(res.content[0].text) == {"items": []}
    assert in_process == [["inbox", "--json"]]


async def test_tools_list_has_only_the_allowed_tools():
    async with create_connected_server_and_client_session(mcp_serve.build()._mcp_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == ALLOWED


async def test_a_failing_command_becomes_a_tool_error(monkeypatch):
    monkeypatch.setattr(mcp_serve, "_pl", lambda argv: subprocess.CompletedProcess(argv, 2, "", "pl: no card nope\n"))
    res = await call("pl_card", {"id": "nope"})
    assert res.isError
    assert "no card nope" in res.content[0].text


async def test_the_profile_goes_before_the_command_and_the_source_is_mcp(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("PL_SOURCE", raising=False)
    await call("pl_usage", {"profile": "work", "since": "7d", "by": "card"})
    assert seen["argv"][-8:] == ["--profile", "work", "usage", "--since", "7d", "--by", "card", "--json"]
    assert seen["env"]["PL_SOURCE"] == "mcp"


def test_an_action_through_the_server_shows_as_agent_session_in_activity(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, card=None, **f: seen.append({"kind": kind, "card": card, **f}))
    monkeypatch.setenv("PL_SOURCE", "mcp")
    monkeypatch.setattr(commands, "cmd_section", lambda a: None, raising=False)
    monkeypatch.setattr(sys, "argv", ["pl", "section", "acme/app#7", "SPEC", "--dry-run"])
    with contextlib.suppress(SystemExit):
        cli.main()
    event = next(e for e in seen if e["kind"] == "remote_action")
    assert event["source"] == "mcp"
    assert "agent session (pl mcp serve)" in event_text(event)


async def test_no_merge_drop_done_or_move_tool_exists():
    async with create_connected_server_and_client_session(mcp_serve.build()._mcp_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not {n for n in names if any(w in n for w in ("merge", "drop", "done", "move", "kill", "undrop"))}
    assert WRITE_TOOLS <= names


async def test_write_tools_run_the_matching_commands(monkeypatch):
    seen = []

    def fake(argv, stdin=None):
        seen.append((argv, stdin))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(mcp_serve, "_pl", fake)
    await call("pl_idea", {"text": "Add dark mode", "title": "Dark mode"})
    await call("pl_section_write", {"id": "acme/app#7", "name": "spec", "text": "the new spec"})
    await call("pl_approve", {"id": "acme/app#7"})
    assert seen == [(["idea", "Add dark mode", "--title", "Dark mode"], None),
                    (["section", "acme/app#7", "SPEC", "--from", "-"], "the new spec"),
                    (["approve", "acme/app#7"], None)]


async def test_section_write_refuses_other_sections():
    res = await call("pl_section_write", {"id": "acme/app#7", "name": "INPUT", "text": "x"})
    assert res.isError


class FakeTracker:
    def __init__(self, card):
        self.items = {card["id"]: card}

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


def gate(monkeypatch, tmp_path, outward_meta, source="mcp"):
    card = {"id": "card-app", "title": "Send bulk notification", "list_id": "col-Plan for review",
            "description": "# PIPELINE: SPEC\nspec\n# PIPELINE: PLAN\nplan", "metadata": {"outward": outward_meta} if outward_meta else {}}
    fake = FakeTracker(card)
    monkeypatch.setattr(trackers, "get", lambda kind: fake)
    monkeypatch.setattr(commands, "find_card", lambda cid: dict(fake.items[cid]))
    monkeypatch.setattr(commands, "sections", lambda d: {"SPEC": "spec", "PLAN": "plan"})
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    for var in ("PL_SOURCE", "PL_ASSISTANT", "PL_STAGE"):
        monkeypatch.delenv(var, raising=False)
    if source:
        monkeypatch.setenv("PL_SOURCE", source)
    sent = []
    monkeypatch.setattr(commands, "notify", lambda title, message: sent.append(message))
    return fake, sent


def approve(**kw):
    return commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code=None, **kw))


def test_agent_approve_of_an_outward_gate_is_pending_and_card_does_not_move(monkeypatch, tmp_path, capsys):
    fake, sent = gate(monkeypatch, tmp_path, {"kind": "email", "recipients": 1})
    approve()
    c = fake.items["card-app"]
    assert c["list_id"] == "col-Plan for review"
    assert c["metadata"]["approval_pending"]["by"] == "agent session"
    assert "pending: the owner confirms" in capsys.readouterr().out
    assert len(sent) == 1


def test_owner_confirms_in_console_and_the_card_moves(monkeypatch, tmp_path):
    fake, _ = gate(monkeypatch, tmp_path, {"kind": "email", "recipients": 1})
    approve()
    monkeypatch.delenv("PL_SOURCE")
    approve()
    c = fake.items["card-app"]
    assert c["list_id"] == "col-Approved"
    assert not c["metadata"].get("approval_pending")


def test_one_time_code_works_once_and_expires_after_10_min(monkeypatch, tmp_path):
    fake, sent = gate(monkeypatch, tmp_path, {"kind": "email", "recipients": 1})
    approve()
    code = re.search(r"\b\d{6}\b", sent[0]).group()
    monkeypatch.delenv("PL_SOURCE")
    with pytest.raises(SystemExit) as e:
        commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code="000000" if code != "000000" else "111111"))
    assert isinstance(e.value, BadInput)   # BadInput exits 2
    commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code=code))
    assert fake.items["card-app"]["list_id"] == "col-Approved"
    fake.items["card-app"]["list_id"] = "col-Plan for review"
    with pytest.raises(SystemExit):
        commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code=code))
    monkeypatch.setenv("PL_SOURCE", "mcp")
    approve()
    code = re.search(r"\b\d{6}\b", sent[-1]).group()
    monkeypatch.delenv("PL_SOURCE")
    now = time.time()
    monkeypatch.setattr(confirm.time, "time", lambda: now + 601)
    with pytest.raises(SystemExit):
        commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code=code))


@pytest.mark.parametrize("var", ["PL_SOURCE", "PL_ASSISTANT"])
def test_code_refused_from_an_agent_session(monkeypatch, tmp_path, var):
    fake, sent = gate(monkeypatch, tmp_path, {"kind": "email", "recipients": 1})
    approve()
    code = re.search(r"\b\d{6}\b", sent[0]).group()
    monkeypatch.delenv("PL_SOURCE")
    monkeypatch.setenv(var, "mcp" if var == "PL_SOURCE" else "1")
    with pytest.raises(SystemExit) as e:
        commands.cmd_approve(argparse.Namespace(id="card-app", force=False, code=code))
    assert isinstance(e.value, BadInput)   # BadInput exits 2
    assert fake.items["card-app"]["list_id"] == "col-Plan for review"


def test_code_never_in_tool_output_events_or_card(monkeypatch, tmp_path, capsys):
    fake, sent = gate(monkeypatch, tmp_path, {"kind": "email", "recipients": 1})
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, card=None, **f: seen.append({"kind": kind, **f}))
    approve()
    code = re.search(r"\b\d{6}\b", sent[0]).group()
    assert code not in capsys.readouterr().out
    assert code not in json.dumps(seen) + json.dumps(fake.items["card-app"])
    assert any(e["kind"] == "approval_pending" for e in seen)
    path = tmp_path / "machine" / "confirm.json"
    assert code not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_agent_approve_of_a_plain_plan_gate_goes_through_and_is_marked(monkeypatch, tmp_path):
    from pl import audit
    fake, _ = gate(monkeypatch, tmp_path, None)
    monkeypatch.setattr(audit, "who", audit.who)
    approve()
    assert fake.items["card-app"]["list_id"] == "col-Approved"
    monkeypatch.setenv("PL_SOURCE", "mcp")
    assert audit.who() == "mcp-session"
