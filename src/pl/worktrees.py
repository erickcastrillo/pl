"""pl worktrees list | gc [--yes] [--json]: the worktrees pl-run made in the profile's worktrees folder, and which are safe to remove."""
import json
import subprocess
from pathlib import Path

from pl import config as C
from pl.agents import registry, worker_status
from pl.board import cards, col_name
from pl.util import slug_of


def _git(argv):
    """The one subprocess seam of this module; tests fake it."""
    r = subprocess.run(["git", *argv], capture_output=True, text=True)
    return r.returncode, r.stdout


def _card_for(name, cs):
    return next((c for c in cs if (s := slug_of(c)) and name and (s.startswith(name) or name.startswith(s))), None)


def _inspect(path, c):
    """(action, reason) for one worktree folder and the card it belongs to, if any."""
    if c:
        column = col_name(c["list_id"])
        if column != "Done":
            return "keep", f"card {column}"
        w = (c.get("metadata") or {}).get("worker")
        if w and worker_status(w, registry())[0] in ("alive", "starting"):
            return "keep", "agent running"
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
        removed, freed = removed + 1, freed + size
        commons.add(common)
    for common in commons:
        _git(["--git-dir", common, "worktree", "prune"])
    return removed, failed, freed


def cmd_worktrees(a):
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
