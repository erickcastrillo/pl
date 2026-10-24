"""Central read-only brain: tool set builder for FastMCP("pl-brain")."""
import inspect
import json
import os
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
        res = await self.fn(**arguments) if inspect.iscoroutinefunction(self.fn) else self.fn(**arguments)
        return self.fn_metadata.convert_result(res) if convert_result else res


def _list_connectors() -> dict[str, dict[str, Any]]:
    connectors: dict[str, dict[str, Any]] = {}
    for r in profiles.list_profiles(check_lock=False):
        cfg = Path(r["dir"]) / "config.toml"
        if cfg.is_file():
            try:
                for cname, ccfg in (tomllib.loads(cfg.read_text()).get("connectors") or {}).items():
                    if isinstance(ccfg, dict) and cname not in connectors:
                        connectors[cname] = ccfg
            except Exception:
                pass
    mdir = manager.machine_dir()
    for fname in ("connectors.toml", "machine.toml"):
        f = mdir / fname
        if f.is_file():
            try:
                for cname, ccfg in (tomllib.loads(f.read_text()).get("connectors") or {}).items():
                    if isinstance(ccfg, dict) and cname not in connectors:
                        connectors[cname] = ccfg
            except Exception:
                pass
    return connectors


def build(conv: str = "default") -> FastMCP:
    """Build the read-only FastMCP('pl-brain') server."""
    server = FastMCP("pl-brain")

    def _call(argv: list[str]) -> Any:
        res = mcp_serve.run_pl(argv)
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_pipelines", description="List every pipeline name on this machine.")
    def pl_pipelines() -> list[str]:
        res = [r["name"] for r in profiles.list_profiles(check_lock=False)]
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_inbox", description="Every waiting gate across pipelines.")
    def pl_inbox(pipeline: str | None = None) -> Any:
        return _call(["--profile", pipeline, "inbox", "--json"] if pipeline else ["inbox", "--json"])

    @server.tool(name="pl_alerts", description="Open alerts in a pipeline.")
    def pl_alerts(pipeline: str | None = None) -> Any:
        return _call(["--profile", pipeline, "alerts", "--json"] if pipeline else ["alerts", "--json"])

    @server.tool(name="pl_list", description="List cards in a pipeline by column.")
    def pl_list(pipeline: str) -> Any:
        return _call(["--profile", pipeline, "list", "--json"])

    @server.tool(name="pl_card", description="Show a card's sections and metadata.")
    def pl_card(pipeline: str, id: str) -> Any:
        return _call(["--profile", pipeline, "card", str(id), "--json"])

    @server.tool(name="pl_standup", description="Standup summary of what changed in a pipeline.")
    def pl_standup(pipeline: str, since: str | None = None) -> Any:
        return _call(["--profile", pipeline, "standup", *(["--since", since] if since else []), "--json"])

    @server.tool(name="pl_file_card", description="File a card in a named pipeline (Inbox or Triage).")
    def pl_file_card(pipeline: str, title: str, text: str) -> Any:
        old = os.environ.get("PL_SOURCE")
        try:
            os.environ["PL_SOURCE"] = "brain"
            return _call(["--profile", pipeline, "idea", text, "--title", title])
        finally:
            if old is None:
                os.environ.pop("PL_SOURCE", None)
            else:
                os.environ["PL_SOURCE"] = old

    # Proxy connector read tools
    for cname, ccfg in _list_connectors().items():
        if ccfg.get("brain") is False:
            continue

        scfg = ccfg.get("server") or ccfg
        to = float(ccfg.get("timeout") or 60)
        as_is = bool(ccfg.get("mcp_config"))

        try:
            _, sess = _session(scfg, timeout=to, as_is=as_is)
            tools_res = sess.run(lambda s: s.list_tools())
            tlist = getattr(tools_res, "tools", tools_res) or []
        except Exception:
            continue

        read_tools, write_tools = set(ccfg.get("read_tools") or []), set(ccfg.get("write_tools") or [])
        for td in tlist:
            hint = getattr(getattr(td, "annotations", None), "readOnlyHint", None)
            if not ((td.name in read_tools) or (hint is True)) or ((td.name in write_tools) or (hint is False)):
                continue

            full_name = f"{cname}__{td.name}"

            def make_handler(name: str):
                async def call_connector(**kwargs: Any) -> Any:
                    _, session = _session(scfg, timeout=to, as_is=as_is)
                    raw = session.run(lambda s: s.call_tool(name, kwargs))
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

            pt = ProxyTool.from_function(
                make_handler(td.name),
                name=full_name,
                description=td.description or f"Tool {td.name} on connector {cname}",
            )
            pt.parameters = td.inputSchema or {"type": "object"}
            server._tool_manager._tools[full_name] = pt

    return server
