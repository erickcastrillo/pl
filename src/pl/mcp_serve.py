"""pl mcp: serve pl read tools and brain tool set over MCP."""
import argparse
import json
import os
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


def _pl(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """The one subprocess seam of this module; tests fake it."""
    env = dict(os.environ)
    env.setdefault("PL_SOURCE", "mcp")
    return subprocess.run([sys.executable, "-m", "pl", *argv], capture_output=True, text=True, env=env, input=stdin)


def run_pl(argv: list[str], stdin: str | None = None) -> Any:
    """Run a pl command and return parsed JSON or text, raising ToolError on failure."""
    res = _pl(argv) if stdin is None else _pl(argv, stdin)
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


WRITABLE_SECTIONS = ("SPEC", "DESIGN", "PLAN")


def _profile_args(profile: str | None) -> tuple[str, ...]:
    return ("--profile", profile) if profile else ()


def _read(profile: str | None, *argv: str) -> Any:
    return run_pl([*_profile_args(profile), *argv, "--json"])


def build(profile: str | None = None) -> FastMCP:
    """The read tools: each runs the matching `pl ... --json` and returns its document."""
    server = FastMCP("pl")

    @server.tool(description="Gates waiting for you across profiles.")
    def pl_inbox(profile: str | None = profile) -> Any:
        return _read(profile, "inbox")

    @server.tool(description="The board: every open card.")
    def pl_list(profile: str | None = profile, all: bool = False) -> Any:
        return _read(profile, "list", *(["--all"] if all else []))

    @server.tool(description="One card with its metadata.")
    def pl_card(id: str, profile: str | None = profile) -> Any:
        return _read(profile, "card", id)

    @server.tool(description="One section of a card: SPEC, DESIGN, PLAN, INPUT or REVIEW NOTES.")
    def pl_section(id: str, name: str, profile: str | None = profile) -> Any:
        return _read(profile, "section", id, name)

    @server.tool(description="Alerts, open ones unless all is true.")
    def pl_alerts(profile: str | None = profile, all: bool = False) -> Any:
        return _read(profile, "alerts", *(["--all"] if all else []))

    @server.tool(description="The standup: what happened since 24h (default), 90m, 7d or a date.")
    def pl_standup(profile: str | None = profile, since: str | None = None) -> Any:
        return _read(profile, "standup", *(["--since", since] if since else []))

    @server.tool(description="The activity feed, newest first: filter by level (info, warning, error), kind and since "
                             "(24h, 90m, 7d or a date).")
    def pl_activity(profile: str | None = profile, level: str | None = None, kind: str | None = None,
                    since: str | None = None) -> Any:
        opts = [x for flag, v in (("--level", level), ("--kind", kind), ("--since", since)) if v for x in (flag, v)]
        out = _read(profile, "activity", *opts)
        if isinstance(out, str):
            return [json.loads(ln) for ln in out.splitlines()]
        return [out] if out else []

    @server.tool(description="Token usage since 24h (default), 90m, 7d or a date, grouped by card, account, model or loop.")
    def pl_usage(profile: str | None = profile, since: str | None = None, by: str = "account") -> Any:
        return _read(profile, "usage", *(["--since", since] if since else []), "--by", by)

    @server.tool(description="File an idea on the board. Returns what pl printed.")
    def pl_idea(text: str, title: str | None = None, repo: str | None = None, profile: str | None = profile) -> Any:
        return run_pl([*_profile_args(profile), "idea", text, *(["--title", title] if title else []),
                       *(["--repo", repo] if repo else [])])

    @server.tool(description="Replace a card's SPEC, DESIGN or PLAN with text.")
    def pl_section_write(id: str, name: str, text: str, profile: str | None = profile) -> Any:
        name = " ".join(name.split()).upper()
        if name not in WRITABLE_SECTIONS:
            raise ToolError(f"pl_section_write: only {', '.join(WRITABLE_SECTIONS)} can be written")
        return run_pl([*_profile_args(profile), "section", id, name, "--from", "-"], text)

    @server.tool(description="Approve a card's gate. An outward action stays pending until the owner confirms.")
    def pl_approve(id: str, profile: str | None = profile) -> Any:
        return run_pl([*_profile_args(profile), "approve", id])

    @server.tool(description="Every agent: built-in and this profile's.")
    def pl_agents_list(profile: str | None = profile) -> Any:
        return _read(profile, "agents", "list")

    @server.tool(description="One agent's full definition.")
    def pl_agents_show(name: str, profile: str | None = profile) -> Any:
        return _read(profile, "agents", "show", name)

    @server.tool(description="Set the agent a pipeline stage runs.")
    def pl_agents_use(stage: str, agent: str, pipeline: str | None = None, profile: str | None = profile) -> Any:
        return run_pl([*_profile_args(profile), "agents", "use", stage, agent,
                       *(["--pipeline", pipeline] if pipeline else []), "--json"])

    return server


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
        elif a.stdio:
            build(profile).run(transport="stdio")
        else:
            raise SystemExit("pl mcp: specify --stdio, --brain or --memory")
