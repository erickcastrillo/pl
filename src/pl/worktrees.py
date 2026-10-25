"""A repo's .pl/worktree.yaml (or .worktreeinclude): files a fresh worktree copies or links from the main checkout."""
import os
import re
import shutil
import subprocess
from pathlib import Path

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


def bootstrap_worktree(main_repo, worktree_path, dry_run=False):
    """Apply the manifest to a worktree and return one line per path; nothing is staged, and no file contents are logged."""
    main_repo, worktree_path = Path(main_repo), Path(worktree_path)
    manifest = load_manifest(main_repo)
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
    return notes


def _git(argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True)


def main_checkout(cwd):
    r = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    return Path(r.stdout.strip()).parent if r.returncode == 0 else Path(cwd)
