import sys
import textwrap
import pytest

from pl import alerts
from pl import config as C

SERVER_CODE = textwrap.dedent('''
    import os
    import sys
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    srv = FastMCP("test-pinning-server")
    srv._mcp_server.version = os.environ.get("FAKEMCP_VERSION", "1.0.0")
    mode = os.environ.get("FAKEMCP_MODE", "initial")

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_status() -> str:
        """Read server status."""
        return "status_ok"

    if mode == "changed_schema":
        @srv.tool()
        def write_data(payload: str, extra: int = 1) -> str:
            """Write data with extra parameter."""
            return f"written: {payload} extra: {extra}"
    else:
        @srv.tool()
        def write_data(payload: str) -> str:
            """Write data."""
            return f"written: {payload}"

    if mode == "added_tool":
        @srv.tool()
        def danger_action(level: int = 1) -> str:
            """An added dangerous action."""
            return f"danger: {level}"

    srv.run(transport="stdio")
''')


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "profile_state")
    (tmp_path / "machine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profile_state").mkdir(parents=True, exist_ok=True)

    server_file = tmp_path / "server.py"
    server_file.write_text(SERVER_CODE)

    from pl import connectors

    connectors.add(
        "test_conn",
        command=[sys.executable, str(server_file)],
    )
    return server_file


def test_an_added_tool_disables_writes_and_alerts_with_its_name(fake_env, monkeypatch):
    from pl import connectors

    monkeypatch.setenv("FAKEMCP_MODE", "added_tool")
    tools = connectors.tools("test_conn")
    assert any(t["name"] == "danger_action" for t in tools)

    alert = alerts.get("connector_tools:test_conn")
    assert alert is not None
    assert "danger_action" in alert["title"]
    assert "pl connectors approve-tools test_conn" in alert["fix"]

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "write_data", {"payload": "hello"})

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "danger_action", {"level": 2})


def test_a_changed_input_schema_counts_as_a_change(fake_env, monkeypatch):
    from pl import connectors

    monkeypatch.setenv("FAKEMCP_MODE", "changed_schema")
    tools = connectors.tools("test_conn")
    assert any(t["name"] == "write_data" for t in tools)

    alert = alerts.get("connector_tools:test_conn")
    assert alert is not None
    assert "write_data" in alert["title"]

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "write_data", {"payload": "hello", "extra": 5})


def test_approve_tools_re_enables_writes(fake_env, monkeypatch, capsys):
    from pl import connectors

    monkeypatch.setenv("FAKEMCP_MODE", "added_tool")
    connectors.tools("test_conn")

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "write_data", {"payload": "before_approve"})

    diff = connectors.approve_tools("test_conn")
    assert "danger_action" in diff.get("added", [])

    assert alerts.get("connector_tools:test_conn") is None or alerts.get("connector_tools:test_conn").get("resolved_at")

    res = connectors.call("test_conn", "write_data", {"payload": "after_approve"})
    assert "written: after_approve" in str(res)


def test_a_version_other_than_pin_warns_and_counts_as_a_change(fake_env, monkeypatch, capsys):
    from pl import connectors

    connectors.set_pin("test_conn", "1.4.2")

    monkeypatch.setenv("FAKEMCP_VERSION", "1.4.3")
    connectors.tools("test_conn")

    alert = alerts.get("connector_tools:test_conn")
    assert alert is not None
    assert "1.4.3" in alert["title"] or "version" in alert["title"]

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "write_data", {"payload": "foo"})

    capsys.readouterr()
    connectors.list_connectors()
    out = capsys.readouterr().out
    assert "warning" in out.lower()
    assert "1.4.2" in out or "1.4.3" in out


def test_reads_still_work_while_tools_are_unapproved(fake_env, monkeypatch):
    from pl import connectors
    from pl import connectors_proxy

    monkeypatch.setenv("FAKEMCP_MODE", "added_tool")
    connectors.tools("test_conn")

    with pytest.raises(PermissionError):
        connectors.call("test_conn", "write_data", {"payload": "test"})

    with pytest.raises(PermissionError):
        connectors_proxy.call_tool("test_conn", "write_data", {"payload": "test"})

    res = connectors.call("test_conn", "read_status", {})
    assert "status_ok" in str(res)

    res_proxy = connectors_proxy.call_tool("test_conn", "read_status", {})
    assert "status_ok" in str(res_proxy)


def test_dispatch_check_alerts_sweeps_connector_tools(fake_env, monkeypatch):
    from pl import connectors, dispatch

    monkeypatch.setenv("FAKEMCP_MODE", "added_tool")
    connectors.tools("test_conn")
    assert alerts.get("connector_tools:test_conn") is not None

    dispatch.check_alerts([], set(), st={})
    assert alerts.get("connector_tools:test_conn") is not None

    connectors.approve_tools("test_conn")
    dispatch.check_alerts([], set(), st={})
    a = alerts.get("connector_tools:test_conn")
    assert a is None or a.get("resolved_at")
