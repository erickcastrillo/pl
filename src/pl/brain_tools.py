"""Central read-only brain: tool set builder for FastMCP("pl-brain")."""
import inspect
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from pl import brain, commands, manager, mcp_serve, profiles
from pl import config as C
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


SOURCES: list[dict] = []   # code-host reads logged by pl_pr / pl_runs


def _gh(argv: list[str]) -> str:
    args = argv[1:] if argv and argv[0] == "gh" else argv
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, env=C.gh_env())
    if r.returncode != 0:
        raise OSError(r.stderr.strip() if r.stderr else f"gh exited with {r.returncode}")
    return r.stdout


def log_source(source: dict) -> None:
    SOURCES.append(source)


def get_logged_sources() -> list[dict]:
    return list(SOURCES)


def clear_logged_sources() -> None:
    SOURCES.clear()


def pl_pr(url: str) -> dict:
    if not commands.PR_URL_RE.fullmatch(url):
        raise ValueError(f"pl_pr: {url!r} is not a GitHub pull request URL")
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"pl_pr: cannot parse repo and number from {url}")
    repo, number = m.group(1), m.group(2)
    out = _gh(["pr", "view", number, "--repo", repo, "--json", "title,state,mergedAt,mergeCommit,url"])
    data = json.loads(out) if isinstance(out, str) else out
    log_source({"kind": "pr", "url": data.get("url") or url, "title": data.get("title")})
    return data


def pl_runs(repo: str, sha: str) -> list[dict]:
    out = _gh(["run", "list", "--repo", repo, "--commit", sha, "--json", "name,status,conclusion,url,createdAt"])
    data = json.loads(out) if isinstance(out, str) else out
    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else []
    for run in data:
        if isinstance(run, dict):
            log_source({"kind": "run", "url": run.get("url"), "name": run.get("name")})
    return data


def is_feature_deployed(card: dict) -> dict:
    meta = card.get("metadata") or {}
    pr_urls = meta.get("pr_urls") or card.get("pr_urls") or []
    title = card.get("title", "feature")
    if not pr_urls:
        return {
            "deployed": False,
            "answer": f"No pull requests found for {title}.",
            "sources": [],
        }

    deployed_prs = []
    for url in pr_urls:
        try:
            pr = pl_pr(url)
        except (ValueError, OSError):
            continue

        if pr.get("state") == "MERGED" or pr.get("mergedAt"):
            commit = pr.get("mergeCommit")
            sha = commit.get("oid") if isinstance(commit, dict) else (commit or "")
            m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
            repo = m.group(1) if m else ""
            runs = []
            if repo and sha:
                try:
                    runs = pl_runs(repo, sha)
                except OSError:
                    runs = []

            deploy_runs = [r for r in runs if "deploy" in (r.get("name") or "").lower() or r.get("conclusion") == "success"]
            selected_run = deploy_runs[0] if deploy_runs else (runs[0] if runs else None)
            deployed_prs.append((pr, selected_run))

    if deployed_prs:
        pr, run = deployed_prs[0]
        pr_title = pr.get("title") or "PR"
        pr_link = pr.get("url") or pr_urls[0]
        if run:
            run_name = run.get("name") or "deploy run"
            run_link = run.get("url") or ""
            answer = f"{title} is deployed: PR '{pr_title}' ({pr_link}) and deploy run '{run_name}' ({run_link})."
        else:
            answer = f"{title} is merged in PR '{pr_title}' ({pr_link}), but no deploy runs found."
        return {
            "deployed": True,
            "pr": pr,
            "run": run,
            "answer": answer,
            "sources": [s for s in [
                {"kind": "pr", "url": pr_link, "title": pr_title},
                {"kind": "run", "url": run.get("url"), "name": run.get("name")} if run else None,
            ] if s],
        }

    return {
        "deployed": False,
        "answer": f"{title} is not deployed.",
        "sources": [],
    }


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

    @server.tool(name="pl_pr", description="Read a GitHub pull request: title, state, merge commit.")
    def pl_pr_tool(url: str) -> Any:
        res = pl_pr(url)
        brain.record_sources(conv, res)
        return res

    @server.tool(name="pl_runs", description="List the CI runs for a commit of a repo (owner/name).")
    def pl_runs_tool(repo: str, sha: str) -> Any:
        res = pl_runs(repo, sha)
        brain.record_sources(conv, res)
        return res

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
