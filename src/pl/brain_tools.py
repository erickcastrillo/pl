import json
import re
import subprocess

from pl import commands
from pl import config as C

SOURCES: list[dict] = []


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
