"""pl mcp: serve pl read tools and brain tool set over MCP."""
import argparse
import json
import os
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError


def _pl(argv: list[str]) -> subprocess.CompletedProcess:
    """The one subprocess seam of this module; tests fake it."""
    env = dict(os.environ)
    env.setdefault("PL_SOURCE", "mcp")
    return subprocess.run([sys.executable, "-m", "pl", *argv], capture_output=True, text=True, env=env)


def run_pl(argv: list[str]) -> Any:
    """Run a pl command and return parsed JSON or text, raising ToolError on failure."""
    res = _pl(argv)
    if res.returncode != 0:
        err = res.stderr.strip() if res.stderr else f"command failed with exit {res.returncode}"
        raise ToolError(err)
    out = res.stdout.strip() if hasattr(res, "stdout") else str(res).strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return out


def memory_search(query: str, limit: int = 10, pod: str | None = None, card: str | None = None) -> list[dict]:
    from pl import recall
    return recall.search(query, limit=limit, pod=pod, card=card)


def cmd_mcp(argv: list[str], profile: str | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pl mcp", description="serve pl tools over MCP")
    sub = ap.add_subparsers(dest="mcp_cmd")
    p = sub.add_parser("serve")
    p.add_argument("--brain", action="store_true", help="serve the central brain read-only tool set")
    p.add_argument("--memory", action="store_true", help="serve memory_search over the agent memory")
    p.add_argument("--stdio", action="store_true", help="use stdio transport")
    p.add_argument("--conv", default="default", help="conversation id for brain source logging")
    a = ap.parse_args(argv)
    if a.mcp_cmd == "serve":
        if a.brain:
            from pl import brain_tools
            server = brain_tools.build(conv=a.conv)
            server.run(transport="stdio")
        elif a.memory:
            from mcp.server.fastmcp import FastMCP
            server = FastMCP("pl-memory")
            server.tool(name="memory_search", description="Search saved run summaries and approval reasons.")(memory_search)
            server.run(transport="stdio")
        else:
            raise SystemExit("pl mcp: specify --brain or --memory")
