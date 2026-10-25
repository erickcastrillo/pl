"""Skills installed from GitHub: parse a source, pin its commit, fetch it into a temporary folder and list its files.
Nothing fetched is executed, imported or installed here."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pl import config as C
from pl import harnesses

NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
CODE_EXT = {".sh", ".py", ".js", ".ts", ".rb", ".ps1"}
JSON_FLAGS = {"hooks": "hook", "mcpServers": "mcp"}   # a key in plugin or settings JSON -> the flag it raises
KIND_ORDER = {"script": 0, "hook": 1, "mcp": 2}


class Refused(Exception):
    """A source pl will not fetch or install; the message is the reason."""


def _git(argv, cwd=None, timeout=60):
    p = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr.strip().splitlines() or [f"git exited {p.returncode}"])[-1])
    return p.stdout


def _src(owner, repo, path, ref):
    if not NAME_RE.fullmatch(owner) or not NAME_RE.fullmatch(repo):
        raise Refused("owner and repo may only hold letters, digits, '_', '.' and '-'")
    parts = path.split("/") if path else []
    if ".." in parts or path.startswith("/") or "" in parts:
        raise Refused(f"path {path!r} may not hold '..' or empty parts or start with '/'")
    return {"owner": owner, "repo": repo, "path": path, "ref": ref}


def parse(text):
    """{owner, repo, path, ref} of github:owner/repo[/path][@ref] or https://github.com/owner/repo[/tree/<ref>/<path>]."""
    text = text.strip()
    if text.startswith("github:"):
        body, _, ref = text[len("github:"):].partition("@")
        owner, _, rest = body.partition("/")
        repo, _, path = rest.partition("/")
        if not owner or not repo:
            raise Refused("a source is github:owner/repo[/path][@ref]: owner/repo is missing")
        return _src(owner, repo, path, ref)
    m = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?/?", text)
    if not m:
        raise Refused("only GitHub sources are supported: github:owner/repo[/path][@ref] or a github.com URL")
    return _src(m[1], m[2], (m[4] or "").rstrip("/"), m[3] or "")


def _url(src):
    return f"https://github.com/{src['owner']}/{src['repo']}"


def resolve(src):
    """The commit sha src's ref (or HEAD) points to now."""
    ref = src["ref"]
    if SHA_RE.fullmatch(ref):
        return ref
    out = _git(["git", "ls-remote", _url(src), ref or "HEAD"])
    for line in out.splitlines():
        sha = line.split("\t")[0]
        if SHA_RE.fullmatch(sha):
            return sha
    raise Refused(f"no such ref {ref or 'HEAD'!r} in {src['owner']}/{src['repo']}")


def fetch(src, sha):
    """Folder of a temporary checkout of exactly commit sha, under the profile's state folder."""
    if not SHA_RE.fullmatch(sha):
        raise Refused(f"{sha!r} is not a commit sha")
    root = Path(tempfile.mkdtemp(prefix="skill-src-", dir=C.STATE_DIR))
    for argv in (["git", "init", "-q"], ["git", "remote", "add", "origin", _url(src)],
                 ["git", "fetch", "-q", "--depth", "1", "origin", sha], ["git", "checkout", "-q", "FETCH_HEAD"]):
        _git(argv, cwd=root, timeout=300 if argv[1] == "fetch" else 60)
    return root


def _is_script(p, rel):
    if os.access(p, os.X_OK) or Path(rel).suffix.lower() in CODE_EXT:
        return True
    with open(p, "rb") as f:
        return f.read(2) == b"#!"


def _json_flags(p):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {kind for key, kind in JSON_FLAGS.items() if isinstance(data, dict) and key in data}


def inventory(root, path):
    """Every file under root/path, the code in them flagged by kind, and what kind of source it is."""
    root = Path(root).resolve()
    base = root / path if path else root
    if not base.exists():
        raise Refused(f"no such path at the pinned commit: {path}")
    files, flags, skills = [], [], []
    for d, dirs, names in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x != ".git")
        for n in sorted(names) + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            p = Path(d, n)
            if harnesses.SECRET_RE.fullmatch(n) or (p.is_symlink() and not Path(os.path.realpath(p)).is_relative_to(root)):
                raise Refused(f"{p.relative_to(root)} is a credential file or a link out of the source")
            if p.is_dir():
                continue
            rel = p.relative_to(base).as_posix()
            files.append(rel)
            kinds = set()
            if _is_script(p, rel):
                kinds.add("script")
            if rel == "hooks/hooks.json":
                kinds.add("hook")
            if p.suffix == ".json":
                kinds |= _json_flags(p)
            if n == ".mcp.json":
                kinds.add("mcp")
            flags += [{"kind": k, "file": rel} for k in kinds]
            if n == "SKILL.md":
                skills.append(Path(d).name)
    flags.sort(key=lambda f: (KIND_ORDER[f["kind"]], f["file"]))
    kind = ("marketplace" if (base / ".claude-plugin" / "marketplace.json").is_file()
            else "plugin" if (base / ".claude-plugin" / "plugin.json").is_file() else "skill")
    return {"kind": kind, "files": sorted(files), "flags": flags, "skills": sorted(skills)}
