"""Skills installed from GitHub: parse a source, pin its commit, fetch it into a temporary folder, list its files and,
after a yes, copy the skill into the library. Nothing fetched is executed or imported."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pl import config as C
from pl import harnesses
from pl import skills

NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
CODE_EXT = {".sh", ".py", ".js", ".ts", ".rb", ".ps1"}
JSON_FLAGS = {"hooks": "hook", "mcpServers": "mcp"}   # a key in plugin or settings JSON -> the flag it raises
KIND_ORDER = {"script": 0, "hook": 1, "mcp": 2}
SOURCES_FILE = ".pl-sources.json"   # in the library: {name: {source, repo, path, ref, sha, files, installed_at, plugin}}


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
    try:
        for argv in (["git", "init", "-q"], ["git", "remote", "add", "origin", _url(src)],
                     ["git", "fetch", "-q", "--depth", "1", "origin", sha], ["git", "checkout", "-q", "FETCH_HEAD"]):
            _git(argv, cwd=root, timeout=300 if argv[1] == "fetch" else 60)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
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


def _sole_skill_path(inv, path):
    """The path of the one skill folder in a source that is not itself a skill folder."""
    if "SKILL.md" in inv["files"]:
        return path
    if inv["kind"] != "skill" or len(inv["skills"]) != 1:
        raise Refused("this source is not a single skill; name the skill's folder: github:owner/repo/path/to/skill")
    sub = next(f for f in inv["files"] if f.endswith("/SKILL.md"))[:-len("/SKILL.md")]
    return f"{path}/{sub}" if path else sub


def _print_listing(root, path, inv, out, show_text):
    base = Path(root) / path if path else Path(root)
    if inv["flags"]:
        counts = {k: sum(f["kind"] == k for f in inv["flags"]) for k in KIND_ORDER}
        print("Runs code: " + ", ".join(f"{k} x{n}" for k, n in counts.items() if n), file=out)
        for f in inv["flags"]:
            print(f"  {f['kind']}  {f['file']}", file=out)
    else:
        print("Runs code: none found", file=out)
    print("Files:", file=out)
    for rel in inv["files"]:
        print(f"  {rel}  ({(base / rel).stat().st_size} bytes)", file=out)
    if not show_text:
        return
    for rel in inv["files"]:
        print(f"\n===== {rel} =====", file=out)
        try:
            print((base / rel).read_text(encoding="utf-8"), file=out)
        except (OSError, ValueError):
            print("(binary or unreadable)", file=out)


def _record(lib, name, entry):
    path = lib / SOURCES_FILE
    with skills._install_lock(lib) as held:
        if not held:
            raise RuntimeError("another install holds the library lock; try again")
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            rec = {}
        rec = rec if isinstance(rec, dict) else {}
        rec[name] = entry
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(rec, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)


def records():
    """{name: record} of the skills installed from GitHub; no record counts as {}."""
    try:
        rec = json.loads((skills.library() / SOURCES_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in rec.items() if isinstance(v, dict)} if isinstance(rec, dict) else {}


def _fail(msg):
    print(f"pl skills install: {msg}", file=sys.stderr)
    return 2


def install(text, yes, out, dry_run=False, name=None):
    """Show every file of the source, ask, then copy the skill into the library and record where it came from.
    Returns the exit code: 0 done (or declined, or a dry run), 2 refused."""
    tty = sys.stdin.isatty()
    if not yes and not dry_run and not tty:
        return _fail("no terminal to confirm on; add --yes")
    root = stage = None
    try:
        src = parse(text)
        sha = resolve(src)
        root = fetch(src, sha)
        inv = inventory(root, src["path"])
        path = _sole_skill_path(inv, src["path"])
        if path != src["path"]:
            inv = inventory(root, path)
        name = name or (path.rsplit("/", 1)[-1] if path else src["repo"])
        skills._check_name(name)
        lib = skills.library()
        if os.path.lexists(lib / name):
            return _fail(f"{lib / name} already exists; pl never overwrites")
        _print_listing(root, path, inv, out, show_text=tty)
        if dry_run:
            return 0
        if not yes:
            try:
                ok = input(f"Install {name} from {src['owner']}/{src['repo']}@{sha[:7]}? [y/N]: ")
            except EOFError:
                ok = ""
            if ok.strip().lower() not in ("y", "yes"):
                print("nothing installed", file=out)
                return 0
        lib.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=lib))
        base = Path(root) / path if path else Path(root)
        shutil.copytree(base, stage / name, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        with skills._install_lock(lib) as held:
            if not held:
                raise RuntimeError("another install holds the library lock; try again")
            if os.path.lexists(lib / name):
                return _fail(f"{lib / name} already exists; pl never overwrites")
            os.replace(stage / name, lib / name)
        _record(lib, name, {
            "source": "github", "repo": f"{src['owner']}/{src['repo']}", "path": path, "ref": src["ref"], "sha": sha,
            "files": {rel: skills._sha256((lib / name / rel).read_bytes()) for rel in inv["files"]},
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "plugin": None})
    except Refused as e:
        return _fail(str(e))
    except SystemExit as e:
        return _fail(str(e).removeprefix("pl: "))
    except RuntimeError as e:
        print(f"pl skills install: {e}", file=sys.stderr)
        return 1
    finally:
        for d in (root, stage):
            if d:
                shutil.rmtree(d, ignore_errors=True)
    print(f"installed {name} into {skills.library() / name}", file=out)
    for line in skills.install_in_all_harnesses(name):
        print(line, file=out)
    return 0


def _src_of(rec):
    owner, _, repo = rec["repo"].partition("/")
    return _src(owner, repo, rec["path"], rec["ref"])


def _hashes(folder):
    return {p.relative_to(folder).as_posix(): skills._sha256(p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file() and ".git" not in p.relative_to(folder).parts}


def check(out, as_json=False):
    """List the installed skills whose ref now points at a newer commit; changes nothing. Exit 1 only when every
    check failed."""
    rows = []
    for name, rec in sorted(records().items()):
        row = {"name": name, "repo": rec.get("repo"), "path": rec.get("path"), "ref": rec.get("ref"),
               "pinned": rec.get("sha"), "latest": None, "update": False, "error": None}
        try:
            row["latest"] = resolve(_src_of(rec))
            row["update"] = row["latest"] != rec["sha"]
        except (Refused, RuntimeError, KeyError, OSError, subprocess.SubprocessError) as e:
            row["error"] = str(e) or type(e).__name__
        rows.append(row)
    if as_json:
        print(json.dumps(rows, indent=1), file=out)
    for r in rows:
        if r["error"] and not as_json:
            print(f"{r['name']}  check failed: {r['error']}", file=out)
        elif r["update"] and not as_json:
            print(f"{r['name']}  {r['repo']}@{r['pinned'][:7]} -> {r['latest'][:7]}", file=out)
    return 1 if rows and all(r["error"] for r in rows) else 0


def _print_changes(old, new, hashes, inv, out):
    changed = {f for f in inv["files"] if hashes.get(f) != skills._sha256((new / f).read_bytes())}
    fresh = [f for f in inv["flags"] if f["file"] in changed]
    if fresh:
        print("New or changed code: " + ", ".join(f"{f['kind']} {f['file']}" for f in fresh), file=out)
    for rel in sorted(changed | (set(hashes) - set(inv["files"]))):
        before = (old / rel).read_text(errors="replace") if (old / rel).is_file() else ""
        after = (new / rel).read_text(errors="replace") if rel in changed else ""
        print(skills.diff(before, after, rel) or f"{rel}: binary or empty", file=out)


def update(name, yes, out):
    """Show what the pinned commit's upstream changed in an installed skill, ask, then apply it. An edited copy moves
    to <library>/.pl-edited/ first. Returns the exit code."""
    if not yes and not sys.stdin.isatty():
        return _fail("no terminal to confirm on; add --yes")
    root = stage = None
    try:
        rec = records().get(name)
        if not rec:
            return _fail(f"{name!r} was not installed from GitHub")
        src, lib = _src_of(rec), skills.library()
        sha = resolve(src)
        if sha == rec["sha"]:
            print(f"{name} is up to date", file=out)
            return 0
        root = fetch(src, sha)
        inv = inventory(root, rec["path"])
        new = Path(root) / rec["path"] if rec["path"] else Path(root)
        _print_changes(lib / name, new, rec["files"], inv, out)
        edited = _hashes(lib / name) != rec["files"]
        if not yes:
            try:
                ok = input(f"Update {name} to {rec['repo']}@{sha[:7]}? [y/N]: ")
            except EOFError:
                ok = ""
            if ok.strip().lower() not in ("y", "yes"):
                print("nothing updated", file=out)
                return 0
        stage = Path(tempfile.mkdtemp(prefix=".update-", dir=lib))
        shutil.copytree(new, stage / name, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        with skills._install_lock(lib) as held:
            if not held:
                raise RuntimeError("another install holds the library lock; try again")
            if edited:
                kept = skills._stamp(lib / ".pl-edited" / name)
                kept.parent.mkdir(exist_ok=True)
                os.replace(lib / name, kept)
                print(f"your edited copy is kept at {kept}", file=out)
            else:
                shutil.rmtree(lib / name)
            os.replace(stage / name, lib / name)
        _record(lib, name, {**rec, "sha": sha, "files": _hashes(lib / name)})
    except Refused as e:
        return _fail(str(e))
    except RuntimeError as e:
        print(f"pl skills update: {e}", file=sys.stderr)
        return 1
    finally:
        for d in (root, stage):
            if d:
                shutil.rmtree(d, ignore_errors=True)
    print(f"updated {name} to {sha[:7]}", file=out)
    return 0
