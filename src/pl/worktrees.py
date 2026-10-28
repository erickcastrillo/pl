"""pl worktrees list | gc [--yes] [--json]: the worktrees pl-run made in the profile's worktrees folder, and which are safe to remove.
sweep(): the same cleanup on dispatcher passes, with the [worktrees] settings."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import events, ghquota
from pl.agents import registry, worker_status
from pl.board import cards, col_name
from pl.util import mask, slug_of


def _git(argv):
    """The one subprocess seam of this module; tests fake it."""
    r = subprocess.run(["git", *argv], capture_output=True, text=True)
    return r.returncode, r.stdout


def _card_for(name, cs):
    return next((c for c in cs if (s := slug_of(c)) and name and (s.startswith(name) or name.startswith(s))), None)


def agent_hold(c):
    """("held", why) while the card's agent waits at the trust prompt,
    ("running", None) while its agent is alive, else None. Both cleanup paths keep the worktree then."""
    w = (c.get("metadata") or {}).get("worker")
    if w and w.get("trust_wait"):
        return "held", "trust the folder"
    if not w:
        return None
    return ("running", None) if worker_status(w, registry())[0] in ("alive", "starting") else None


def _inspect(path, c):
    """(action, reason) for one worktree folder and the card it belongs to, if any."""
    if c:
        if hold := agent_hold(c):
            return "keep", f"agent {hold[0]}"
        column = col_name(c["list_id"])
        if column != "Done":
            return "keep", f"card {column}"
    if _git(["-C", str(path), "status", "--porcelain"])[1].strip():
        return "keep", "uncommitted changes"
    if _git(["-C", str(path), "log", "HEAD", "--not", "--remotes", "--oneline"])[1].strip():
        return "keep", "unpushed commits"
    return "remove", "finished card" if c else "no card on the board"


def inventory():
    root = C.WORKTREES_DIR
    if not root or not root.is_dir():
        return []
    cs, rows = cards(), []
    for d in sorted(root.iterdir()):
        if not (d / ".git").exists() or "-pl-" not in d.name:
            continue
        c = _card_for(d.name.split("-pl-", 1)[1], cs)
        action, reason = _inspect(d, c)
        rows.append({"name": d.name, "path": str(d), "card": c["id"] if c else None,
                     "column": col_name(c["list_id"]) if c else None, "action": action, "reason": reason})
    return rows


def _size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def _remove(rows):
    """Remove every row marked remove; one line per failure, the rest go on. (removed, failed, freed bytes)"""
    removed = failed = freed = 0
    commons = set()
    for r in rows:
        if r["action"] != "remove":
            continue
        path = Path(r["path"])
        size = _size(path)
        common = _git(["-C", r["path"], "rev-parse", "--git-common-dir"])[1].strip()
        rc, out = _git(["--git-dir", common, "worktree", "remove", r["path"]])
        if rc:
            failed += 1
            print(f"pl worktrees: could not remove {r['name']}: {out.strip() or 'git worktree remove failed'}")
            continue
        events.emit("worktree_cleanup", r["card"], path=r["path"], by="gc")
        removed, freed = removed + 1, freed + size
        commons.add(common)
    for common in commons:
        _git(["--git-dir", common, "worktree", "prune"])
    return removed, failed, freed


def cmd_worktrees(a):
    if a.action == "bootstrap":
        from pl import commands
        if not a.path:
            raise SystemExit("pl worktrees bootstrap: give the new worktree's PATH")
        return commands.cmd_worktrees_bootstrap(a)
    rows = inventory()
    if a.action == "gc" and a.json:
        print(json.dumps(rows, indent=1))
    elif a.action == "list" or not a.yes:
        for r in rows:
            verdict = "remove" if r["action"] == "remove" else f"keep: {r['reason']}"
            print(f"{r['name']:<50} {r['card'] or '-':<28} {r['column'] or '-':<12} {verdict}")
        if a.action == "gc":
            print(f"\n{sum(r['action'] == 'remove' for r in rows)} to remove; nothing changed (pl worktrees gc --yes removes them)")
    if a.action == "gc" and a.yes:
        removed, failed, freed = _remove(rows)
        kept = sum(r["action"] == "keep" for r in rows)
        print(f"removed {removed}, kept {kept}, failed {failed}, freed {freed / 1e6:.1f} MB")


_run = subprocess.run
GIT_TIMEOUT = 60


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


def _worktree_on(work: Path, branch: str) -> Path | None:
    return next((w["path"] for w in _list_worktrees(work) if w.get("branch") == branch), None)


def _stamp() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"


def _set_aside(path: Path, card_id: str) -> str | None:
    """Move a plain folder out of the way, keeping its contents; an empty one is just removed. Returns an error or None."""
    try:
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
            return None
        target = path.with_name(f"{path.name}.stale-{_stamp()}")
        n = 1
        while target.exists() or target.is_symlink():
            n += 1
            target = path.with_name(f"{path.name}.stale-{_stamp()}-{n}")
        os.rename(path, target)
    except OSError as e:
        return f"could not set aside {path}: {e.strerror or e}"
    events.emit("worktree_set_aside", card_id, path=str(path), moved_to=str(target))
    return None


def for_run(c) -> tuple[Path | None, str | None]:
    """(worktree, None) for card c's run agent, reusing its branch or worktree; (None, git's last line) when git fails.
    Only fetch and worktree add touch the repository, never the main checkout's files."""
    work, slug = Path(C.WORK_DIR), slug_of(c)
    branch = f"pl/{slug}"
    if found := _worktree_on(work, branch):
        return found, None
    rework = (c.get("metadata") or {}).get("rework") or {}
    if rework.get("active") and rework.get("prs"):
        head = ((_pr_info(rework["prs"][0]) or {}).get("headRefName") or "")
        if head and not head.startswith("-"):
            branch = head
            if found := _worktree_on(work, branch):
                return found, None
    root = Path(C.WORKTREES_DIR or work.parent)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{work.name}-pl-{slug}"
    if path.exists() or path.is_symlink():
        if err := _set_aside(path, c["id"]):
            return None, err

    def git(*args):
        try:
            return _run(["git", *args], capture_output=True, text=True, cwd=str(work), timeout=GIT_TIMEOUT,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:   # the dispatcher's pass must not wait on a slow remote or a prompt
            return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {GIT_TIMEOUT}s")

    steps = [git("fetch", "origin")]
    if not steps[0].returncode:
        if git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
            steps.append(git("worktree", "add", str(path), branch))
        elif git("rev-parse", "--verify", "--quiet", f"origin/{branch}").returncode == 0:
            steps.append(git("worktree", "add", "-b", branch, str(path), f"origin/{branch}"))
        else:
            steps.append(git("worktree", "add", "-b", branch, str(path), "origin/HEAD"))
    failed = next((r for r in steps if r.returncode), None)
    if failed is None:
        return path, None
    lines = [l.strip() for l in (failed.stderr or "").splitlines() if l.strip()]
    return None, lines[-1] if lines else "git worktree add failed"


def _settled_path() -> Path:
    return Path(C.STATE_DIR) / "pr-settled.json"


def _settled() -> dict:
    """Merged and closed PRs already looked up: their state never changes, so they are asked about once."""
    try:
        return json.loads(_settled_path().read_text())
    except (OSError, ValueError):
        return {}


def _pr_info(url: str) -> dict | None:
    known = _settled()
    if url in known:
        return known[url]
    r = ghquota.gh(_run, ["pr", "view", url, "--json", "state,mergedAt,headRefName"],
                   capture_output=True, text=True, env=C.gh_env())
    if r.returncode != 0:
        return None
    try:
        info = json.loads(r.stdout)
    except Exception:
        return None
    if isinstance(info, dict) and (info.get("state") in ("MERGED", "CLOSED") or info.get("mergedAt")):
        try:
            known[url] = info
            _settled_path().parent.mkdir(parents=True, exist_ok=True)
            _settled_path().write_text(json.dumps(known))
        except OSError:
            pass
    return info


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


def _find_card(wt: dict, all_cards: list[dict], pr_cache: dict) -> dict | None:
    wt_path = wt["path"].resolve()
    branch = wt.get("branch") or ""
    branch_short = branch.removeprefix("pl/")
    repo_name = Path(C.WORK_DIR).name if C.WORK_DIR else ""

    for c in all_cards:   # the free matches first, over every card: a PR lookup costs a GitHub call
        m = c.get("metadata") or {}
        if m.get("worktree") and Path(m["worktree"]).resolve() == wt_path:
            return c
        if m.get("branch") and m["branch"] in (branch, branch_short):
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

    for c in all_cards:
        for url in (c.get("metadata") or {}).get("pr_urls") or []:
            if url not in pr_cache:
                pr_cache[url] = _pr_info(url)
            pr = pr_cache.get(url)
            if pr and pr.get("headRefName") in (branch, branch_short):
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

        if hold := agent_hold(card):
            why = f" ({hold[1]})" if hold[1] else ""
            print(f"worktree {wt_path} {card['id']} skipped: agent {hold[0]}{why}")
            continue

        col = card.get("column") or (col_name(card.get("list_id")) if card.get("list_id") else None)
        if col != "Done":
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
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            if wt_path.exists():
                print(f"worktree {wt_path}: removed from git but the folder is still there; delete it by hand")
            events.emit("worktree_cleanup", card["id"], path=str(wt_path), by="sweep")
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"worktree {wt_path.name}: cleanup failed: {e}", file=sys.stderr)


def check_missing(all_cards: list[dict]) -> None:
    """One worktree_removed event for each unfinished card whose recorded worktree folder is gone."""
    gone = [(c, p) for c in all_cards
            if (p := (c.get("metadata") or {}).get("worktree")) and not Path(p).exists()
            and not (c.get("metadata") or {}).get("dropped_at")
            and (c.get("column") or col_name(c["list_id"])) != "Done"]
    if not gone:
        return
    log = events._read()
    for c, p in gone:
        if any(e["kind"] == "worktree_removed" and e.get("card") == c["id"] and e.get("path") == p for e in log):
            continue
        cleanup = next((e for e in reversed(log) if e["kind"] == "worktree_cleanup" and e.get("path") == p), None)
        events.emit("worktree_removed", c["id"], path=p,
                    message=f"removed by pl cleanup at {cleanup['ts'][:16]}" if cleanup else "removed outside pl")


LISTS = ("copy", "link")


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return int(text) if re.fullmatch(r"\d+", text) else text


def _parse_yaml(text):
    """The subset the manifest uses: `key: scalar`, `key: [a, b]` and `key:` followed by `- item` lines."""
    data, key = {}, None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key in LISTS:
            data[key].append(_scalar(line.lstrip()[2:]))
            continue
        name, _, value = line.partition(":")
        key, value = name.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
        elif value:
            data[key] = _scalar(value)
        else:
            data[key] = []
    return data


def validate_path(repo_root, rel):
    p = Path(str(rel))
    if not str(rel).strip() or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{rel}: not a path inside the repository; the whole manifest is rejected, nothing applied")
    root = Path(repo_root).resolve()
    if not (root / p).resolve().is_relative_to(root):
        raise ValueError(f"{rel}: resolves outside the repository; the whole manifest is rejected, nothing applied")
    return p


def load_manifest(repo_root):
    repo_root = Path(repo_root)
    yaml_file, include = repo_root / ".pl" / "worktree.yaml", repo_root / ".worktreeinclude"
    if yaml_file.is_file():
        data = _parse_yaml(yaml_file.read_text())
    elif include.is_file():
        data = {"copy": [x.strip() for x in include.read_text().splitlines() if x.strip() and not x.lstrip().startswith("#")]}
    else:
        data = {}
    manifest = {"copy": [], "link": [], "setup": None, "timeout": None, **data}
    for name in LISTS:
        for rel in manifest[name]:
            validate_path(repo_root, rel)
    return manifest


DEFAULT_SETUP_TIMEOUT = 300
TAIL_LINES = 20
_popen = subprocess.Popen


def parse_timeout(value):
    if value is None:
        return DEFAULT_SETUP_TIMEOUT
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError(f"invalid timeout '{value}': timeout must be a positive integer")


def log_tail(log_path, lines=TAIL_LINES):
    try:
        with open(log_path, errors="replace") as f:
            return "".join(deque(f, maxlen=lines)).rstrip("\n")
    except OSError:
        return ""


def _stop_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def run_setup(command, timeout, worktree_path, log_path):
    """Run the setup command in the worktree with stdout and stderr in log_path; return the failure reason, or None."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as log:
        proc = _popen(["sh", "-c", command], cwd=str(worktree_path), stdout=log, stderr=subprocess.STDOUT,
                      start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_group(proc)
            return f"Bootstrap setup timed out after {timeout}s"
    return None if code == 0 else f"Bootstrap setup failed with exit code {code}"


def bootstrap_worktree(main_repo, worktree_path, dry_run=False, log_path=None, failures=None):
    """Apply the manifest to a worktree and return one line per path; nothing is staged, and no file contents are logged.
    A failed setup command adds {"reason", "output"} to `failures`; it does not raise."""
    main_repo, worktree_path = Path(main_repo), Path(worktree_path)
    manifest = load_manifest(main_repo)
    timeout = parse_timeout(manifest["timeout"])
    notes = []
    for action in LISTS:
        for rel in manifest[action]:
            src, dst = main_repo / rel, worktree_path / rel
            if not (src.exists() or src.is_symlink()):
                notes.append(f"skip {rel}: not found in main checkout")
            elif dry_run:
                notes.append(f"copy {rel}" if action == "copy" else f"link {rel} -> {src}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if action == "link":
                        os.symlink(src, dst)
                    elif src.is_dir():
                        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except OSError as e:
                    notes.append(f"skip {rel}: {e.strerror or e}")
                    continue
                notes.append(f"copy {rel}" if action == "copy" else f"link {rel} -> {src}")
    command = manifest["setup"]
    if not command or not str(command).strip():
        return notes
    if dry_run:
        notes.append(f'setup: "{command}" (timeout: {timeout}s)')
        return notes
    log_path = log_path or worktree_path / ".pl-setup.log"
    reason = run_setup(str(command), timeout, worktree_path, log_path)
    if reason and failures is not None:
        failures.append({"reason": reason, "output": mask(log_tail(log_path))})
    return notes


def _git_in(argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True)


def main_checkout(cwd):
    r = _git_in(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    return Path(r.stdout.strip()).parent if r.returncode == 0 else Path(cwd)
