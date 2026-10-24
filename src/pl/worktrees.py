"""Automatic worktree cleanup on dispatcher passes and [worktrees] settings."""
import json
from pathlib import Path
import subprocess
import sys

from pl import config as C
from pl.board import col_name
from pl.util import slug_of

_run = subprocess.run


def _list_worktrees(work_dir: Path) -> list[dict]:
    r = _run(["git", "worktree", "list", "--porcelain"], capture_output=True, text=True, cwd=str(work_dir))
    if r.returncode != 0:
        return []
    worktrees = []
    current = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            current["path"] = Path(val)
        elif key == "branch":
            current["branch"] = val.removeprefix("refs/heads/")
        elif key == "HEAD":
            current["head"] = val
    if current:
        worktrees.append(current)
    return worktrees


def _pr_info(url: str) -> dict | None:
    r = _run(["gh", "pr", "view", url, "--json", "state,mergedAt,headRefName"],
             capture_output=True, text=True, env=C.gh_env())
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _is_dirty(path: Path) -> bool:
    r = _run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=str(path))
    return r.returncode != 0 or bool(r.stdout.strip())


def _has_unpushed(path: Path, branch: str | None) -> bool:
    if not branch:
        return False
    ref = f"origin/{branch}"
    r = _run(["git", "rev-parse", "--verify", ref], capture_output=True, text=True, cwd=str(path))
    if r.returncode != 0:
        r_head = _run(["git", "rev-list", "--count", "origin/HEAD..HEAD"], capture_output=True, text=True, cwd=str(path))
        if r_head.returncode != 0:
            r_head = _run(["git", "rev-list", "--count", "origin/main..HEAD"], capture_output=True, text=True, cwd=str(path))
        return r_head.returncode != 0 or int(r_head.stdout.strip() or "0") > 0
    r_diff = _run(["git", "rev-list", "--count", f"{ref}..HEAD"], capture_output=True, text=True, cwd=str(path))
    return r_diff.returncode != 0 or int(r_diff.stdout.strip() or "0") > 0


def _has_live_agent(c: dict) -> bool:
    m = c.get("metadata") or {}
    w = m.get("worker")
    if not w:
        return False
    if w.get("status") in ("alive", "starting"):
        return True
    pane = w.get("pane")
    if pane:
        r = _run(["tmux", "display-message", "-p", "-t", pane, "#S"], capture_output=True, text=True)
        if r.returncode == 0:
            return True
    win = w.get("window")
    if win:
        r = _run(["tmux", "list-panes", "-s", "-t", C.TMUX_SESSION, "-F", "#{window_id}"], capture_output=True, text=True)
        if r.returncode == 0 and win in r.stdout.splitlines():
            return True
    return False


def _find_card(wt: dict, all_cards: list[dict], pr_cache: dict) -> dict | None:
    wt_path = wt["path"].resolve()
    branch = wt.get("branch") or ""
    branch_short = branch.removeprefix("pl/")
    repo_name = Path(C.WORK_DIR).name if C.WORK_DIR else ""

    for c in all_cards:
        m = c.get("metadata") or {}
        if m.get("worktree") and Path(m["worktree"]).resolve() == wt_path:
            return c
        if m.get("branch") and m["branch"] in (branch, branch_short):
            return c

        for url in m.get("pr_urls") or []:
            if url not in pr_cache:
                pr_cache[url] = _pr_info(url)
            pr = pr_cache.get(url)
            if pr and pr.get("headRefName") in (branch, branch_short):
                return c

        slug = slug_of(c)
        clean_dir = wt_path.name
        if repo_name:
            clean_dir = clean_dir.removeprefix(f"{repo_name}-pl-").removeprefix("pl-")
        if branch_short and (branch_short == slug or branch_short.startswith(slug[:20]) or slug.startswith(branch_short)):
            return c
        if clean_dir and (clean_dir == slug or clean_dir.startswith(slug[:20]) or slug.startswith(clean_dir)):
            return c

        cid = str(c.get("id") or "").split("#")[-1]
        if cid:
            parts = {branch_short, clean_dir, *branch_short.split("-"), *clean_dir.split("-")}
            if cid in parts or f"card-{cid}" in parts or f"pl-{cid}" in parts:
                return c

    return None


def sweep(all_cards: list[dict], dry: bool = False) -> None:
    """Safely remove clean worktrees of finished and dropped cards under WORKTREES_DIR."""
    if not (C.WORKTREES or {}).get("cleanup", True):
        return
    if not C.WORKTREES_DIR or not Path(C.WORKTREES_DIR).is_dir():
        return
    if not C.WORK_DIR or not Path(C.WORK_DIR).is_dir():
        return

    wt_base = Path(C.WORKTREES_DIR).resolve()
    pr_cache: dict[str, dict | None] = {}

    for wt in _list_worktrees(C.WORK_DIR):
        try:
            wt_path = wt["path"].resolve()
            if wt_path == wt_base or not wt_path.is_relative_to(wt_base):
                continue
        except Exception:
            continue

        card = _find_card(wt, all_cards, pr_cache)
        if not card:
            continue

        col = card.get("column") or (col_name(card.get("list_id")) if card.get("list_id") else None)
        if col != "Done" or _has_live_agent(card):
            continue

        meta = card.get("metadata") or {}
        pr_urls = meta.get("pr_urls") or []
        is_dropped = bool(meta.get("dropped_at"))

        merged = False
        closed_unmerged = False
        for url in pr_urls:
            if url not in pr_cache:
                pr_cache[url] = _pr_info(url)
            pr = pr_cache.get(url)
            if pr:
                if pr.get("state") == "MERGED" or pr.get("mergedAt"):
                    merged = True
                    break
                elif pr.get("state") == "CLOSED":
                    closed_unmerged = True

        if merged:
            if _is_dirty(wt_path):
                continue
        elif is_dropped or closed_unmerged:
            if _is_dirty(wt_path) or _has_unpushed(wt_path, wt.get("branch")):
                continue
        else:
            continue

        if dry:
            print(f"  would remove worktree {wt_path}")
            continue

        try:
            _run(["git", "worktree", "remove", str(wt_path)], capture_output=True, text=True, cwd=str(C.WORK_DIR), check=True)
            _run(["git", "worktree", "prune"], capture_output=True, text=True, cwd=str(C.WORK_DIR), check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"worktree {wt_path.name}: cleanup failed: {e}", file=sys.stderr)
