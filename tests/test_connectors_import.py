"""Plan 11 slice 8: import MCP servers from Claude Code, and roles by name only."""
import argparse
import builtins
import json
import sys
import textwrap
from pathlib import Path

import pytest

from pl import config as C
from pl import connectors

KEY = "sk-live-0123456789abcdef"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    (tmp_path / "machine").mkdir()
    acct, work = tmp_path / ".claude-work", tmp_path / "repo"
    acct.mkdir()
    work.mkdir()
    monkeypatch.setattr(C, "PROFILES", {"work": acct})
    monkeypatch.setattr(C, "WORK_DIR", work)
    monkeypatch.setattr(C, "PIPELINE", {"name": "billing"})
    (acct / ".claude.json").write_text(json.dumps({"mcpServers": {
        "ledger": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": f"Bearer {KEY}"}},
        "files": {"command": "files-mcp", "args": ["--root", "/srv"], "env": {"API_KEY": KEY}}}}))
    (work / ".mcp.json").write_text(json.dumps({"mcpServers": {"local": {"command": "local-mcp"}}}))
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _args(**kw):
    return argparse.Namespace(connectors_cmd="import", source="claude", add=None, team=None, role=None, yes=True, json=False, **kw)


def test_import_lists_claude_servers(capsys):
    connectors.cmd_connectors(_args())
    out = capsys.readouterr().out
    assert all(n in out for n in ("ledger", "files", "local"))
    assert KEY not in out and "--root" not in out


def test_import_json_lists_every_server_without_values(capsys):
    connectors.cmd_connectors(argparse.Namespace(**{**vars(_args()), "json": True}))
    out = capsys.readouterr().out
    assert sorted(r["name"] for r in json.loads(out)) == ["files", "ledger", "local"]
    assert KEY not in out


def test_adding_an_import_copies_no_key_into_connectors_toml():
    a = argparse.Namespace(**{**vars(_args()), "add": "ledger", "team": ["billing"], "role": ["payments"]})
    connectors.cmd_connectors(a)
    text = connectors.path().read_text()
    assert KEY not in text and "Authorization" not in text and "API_KEY" not in text
    c = connectors.get("ledger")
    assert c["mcp_config"].endswith(".claude.json") and c["server"] == "ledger"
    assert c["teams"] == ["billing"] and c["role"] == ["payments"]
    assert "url" not in c and "env" not in c


def test_adding_an_unknown_server_or_role_exits_2(capsys):
    for kw in ({"add": "nope"}, {"add": "ledger", "role": ["wizard"]}):
        with pytest.raises(SystemExit) as e:
            connectors.cmd_connectors(argparse.Namespace(**{**vars(_args()), **kw}))
        assert e.value.code == 2
    assert not connectors.path().exists()


def test_codex_import_exits_2(capsys):
    with pytest.raises(SystemExit) as e:
        connectors.cmd_connectors(argparse.Namespace(**{**vars(_args()), "source": "codex"}))
    assert e.value.code == 2
    assert "Codex's TOML MCP file is not supported yet" in capsys.readouterr().err


def test_a_credentials_file_is_never_opened(env, monkeypatch, capsys):
    (env / ".claude-work" / ".credentials.json").write_text(json.dumps({"mcpServers": {"stolen": {"command": "x"}}}))
    opened = []
    real_open, real_read = builtins.open, Path.read_text
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])
    assert connectors.claude_servers_in(env / ".claude-work" / ".credentials.json") == {}
    connectors.cmd_connectors(_args())
    assert "stolen" not in capsys.readouterr().out
    assert not [p for p in opened if ".credentials" in p]


FAKE = textwrap.dedent('''
    import sys
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    srv = FastMCP("fake")
    tool = sys.argv[1]
    @srv.tool(name=tool, annotations=ToolAnnotations(readOnlyHint=True))
    def since(day: str) -> str:
        return f"{tool}:{day}"
    srv.run(transport="stdio")
''')


def _config(tmp_path, server_tool):
    server = tmp_path / "fake.py"
    server.write_text(FAKE)
    connectors.path().write_text(
        f'[connectors.pay]\ncommand = ["{sys.executable}", "{server}", "{server_tool}"]\nrole = ["payments"]\n'
        f'teams = ["billing"]\ntools = {{ payments_since = "{server_tool}" }}\n')


def test_swapping_the_server_behind_payments_changes_only_config_and_the_ref(env):
    ref = {"refs": {"payments": "pay_42"}}
    _config(env, "list_charges")
    first = connectors.call_role("payments", "payments_since", day="mon")
    _config(env, "fetch_payments")
    second = connectors.call_role("payments", "payments_since", day="mon")
    assert (first, second) == ("list_charges:mon", "fetch_payments:mon")
    assert ref["refs"]["payments"] == "pay_42"


def test_role_returns_the_single_enabled_connector_for_the_team(env):
    connectors.path().write_text(
        '[connectors.a]\ncommand = "a"\nrole = ["payments"]\nteams = ["billing"]\n'
        '[connectors.b]\ncommand = "b"\nrole = ["payments"]\nteams = ["sales"]\n'
        '[connectors.c]\ncommand = "c"\nrole = ["payments"]\nteams = ["billing"]\nenabled = false\n')
    assert connectors.role("payments")[0] == "a"
    with pytest.raises(SystemExit):
        connectors.role("email")


def test_two_connectors_filling_one_role_for_a_team_is_an_error(env):
    connectors.path().write_text(
        '[connectors.a]\ncommand = "a"\nrole = ["payments"]\nteams = ["billing"]\n'
        '[connectors.b]\ncommand = "b"\nrole = ["payments"]\nteams = ["billing"]\n')
    with pytest.raises(SystemExit, match="more than one"):
        connectors.role("payments")


def test_a_harness_file_connector_expands_env_and_opens_the_server(env, monkeypatch):
    f = env / "h.json"
    f.write_text(json.dumps({"mcpServers": {"s": {"command": "tool-mcp", "args": ["${PL_T_ARG}"]}}}))
    monkeypatch.setenv("PL_T_ARG", "xyz")
    cfg = connectors.resolve_server({"mcp_config": str(f), "server": "s"})
    assert cfg["command"] == "tool-mcp" and cfg["args"] == ["xyz"]
