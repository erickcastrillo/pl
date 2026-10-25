"""pl mcp serve --stdio: the read tools run `pl ... --json`; actions through it are marked as an agent session."""
import contextlib
import io
import json
import subprocess
import sys

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pl import cli, commands, events, mcp_serve
from pl.tui.loops import event_text

ALLOWED = {"pl_inbox", "pl_list", "pl_card", "pl_section", "pl_alerts", "pl_standup", "pl_usage"}
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
