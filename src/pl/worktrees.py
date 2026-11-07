"""pl worktrees [list] | gc [--yes] [--json]: the worktrees pl-run made in the profile's worktrees folder, and which are safe to remove.
sweep(): the same cleanup on dispatcher passes, with the [worktrees] settings."""
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from pl import config as C
from pl import events, ghquota
from pl.agents import registry, worker_status
from pl.board import cards, col_name
from pl.util import mask, slug_of


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


_STALE = re.compile(r"\.stale-(\d{8})-(\d{6})(?:-\d+)?$")


def _verdict(path, branch, c):
    """(action, reason) for one worktree: the one decision the report, gc and the dispatcher share. Cheapest check first."""
    if C.WORK_DIR and multi_repo(Path(C.WORK_DIR)):
        return "keep", "several repositories in the work folder"
    if not c:
        return "keep", "no card on this board"
    if hold := agent_hold(c):
        return "keep", f"agent {hold[0]}"
    if (column := _column(c)) != "Done":
        return "keep", f"card {column}"
    rc, out = _in(path, "status", "--porcelain")
    if rc:
        return "keep", "cannot inspect"
    if out.strip():
        return "keep", "uncommitted changes"
    dropped = bool((c.get("metadata") or {}).get("dropped_at"))
    prs = _card_prs(c, "" if dropped else branch)
    if not dropped and not any(_merged(p) or p.get("state") == "CLOSED" for p in prs):
        return "keep", "no merged PR on the card"
    unpushed = _unpushed(path, prs)
    if unpushed is None:
        return "keep", "cannot inspect"
    return ("keep", "unpushed commits") if unpushed else ("remove", "finished card")


def _merged(pr):
    return pr.get("state") == "MERGED" or bool(pr.get("mergedAt"))


def _card_prs(c, branch):
    urls = (c.get("metadata") or {}).get("pr_urls") or []
    if urls:
        return [pr for url in urls if (pr := _pr_info(url))]
    return [pr] if branch and (pr := _pr_for_branch(branch)) else []


def _unpushed(path, prs):
    """True/False, or None when git cannot say. A HEAD that is a merged PR's last commit is saved, even when its branch is gone."""
    rc, head = _in(path, "rev-parse", "HEAD")
    if not rc and head.strip() and any(p.get("headRefOid") == head.strip() for p in prs if _merged(p)):
        return False
    rc, out = _in(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if rc or not out.strip().isdigit():
        return None
    return int(out) > 0


def _stale_row(d, name, cs):
    c = _match_card(d.with_name(_STALE.sub("", name)), "", cs)
    record = _read_stale_record(d)
    left = _days_left(name)
    action = "keep"
    if record is None or not _is_complete(record):
        reason = "stale, state unknown"
    elif record["uncommitted"] or record["unpushed"]:
        reason = "stale, unsaved work"
    elif left:
        reason = f"stale, {left} days left"
    else:
        action, reason = "remove", "stale, saved and past retention"
    return {"name": name, "path": str(d), "card": c["id"] if c else None, "column": _column(c) if c else None,
            "action": action, "reason": reason, "stale": True, "expired": not left}


def _is_complete(record):
    """Only a record shaped the way pl writes it can vouch for a folder; anything else fails closed."""
    unpushed = record.get("unpushed")
    return isinstance(record.get("uncommitted"), bool) and type(unpushed) is int and unpushed >= 0


def _days_left(name):
    m = _STALE.search(name)
    set_aside = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - set_aside).total_seconds() / 86400
    return max(0, math.ceil((C.WORKTREES or {}).get("stale_days", 14) - age))


def inventory(cs=None, sizes=True):
    root = C.WORKTREES_DIR
    if not root or not root.is_dir():
        return []
    cs, rows = cards() if cs is None else cs, []
    branches = {w["path"].resolve(): w.get("branch") or "" for w in _list_worktrees(C.WORK_DIR)} if C.WORK_DIR else {}
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        if _STALE.search(d.name):
            row = _stale_row(d, d.name, cs)
        elif (d / ".git").exists() or d.resolve() in branches:
            c = _match_card(d, branches.get(d.resolve(), ""), cs)
            action, reason = _verdict(d, branches.get(d.resolve(), ""), c)
            row = {"name": d.name, "path": str(d), "card": c["id"] if c else None,
                   "column": _column(c) if c else None, "action": action, "reason": reason}
        else:
            continue
        if sizes:
            row["size"] = _size(d)
        rows.append(row)
    return rows


def _size(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                total += st.st_size
    return total


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _totals(rows):
    return {"folders": len(rows), "size": sum(r.get("size", 0) for r in rows),
            "removable": sum(r.get("size", 0) for r in rows if r["action"] == "remove")}


def _remove(rows, by="gc"):
    """Remove every row marked remove; one line per failure, the rest go on. (removed, failed, freed bytes)"""
    removed = failed = freed = 0
    bases = set()
    for r in rows:
        if r["action"] != "remove":
            continue
        path = Path(r["path"])
        if r.get("stale"):
            if _remove_stale(path):
                events.emit("worktree_cleanup", r["card"], path=r["path"], by=by)
                removed, freed = removed + 1, freed + (r.get("size") or 0)
            else:
                failed += 1
            continue
        size = (r["size"] if r.get("size") is not None else _size(path)) if by == "gc" else 0
        cwd, prefix = _git_base(path)
        rc, out = _in(cwd, *prefix, "worktree", "remove", r["path"])
        if rc:
            failed += 1
            print(f"worktree {r['name']}: cleanup failed: {out.strip() or 'git worktree remove failed'}", file=sys.stderr)
            continue
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            print(f"worktree {path}: removed from git but the folder is still there; delete it by hand")
        events.emit("worktree_cleanup", r["card"], path=r["path"], by=by)
        removed, freed = removed + 1, freed + size
        bases.add((cwd, tuple(prefix)))
    for cwd, prefix in bases:
        _in(cwd, *prefix, "worktree", "prune")
    return removed, failed, freed


def _remove_stale(path):
    """Delete one set-aside folder. Only a real folder directly under the worktrees folder with a stale name; True when it is gone."""
    root = Path(C.WORKTREES_DIR) if C.WORKTREES_DIR else None
    if root is None or path.parent != root or path.is_symlink() or not path.is_dir() or not _STALE.search(path.name):
        print(f"worktree {path.name}: not deleted, it is not a set-aside folder of the worktrees folder", file=sys.stderr)
        return False
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        print(f"worktree {path}: could not delete the folder; delete it by hand")
        return False
    return True


def _git_base(path):
    """Where to run worktree commands: the work folder when it is a repository, else the worktree's own common git dir."""
    if C.WORK_DIR and Path(C.WORK_DIR).is_dir():
        return C.WORK_DIR, ()
    common = _in(path, "rev-parse", "--git-common-dir")[1].strip()
    return C.WORKTREES_DIR, ("--git-dir", common)


def _gc_stale(rows, yes):
    """Old folders with no saved-state record: listed, and deleted only with --yes. Nothing else is touched."""
    chosen = [{**r, "action": "remove"} for r in rows if r.get("stale") and r["reason"] == "stale, state unknown" and r["expired"]]
    for r in chosen:
        print(f"{r['name']:<50} {_human(r['size']):>9} {r['card'] or '-'}")
    if not yes:
        print(f"\n{len(chosen)} to remove; nothing changed (pl worktrees gc --stale --yes removes them)")
        return
    removed, failed, freed = _remove(chosen, by="gc")
    for r in chosen:
        if not Path(r["path"]).exists():
            print(f"removed {r['name']}")
    print(f"removed {removed}, failed {failed}, freed {_human(freed)}")


def cmd_worktrees(a):
    if a.action == "bootstrap":
        from pl import commands
        if not a.path:
            raise SystemExit("pl worktrees bootstrap: give the new worktree's PATH")
        return commands.cmd_worktrees_bootstrap(a)
    rows = inventory()
    if a.action == "gc" and getattr(a, "stale", False):
        return _gc_stale(rows, a.yes)
    if a.action == "gc" and a.json:
        print(json.dumps(rows, indent=1))
    elif a.action == "list" and a.json:
        print(json.dumps({"rows": rows, "total": _totals(rows)}, indent=1))
    elif a.action == "list" or not a.yes:
        for r in rows:
            verdict = "remove" if r["action"] == "remove" else f"keep: {r['reason']}"
            print(f"{r['name']:<50} {_human(r['size']):>9} {r['card'] or '-':<28} {r['column'] or '-':<12} {verdict}")
        t = _totals(rows)
        print(f"{t['folders']} folders, {_human(t['size'])}, {_human(t['removable'])} can be removed")
        if a.action == "gc":
            print(f"\n{sum(r['action'] == 'remove' for r in rows)} to remove; nothing changed (pl worktrees gc --yes removes them)")
    if a.action == "gc" and a.yes:
        removed, failed, freed = _remove(rows)
        kept = sum(r["action"] == "keep" for r in rows)
        print(f"removed {removed}, kept {kept}, failed {failed}, freed {freed / 1e6:.1f} MB")


_run = subprocess.run
GIT_TIMEOUT = 60


def _in(cwd, *args):
    """(returncode, stdout) of one git command in cwd; a timeout is 124, which reads as cannot inspect."""
    try:
        r = _run(["git", *args], capture_output=True, text=True, cwd=str(cwd), timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, ""
    return r.returncode, r.stdout or ""


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


RECORD = ".pl-set-aside.json"


def _write_stale_record(path: Path, card_id: str) -> Path | None:
    """Save what the folder held before it is renamed, so a later cleanup can tell saved work from unsaved. None when git cannot say."""
    if not (path / ".git").exists():
        return None
    got = [_in(path, *a) for a in (("branch", "--show-current"), ("rev-parse", "HEAD"), ("status", "--porcelain"),
                                   ("rev-list", "--count", "HEAD", "--not", "--remotes"))]
    if any(rc for rc, _ in got) or not got[3][1].strip().isdigit():
        return None
    record = {"card": card_id, "branch": got[0][1].strip(), "head": got[1][1].strip(), "uncommitted": bool(got[2][1].strip()),
              "unpushed": int(got[3][1]), "at": datetime.now(timezone.utc).isoformat()}
    target = path / RECORD
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
    except OSError:
        return None
    return target


def _read_stale_record(folder: Path) -> dict | None:
    try:
        record = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


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
        record = _write_stale_record(path, card_id)
        try:
            os.rename(path, target)
        except OSError:
            if record:
                record.unlink(missing_ok=True)
            raise
    except OSError as e:
        return f"could not set aside {path}: {e.strerror or e}"
    events.emit("worktree_set_aside", card_id, path=str(path), moved_to=str(target))
    return None


def multi_repo(work: Path) -> bool:
    """True when work is not a git repo but holds git repos."""
    if (work / ".git").exists():
        return False
    try:
        return any((d / ".git").exists() for d in work.iterdir() if d.is_dir())
    except OSError:
        return False


def planned_path(c) -> Path:
    work = Path(C.WORK_DIR)
    return Path(C.WORKTREES_DIR or work.parent) / f"{work.name}-pl-{slug_of(c)}"


def for_run(c) -> tuple[Path | None, str | None]:
    """(worktree, None) for card c's run agent, reusing its branch or worktree; (None, git's last line) when git fails.
    Only fetch and worktree add touch the repository, never the main checkout's files."""
    work, slug = Path(C.WORK_DIR), slug_of(c)
    branch = f"pl/{slug}"
    if multi_repo(work):
        return work, None   # a folder of several repos: the run skill makes one worktree per repo itself

    def git(*args):
        try:
            return _run(["git", *args], capture_output=True, text=True, cwd=str(work), timeout=GIT_TIMEOUT,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:   # the dispatcher's pass must not wait on a slow remote or a prompt
            return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {GIT_TIMEOUT}s")

    git("worktree", "prune")   # a registration whose folder is gone would block worktree add; a real problem shows there
    if found := _worktree_on(work, branch):
        return found, None
    rework = (c.get("metadata") or {}).get("rework") or {}
    if rework.get("active") and rework.get("prs"):
        head = ((_pr_info(rework["prs"][0]) or {}).get("headRefName") or "")
        if head and not head.startswith("-"):
            branch = head
            if found := _worktree_on(work, branch):
                return found, None
    path = planned_path(c)
    if not rework.get("active"):
        own = re.compile(re.escape(branch) + r"-s\d+")
        if any(w["path"] == path and own.fullmatch(w.get("branch") or "") for w in _list_worktrees(work)):
            return path, None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if err := _set_aside(path, c["id"]):
            return None, err
        git("worktree", "prune")   # the renamed folder is still registered as holding its branch

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


_PR_FIELDS = "state,mergedAt,headRefName,headRefOid"
BRANCH_MISS_SECONDS = 24 * 3600


def _settle(key, value):
    known = _settled()
    known[key] = value
    try:
        _settled_path().parent.mkdir(parents=True, exist_ok=True)
        _settled_path().write_text(json.dumps(known))
    except OSError:
        pass


def _pr_info(url: str) -> dict | None:
    cached = _settled().get(url)
    if cached and "headRefOid" in cached:
        return cached
    r = ghquota.gh(_run, ["pr", "view", url, "--json", _PR_FIELDS], capture_output=True, text=True, env=C.gh_env())
    if r.returncode != 0:
        return cached
    try:
        info = json.loads(r.stdout)
    except Exception:
        return cached
    if isinstance(info, dict) and (info.get("state") in ("MERGED", "CLOSED") or info.get("mergedAt")):
        info.setdefault("headRefOid", None)
        _settle(url, info)
    return info


def _pr_for_branch(branch: str) -> dict | None:
    """The merged PR that came from this branch, for a card with no recorded PR; a hit is kept for good, a miss for a day."""
    key = f"branch:{branch}"
    known = _settled().get(key)
    if known and "miss_at" not in known:
        return known
    if known and time.time() - known["miss_at"] < BRANCH_MISS_SECONDS:
        return None
    r = ghquota.gh(_run, ["pr", "list", "--head", branch, "--state", "merged", "--limit", "1", "--json", _PR_FIELDS],
                   capture_output=True, text=True, env=C.gh_env(), cwd=str(C.WORK_DIR))
    try:
        found = json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        found = None
    if not isinstance(found, list):
        return None
    hit = found[0] if found else None
    if hit:
        hit.setdefault("headRefOid", None)
    _settle(key, hit or {"miss_at": time.time()})
    return hit


def _match_card(path, branch, all_cards):
    """The card that owns this worktree, by the recorded path, the recorded branch, the folder's slug or pl/<slug>[-sN];
    a live card wins over a finished one with the same slug."""
    name = path.name.split("-pl-", 1)[-1]
    own = re.fullmatch(r"pl/(.+?)(?:-s\d+)?", branch or "")
    tests = (lambda c, m: m.get("worktree") and Path(m["worktree"]).resolve() == path.resolve(),
             lambda c, m: m.get("branch") and m["branch"] == branch,
             lambda c, m: slug_of(c) == name,
             lambda c, m: own and slug_of(c) in (own.group(1), (branch or "").removeprefix("pl/")))
    for test in tests:
        found = [c for c in all_cards if test(c, c.get("metadata") or {})]
        if found:
            return next((c for c in found if _column(c) != "Done"), found[0])
    return None


def _column(c):
    return c.get("column") or col_name(c["list_id"])


def sweep(all_cards: list[dict], dry: bool = False) -> None:
    """Remove exactly the worktrees the report marks remove."""
    if not (C.WORKTREES or {}).get("cleanup", True):
        return
    if not C.WORKTREES_DIR or not Path(C.WORKTREES_DIR).is_dir():
        return
    if not C.WORK_DIR or not Path(C.WORK_DIR).is_dir():
        return
    by_id = {c["id"]: c for c in all_cards}
    rows = inventory(all_cards, sizes=False)
    for r in rows:
        if r["reason"].startswith("agent") and (hold := agent_hold(by_id[r["card"]])):
            why = f" ({hold[1]})" if hold[1] else ""
            print(f"worktree {r['path']} {r['card']} skipped: agent {hold[0]}{why}")
    if dry:
        for r in rows:
            if r["action"] == "remove":
                print(f"  would remove worktree {r['path']}")
        return
    _remove(rows, by="sweep")


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
