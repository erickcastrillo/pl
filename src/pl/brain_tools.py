"""Central read-only brain: tool set builder for FastMCP("pl-brain")."""
import inspect
import json
import tomllib
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from pl import brain, manager, mcp_serve, profiles
from pl.trackers import mcp as M

# The seam for creating/getting connector sessions
_session = M._session


class ProxyTool(Tool):
    """MCP Tool proxy that bypasses standard Pydantic validation for dynamic arguments."""

    async def run(self, arguments: dict[str, Any], context: Any = None, convert_result: bool = False) -> Any:
        if inspect.iscoroutinefunction(self.fn):
            res = await self.fn(**arguments)
        else:
            res = self.fn(**arguments)
        if convert_result:
            return self.fn_metadata.convert_result(res)
        return res


def _list_connectors() -> dict[str, tuple[dict[str, Any], Path]]:
    """Discover all connectors in every profile and machine config without brain = false."""
    connectors: dict[str, tuple[dict[str, Any], Path]] = {}

    # 1. Profiles
    rows = profiles.list_profiles(check_lock=False)
    for r in rows:
        pdir = Path(r["dir"])
        cfg_file = pdir / "config.toml"
        if cfg_file.is_file():
            try:
                data = tomllib.loads(cfg_file.read_text())
            except Exception:
                data = {}
            for cname, ccfg in (data.get("connectors") or {}).items():
                if isinstance(ccfg, dict) and cname not in connectors:
                    connectors[cname] = (ccfg, pdir)

    # 2. Machine-wide connectors (connectors.toml or machine.toml)
    mdir = manager.machine_dir()
    for fname in ("connectors.toml", "machine.toml"):
        f = mdir / fname
        if f.is_file():
            try:
                data = tomllib.loads(f.read_text())
            except Exception:
                data = {}
            for cname, ccfg in (data.get("connectors") or {}).items():
                if isinstance(ccfg, dict) and cname not in connectors:
                    connectors[cname] = (ccfg, mdir)

    return connectors


def build(conv: str = "default") -> FastMCP:
    """Build the read-only FastMCP('pl-brain') server."""
    server = FastMCP("pl-brain")

    # Read tools
    @server.tool(name="pl_pipelines", description="List every pipeline name on this machine.")
    def pl_pipelines() -> list[str]:
        rows = profiles.list_profiles(check_lock=False)
        res = [r["name"] for r in rows]
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_inbox", description="Every waiting gate across pipelines.")
    def pl_inbox(pipeline: str | None = None) -> Any:
        argv = ["--profile", pipeline, "inbox", "--json"] if pipeline else ["inbox", "--json"]
        res = mcp_serve.run_pl(argv)
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_alerts", description="Open alerts in a pipeline.")
    def pl_alerts(pipeline: str | None = None) -> Any:
        argv = ["--profile", pipeline, "alerts", "--json"] if pipeline else ["alerts", "--json"]
        res = mcp_serve.run_pl(argv)
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_list", description="List cards in a pipeline by column.")
    def pl_list(pipeline: str) -> Any:
        res = mcp_serve.run_pl(["--profile", pipeline, "list", "--json"])
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_card", description="Show a card's sections and metadata.")
    def pl_card(pipeline: str, id: str) -> Any:
        res = mcp_serve.run_pl(["--profile", pipeline, "card", str(id), "--json"])
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_standup", description="Standup summary of what changed in a pipeline.")
    def pl_standup(pipeline: str, since: str | None = None) -> Any:
        argv = ["--profile", pipeline, "standup"]
        if since:
            argv.extend(["--since", since])
        argv.append("--json")
        res = mcp_serve.run_pl(argv)
        brain.record_sources(conv, res)
        return res

    # Proxy connector read tools
    connectors = _list_connectors()
    for cname, (ccfg, _) in connectors.items():
        if ccfg.get("brain") is False:
            continue

        server_cfg = ccfg.get("server") or ccfg
        timeout = float(ccfg.get("timeout") or 60)
        as_is = bool(ccfg.get("mcp_config"))

        try:
            key, sess = _session(server_cfg, timeout=timeout, as_is=as_is)
            tools_res = sess.run(lambda s: s.list_tools())
            tlist = tools_res.tools if hasattr(tools_res, "tools") else (tools_res or [])
        except Exception:
            continue

        read_tools = set(ccfg.get("read_tools") or [])
        write_tools = set(ccfg.get("write_tools") or [])

        for tool_def in tlist:
            hint = getattr(tool_def.annotations, "readOnlyHint", None) if hasattr(tool_def, "annotations") and tool_def.annotations else None
            is_read = (tool_def.name in read_tools) or (hint is True)
            is_write = (tool_def.name in write_tools) or (hint is False)

            # Unmarked and write-marked tools are never exposed
            if not is_read or is_write:
                continue

            full_name = f"{cname}__{tool_def.name}"

            def make_handler(orig_name: str, s_cfg: Any, to: float, as_i: bool):
                async def call_connector(**kwargs: Any) -> Any:
                    _, session = _session(s_cfg, timeout=to, as_is=as_i)
                    raw = session.run(lambda s: s.call_tool(orig_name, kwargs))
                    brain.record_sources(conv, raw)
                    if hasattr(raw, "content"):
                        texts = [c.text for c in raw.content if hasattr(c, "text")]
                        if len(texts) == 1:
                            try:
                                return json.loads(texts[0])
                            except Exception:
                                return texts[0]
                        return texts
                    return raw

                return call_connector

            handler = make_handler(tool_def.name, server_cfg, timeout, as_is)
            pt = ProxyTool.from_function(
                handler,
                name=full_name,
                description=tool_def.description or f"Tool {tool_def.name} on connector {cname}",
            )
            pt.parameters = tool_def.inputSchema or {"type": "object"}
            server._tool_manager._tools[full_name] = pt

    return server
