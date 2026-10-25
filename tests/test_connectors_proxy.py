"""Plan 11 slice 6: team scoping: the harness gets only its team's connectors, through the read-only stdio proxy."""
import asyncio
import json
import stat

import pytest

from pl import config as C
from pl import connectors, connectors_proxy, dispatch, harnesses

TOML = ('[connectors.billing]\nurl = "https://mcp.example.com/mcp?key=SECRETKEY"\nteams = ["billing"]\n'
        '[connectors.sales]\ncommand = "sales-mcp"\nteams = ["sales"]\n'
        '[connectors.docs]\ncommand = "docs-mcp"\nteams = ["engineering", "billing"]\n')


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    (tmp_path / "machine").mkdir()
    connectors.path().write_text(TOML)
    C.load()
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    C.PROFILE_NAME = "engineering"
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_an_engineering_stage_gets_neither_billing_nor_sales_connector():
    assert dispatch.stage_servers("engineering", "run") == ["docs"]
    C.STAGES = {"run": {"connectors": ["docs", "billing"]}}
    assert dispatch.stage_servers("engineering", "run") == ["docs"]   # a stage can only narrow the team's list
    C.STAGES = {"run": {"connectors": []}}
    assert dispatch.stage_servers("engineering", "run") == []


def test_the_launch_mcp_file_is_0600_and_holds_no_token_or_url_secret():
    h = harnesses.get("claude")
    flags = harnesses.mcp_flags(h, ["billing"], profile="engineering", card_id="c1", stage="run", window="@7")
    f = C.STATE_DIR / "launch" / "_7.mcp.json"
    assert flags == [f"--mcp-config={f}", "--strict-mcp-config"]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    text = f.read_text()
    assert "SECRETKEY" not in text and "https://" not in text
    server = json.loads(text)["mcpServers"]
    assert list(server) == ["billing"]
    assert server["billing"]["args"][-9:] == ["--profile", "engineering", "connectors", "serve", "billing",
                                              "--card", "c1", "--stage", "run"]


def test_codex_gets_the_proxy_command_in_dash_c_flags():
    flags = harnesses.mcp_flags(harnesses.get("codex"), ["billing"], profile="p", card_id="c1", stage="run", window="@7")
    assert flags[0] == "-c" and flags[1].startswith("mcp_servers.billing.command=")
    assert flags[2] == "-c" and flags[3].startswith("mcp_servers.billing.args=[") and '"serve"' in flags[3]


def test_another_harness_gets_no_connectors_and_an_error_event(monkeypatch):
    seen = []
    monkeypatch.setattr(harnesses.events, "emit", lambda kind, card_id=None, **d: seen.append((kind, card_id, d)))
    flags = harnesses.mcp_flags(harnesses.get("antigravity"), ["billing"], profile="p", card_id="c1", stage="run", window="@7")
    assert flags == []
    assert seen and seen[0][0] == "error" and "antigravity" in seen[0][2]["message"]


TOOLS = [{"name": "payments_since", "write": False}, {"name": "send_draft", "write": True}]


def test_proxy_hides_write_tools_in_this_slice(monkeypatch):
    calls = []
    monkeypatch.setattr(connectors, "tools", lambda name: TOOLS)
    monkeypatch.setattr(connectors, "call", lambda name, tool, args=None, mode=None: calls.append((name, tool, args, mode)) or "ok")
    assert [t["name"] for t in connectors_proxy.visible_tools("billing")] == ["payments_since"]
    assert connectors_proxy.proxy_call("billing", "payments_since", {"a": 1}) == "ok"
    assert calls == [("billing", "payments_since", {"a": 1}, "stage")]
    for tool in ("send_draft", "nope"):
        with pytest.raises(connectors_proxy.Forbidden):
            connectors_proxy.proxy_call("billing", tool, {})
    assert len(calls) == 1


def test_the_stdio_server_answers_a_hidden_tool_with_a_forbidden_error(monkeypatch):
    monkeypatch.setattr(connectors, "tools", lambda name: TOOLS)
    monkeypatch.setattr(connectors, "call", lambda *a, **k: "ok")
    server = connectors_proxy.build_server("billing")
    from mcp import types
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="send_draft", arguments={}))
    res = asyncio.run(handler(req)).root
    assert res.isError and "forbidden" in res.content[0].text
