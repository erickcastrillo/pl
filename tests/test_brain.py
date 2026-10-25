import json
import pytest
from pathlib import Path
from mcp import types
from pl import brain, brain_tools, manager


class FakeSession:
    def __init__(self, tools, handler=None):
        self._tools = tools
        self._handler = handler

    def run(self, fn):
        import asyncio
        import concurrent.futures
        import inspect

        class S:
            def __init__(self, tools, handler):
                self._tools = tools
                self._handler = handler

            async def list_tools(self):
                return types.ListToolsResult(tools=self._tools)

            async def call_tool(self, name, args):
                if self._handler:
                    res = self._handler(name, args)
                    if inspect.iscoroutine(res):
                        return await res
                    return res
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"id": "rec-1", "url": "https://example.com/rec-1"}))])

        async def _run():
            res = fn(S(self._tools, self._handler))
            if inspect.iscoroutine(res):
                return await res
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_run())).result()

    def scrub(self, text, limit=None):
        return text


def test_tool_set_has_every_connector_read_only_and_skips_brain_false(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Set up profile with connectors:
    # 1. conn_skip has brain = false
    # 2. conn_active has read tool and write tool
    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    cfg_text = """
[tracker]
type = "github-issues"
repo = "org/repo"

[connectors.conn_skip]
brain = false
server = { command = "fake-skip" }

[connectors.conn_active]
server = { command = "fake-active" }
"""
    (pdir / "config.toml").write_text(cfg_text)

    # In-memory fake sessions
    skip_tools = [
        types.Tool(
            name="tool_skip_read",
            description="Skip read tool",
            inputSchema={"type": "object"},
            annotations=types.ToolAnnotations(readOnlyHint=True),
        )
    ]
    active_tools = [
        types.Tool(
            name="tool_read",
            description="Active read tool",
            inputSchema={"type": "object"},
            annotations=types.ToolAnnotations(readOnlyHint=True),
        ),
        types.Tool(
            name="tool_write",
            description="Active write tool",
            inputSchema={"type": "object"},
            annotations=types.ToolAnnotations(readOnlyHint=False),
        ),
    ]

    sessions = {
        "fake-skip": FakeSession(skip_tools),
        "fake-active": FakeSession(active_tools),
    }

    def fake_session(server, timeout=60, as_is=False):
        cmd = server.get("command")
        return cmd, sessions[cmd]

    monkeypatch.setattr(brain_tools, "_session", fake_session)

    server = brain_tools.build()
    tool_names = [t.name for t in server._tool_manager.list_tools()]

    # Standard pl read tools are present
    assert "pl_pipelines" in tool_names
    assert "pl_inbox" in tool_names
    assert "pl_alerts" in tool_names
    assert "pl_list" in tool_names
    assert "pl_card" in tool_names
    assert "pl_standup" in tool_names

    # Connector with brain = false is skipped entirely
    assert "conn_skip__tool_skip_read" not in tool_names

    # Connector read tool is proxied
    assert "conn_active__tool_read" in tool_names

    # Connector write tool is NOT exposed
    assert "conn_active__tool_write" not in tool_names


def test_an_unmarked_tool_is_not_exposed(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    cfg_text = """
[tracker]
type = "github-issues"
repo = "org/repo"

[connectors.conn_unmarked]
server = { command = "fake-unmarked" }
"""
    (pdir / "config.toml").write_text(cfg_text)

    # One tool has readOnlyHint=True, the other has no annotation / unmarked
    tools = [
        types.Tool(
            name="tool_read",
            description="Active read tool",
            inputSchema={"type": "object"},
            annotations=types.ToolAnnotations(readOnlyHint=True),
        ),
        types.Tool(
            name="tool_plain",
            description="Unmarked tool",
            inputSchema={"type": "object"},
            annotations=None,
        ),
    ]

    sessions = {
        "fake-unmarked": FakeSession(tools),
    }

    def fake_session(server, timeout=60, as_is=False):
        cmd = server.get("command")
        return cmd, sessions[cmd]

    monkeypatch.setattr(brain_tools, "_session", fake_session)

    server = brain_tools.build()
    tool_names = [t.name for t in server._tool_manager.list_tools()]

    assert "conn_unmarked__tool_read" in tool_names
    # Unmarked tool must not be exposed
    assert "conn_unmarked__tool_plain" not in tool_names


@pytest.mark.asyncio
async def test_tool_results_are_logged_as_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    pdir = tmp_path / ".pl-test"
    pdir.mkdir(parents=True)
    cfg_text = """
[tracker]
type = "github-issues"
repo = "org/repo"

[connectors.billing]
server = { command = "fake-billing" }
"""
    (pdir / "config.toml").write_text(cfg_text)

    record_data = {
        "id": "pay-999",
        "url": "https://billing.example/payments/pay-999",
        "title": "Payment from client X",
        "amount": 500,
    }

    def fake_handler(name, args):
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(record_data))])

    tools = [
        types.Tool(
            name="get_payment",
            description="Get payment details",
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}},
            annotations=types.ToolAnnotations(readOnlyHint=True),
        ),
    ]

    sessions = {
        "fake-billing": FakeSession(tools, handler=fake_handler),
    }

    def fake_session(server, timeout=60, as_is=False):
        cmd = server.get("command")
        return cmd, sessions[cmd]

    monkeypatch.setattr(brain_tools, "_session", fake_session)

    conv_id = "test-conv-sources-1"
    server = brain_tools.build(conv=conv_id)

    # Call the proxied tool
    await server.call_tool("billing__get_payment", {"id": "pay-999"})

    # Check sources logged
    logged = brain.get_logged_sources(conv_id)
    assert len(logged) > 0
    urls = [s.get("url") or s.get("link") for s in logged]
    assert "https://billing.example/payments/pay-999" in urls


def test_file_card_creates_a_card_in_the_named_pipeline_only(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    calls = []

    def fake_pl(argv):
        import os
        import subprocess
        calls.append((list(argv), os.environ.get("PL_SOURCE")))
        return subprocess.CompletedProcess(argv, returncode=0, stdout='{"id": "card-123"}', stderr="")

    from pl import mcp_serve
    monkeypatch.setattr(mcp_serve, "_pl", fake_pl)

    server = brain_tools.build()
    # Call pl_file_card
    tool = server._tool_manager.get_tool("pl_file_card")
    assert tool is not None

    import asyncio
    res = asyncio.run(server.call_tool("pl_file_card", {
        "pipeline": "billing",
        "title": "Send client X an invoice",
        "text": "Please prepare an invoice for $500",
    }))
    assert res is not None

    assert len(calls) == 1
    argv, source = calls[0]
    assert argv == ["--profile", "billing", "idea", "Please prepare an invoice for $500", "--title", "Send client X an invoice"]
    assert source == "brain"


def test_no_other_write_tool_is_listed(monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "machine_dir", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    server = brain_tools.build()
    tool_names = [t.name for t in server._tool_manager.list_tools()]

    # pl_file_card is present
    assert "pl_file_card" in tool_names

    # All known write commands must NOT be in tools
    forbidden_writes = [
        "pl_approve", "pl_reject", "pl_move", "pl_drop", "pl_done",
        "pl_undrop", "pl_hold", "pl_unhold", "pl_retry", "pl_restart",
        "pl_move_agent", "pl_pause", "pl_resume",
    ]
    for fw in forbidden_writes:
        assert fw not in tool_names
