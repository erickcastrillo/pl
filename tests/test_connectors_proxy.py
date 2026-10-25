"""Plan 11 slices 6 and 7: team scoping through the stdio proxy, and write tools gated by access, the card's gate and the kill switch."""
import asyncio
import json
import stat

import pytest

from pl import alerts, board, outward
from pl import config as C
from pl import connectors, connectors_proxy, dispatch, harnesses, outbound

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


def test_proxy_hides_write_tools_from_a_read_connector(monkeypatch):
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


@pytest.fixture
def gated(monkeypatch, machine):
    """billing as a read-write connector; `card` is what the board returns at call time."""
    connectors.path().write_text(TOML.replace('teams = ["billing"]', 'teams = ["billing"]\naccess = "read-write"', 1))
    calls, card = [], {"id": "c1", "metadata": {}}
    monkeypatch.setattr(connectors, "tools", lambda name: TOOLS)
    monkeypatch.setattr(connectors, "call", lambda name, tool, args=None, mode=None: calls.append(tool) or "sent")
    monkeypatch.setattr(board, "fresh_next", lambda: None)
    monkeypatch.setattr(board, "find_card", lambda token: card)
    return calls, card


def write_call(card="c1"):
    return connectors_proxy.proxy_call("billing", "send_draft", {}, card=card, stage="run")


def test_read_access_write_call_is_forbidden_and_opens_an_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(connectors, "tools", lambda name: TOOLS)
    monkeypatch.setattr(connectors, "call", lambda *a, **k: calls.append(a))
    for tool in ("send_draft", "unlisted_tool"):
        with pytest.raises(connectors_proxy.Forbidden, match="forbidden"):
            connectors_proxy.proxy_call("billing", tool, {}, card="c1", stage="run")
    assert calls == []
    assert alerts.get("connector_forbidden:billing:run")


def test_read_write_without_an_approved_gate_is_refused(gated):
    calls, _ = gated
    assert "send_draft" in [t["name"] for t in connectors_proxy.visible_tools("billing")]
    with pytest.raises(connectors_proxy.Forbidden, match="gate_required: approve the card's gate first"):
        write_call()
    assert calls == []


def test_the_same_call_goes_through_after_the_gate_is_approved(gated):
    calls, card = gated
    with pytest.raises(connectors_proxy.Forbidden):
        write_call()
    card["metadata"]["gates"] = {"run": {"approved_by": "me"}}
    assert write_call() == "sent"
    assert calls == ["send_draft"]


def test_the_kill_switch_queues_an_approved_write_call_instead_of_running_it(gated, monkeypatch):
    calls, card = gated
    card["metadata"]["gates"] = {"run": {"approved_by": "me"}}
    monkeypatch.setattr(outward, "state", lambda: {"on": True})
    assert "queued" in write_call()
    assert calls == []
    assert [(i["connector"], i["tool"], i["status"]) for i in outbound._load(outbound.queue_path())] == [("billing", "send_draft", "queued")]


@pytest.mark.parametrize("mode", ["trigger", "reader"])
def test_trigger_mode_never_calls_a_write_tool(monkeypatch, mode):
    connectors.path().write_text(TOML.replace('teams = ["billing"]', 'teams = ["billing"]\nread_tools = ["payments_since"]', 1))
    ran = []
    monkeypatch.setattr(connectors, "_run_signed_in", lambda *a, **k: ran.append(a) or None)
    with pytest.raises(PermissionError, match="write tool"):
        connectors.call("billing", "send_draft", {}, mode=mode)
    assert ran == []
    connectors.call("billing", "payments_since", {}, mode=mode)
    assert len(ran) == 1
