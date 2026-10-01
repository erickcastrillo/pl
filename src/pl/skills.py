"""Skills, agents and commands of every account of this profile, the shared ~/.claude/skills, and pl's skills library
(~/.local/share/pl/skills, or [skills] library). Only .md files under skills/, agents/ and commands/ are read; a link is
followed only when its target stays in the account folder or the library; credential files are never opened; a
plugin's files are never changed; nothing is overwritten; delete moves to <root>/.pl-trash."""
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

from pl import config as C
from pl import harnesses

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,48}")
KINDS = {"skill": "skills", "agent": "agents", "command": "commands"}
SHARED, LIBRARY = "shared", "library"
# How a library skill reaches an account, by harness. Both follow a symlinked skill folder (Claude Code docs, "Symlinked
# folders"; Codex docs, "Codex supports symlinked skill folders"); Codex keeps its skills in $CODEX_HOME/skills.
LINKABLE = {"claude", "codex"}
TEMPLATE = ("---\nname: {name}\ndescription: Say in one sentence when to use this {kind}.\n---\n\n# {name}\n\n"
            "## When to use\n\n- \n\n## Steps\n\n1. \n")


BUILTIN_DIR = Path(__file__).parent / "skills_builtin"   # pl's own stage skills, copied into the library
BUILTIN_STATE = ".pl-builtin.json"                       # in the library: {name: {version, sha256, told}}
BUILTIN_LOCK = ".pl-builtin.lock"                        # in the library: held while built-ins are installed
LOCK_WAIT = 5                                            # seconds an install waits for another one
VERSION_RE = re.compile(r"^pl-builtin-version:\s*(\d+)\s*$", re.M)
EDIT_LIMIT = 1 << 20     # the in-app editor opens files up to 1 MiB that decode as UTF-8
INLINE_MAX_AGE = 86400   # inlined prompt files older than a day are removed at the next launch


def library():
    """The library folder; SystemExit when [skills] library is relative, HOME, or an account folder or above one."""
    raw = (getattr(C, "SKILLS", None) or {}).get("library")
    p = Path(str(raw or "~/.local/share/pl/skills")).expanduser()
    if raw:
        if not p.is_absolute():
            raise SystemExit(f"pl: config: [skills] library {raw!r} must be an absolute path")
        try:
            home = os.path.samefile(p, Path.home())
        except OSError:
            home = False
        if home:
            raise SystemExit(f"pl: config: [skills] library {raw!r} is your home folder; pick a folder of its own")
        if harnesses.covers_root(None, p):
            raise SystemExit(f"pl: config: [skills] library {raw!r} is an account folder or holds one")
    return p


def sources():
    """[(account, config dir)] for every account, plus ("shared", ~/.claude) when it is not an account already."""
    out = [(a, Path(d)) for a, d in C.PROFILES.items()]
    shared = Path.home() / ".claude"
    try:
        if shared.is_dir() and shared.resolve() not in {p.resolve() for _, p in out}:
            out.append((SHARED, shared))
    except (OSError, RuntimeError):
        pass
    return out


def _root(account):
    if account == LIBRARY:
        return library()
    root = dict(sources()).get(account)
    if root is None or not root.is_dir():
        raise SystemExit(f"pl: account {account!r} has no config dir")
    return root


def _inside(p, root):
    """p resolved (links followed) when it stays in root or the library, else None."""
    try:
        r = Path(p).resolve()
        ok = [Path(root).resolve(), library().resolve()]
    except (OSError, RuntimeError):
        return None
    return r if any(r == t or r.is_relative_to(t) for t in ok) else None


def _plugin(root, p):
    if Path(root) == library():
        return False
    try:
        parts = Path(p).relative_to(root).parts
    except ValueError:
        parts = Path(p).parts
    return parts[:1] == ("plugins",) or ("plugins" in parts and bool({"cache", "marketplaces"} & set(parts)))


def _safe(root, p):
    """The resolved .md file p, or SystemExit saying why it is refused."""
    r = _inside(p, root)
    if r is None:
        raise SystemExit(f"pl: refused: {p} points outside the account folder")
    if r.suffix != ".md" or harnesses.SECRET_RE.fullmatch(r.name) or harnesses.SECRET_RE.fullmatch(Path(p).name):
        raise SystemExit(f"pl: refused: {p} is not a .md file")
    try:
        st = os.stat(r)
    except OSError as e:
        raise SystemExit(f"pl: cannot read {p}: {e}") from None
    if not stat.S_ISREG(st.st_mode) or harnesses.is_secret_file(root, st):
        raise SystemExit(f"pl: refused: {p} is not a plain .md file")
    return r


def _read_bytes(r, limit=EDIT_LIMIT):
    fd = os.open(r, os.O_RDONLY | os.O_NOFOLLOW)   # a link swapped in after the check is not followed
    with os.fdopen(fd, "rb") as f:
        return f.read(limit)


def _read(r, limit=EDIT_LIMIT):
    return _read_bytes(r, limit).decode("utf-8", errors="replace")


def _editable_bytes(r):
    """(raw bytes, text) of the whole file, or SystemExit when it is too big or not UTF-8 for the in-app editor."""
    raw = _read_bytes(r, EDIT_LIMIT + 1)
    if len(raw) > EDIT_LIMIT:
        raise SystemExit(f"pl: {r} is larger than 1 MiB; the in-app editor would cut it. Use E ($EDITOR)")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"pl: {r} is not UTF-8 text; the in-app editor would damage it. Use E ($EDITOR)") from None


def _entries(account, root, base, kind, plugin=False):
    out = []
    if not base.is_dir() or _inside(base, root) is None:
        return out
    for e in sorted(base.iterdir()):
        if e.name.startswith(".") or (kind != "skill" and e.suffix != ".md"):
            continue
        f = e / "SKILL.md" if kind == "skill" else e
        try:
            r = _safe(root, f)
            text = _read(r, 16384)
        except (SystemExit, OSError):
            continue
        name = e.name if kind == "skill" else e.stem
        plug = plugin or _plugin(root, r) or _plugin(root, f)
        out.append({"name": name, "account": account, "kind": kind, "path": str(f), "root": str(root),
                    "description": harnesses._summary(text), "linked": [],
                    "origin": "plugin" if plug else ("omc" if name.startswith("omc-") else ""),
                    "library": kind == "skill" and _in_library(r)})
    return out


def _in_library(r):
    try:
        return Path(r).resolve().is_relative_to(library().resolve())
    except (OSError, RuntimeError):
        return False


def scan():
    """[{name, account, kind, path, root, description, origin, library, linked}] for every account, ~/.claude and the
    library. origin: "plugin" (inside plugins/), "omc" (an omc- name) or "". linked: accounts using a library skill."""
    items = []
    for account, root in sources():
        for kind, d in KINDS.items():
            items += _entries(account, root, root / d, kind)
        pdir = root / "plugins"
        if pdir.is_dir() and not pdir.is_symlink():
            for d, dirs, _ in os.walk(pdir):
                dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "node_modules")
                for x in [x for x in dirs if x in KINDS.values()]:
                    kind = next(k for k, v in KINDS.items() if v == x)
                    items += _entries(account, root, Path(d) / x, kind, plugin=True)
                    dirs.remove(x)
    lib = _entries(LIBRARY, library(), library(), "skill") if library().is_dir() else []
    for it in lib:
        it["linked"] = sorted({i["account"] for i in items if i["library"] and i["name"] == it["name"]})
    return items + lib


def read(item):
    return _read(_safe(item["root"], item["path"]))


def edit_text(item):
    """The whole text for the in-app editor, or SystemExit saying why it is not opened there."""
    return _editable_bytes(check_editable(item))[1]


def check_editable(item):
    """The resolved file, or SystemExit with the reason it may not be changed."""
    if item.get("origin") == "plugin" or _plugin(item["root"], item["path"]):
        raise SystemExit("pl: refused: this is a plugin's file; the plugin manager replaces it on update. "
                         "Copy it into your own skills to change it")
    return _safe(item["root"], item["path"])


def _stamp(p):
    ts = time.strftime("%Y%m%d-%H%M%S")
    out, n = Path(f"{p}-{ts}"), 1
    while os.path.lexists(out):
        out, n = Path(f"{p}-{ts}-{n}"), n + 1
    return out


def diff(old, new, name="file"):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"{name} (now)", f"{name} (new)"))


def save(item, text, expect=None):
    """Back up the file byte for byte to <file>.bak-<time>, then write text. expect: the text the editor opened;
    when the file no longer holds it, nothing is written. Returns the backup's path."""
    r = check_editable(item)
    raw, old = _editable_bytes(r)
    if expect is not None and old != expect:
        raise SystemExit(f"pl: {item['path']} changed on disk; reopen it to edit the new text")
    bak = _stamp(r.with_name(r.name + ".bak"))
    fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        fd = os.open(r, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)   # never through a link swapped in meanwhile
    except OSError as e:
        raise SystemExit(f"pl: refused: {item['path']} could not be written safely: {e}") from None
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))
    return bak


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SystemExit(f"pl: {name!r} is not a valid name: 2 to 49 lowercase letters, digits and dashes, "
                         "starting with a letter or digit")


def _base(account, kind):
    """The skills/, agents/ or commands/ dir of an account (the library itself for "library"), created when absent;
    refused when it resolves outside the account folder."""
    root = _root(account)
    if account == LIBRARY:
        if kind != "skill":
            raise SystemExit("pl: the library holds skills only")
        base = root
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = root / KINDS[kind]
        if not os.path.lexists(base):
            base.mkdir()
    r = Path(base).resolve()
    if not (r == Path(root).resolve() or r.is_relative_to(Path(root).resolve())):
        raise SystemExit(f"pl: refused: {base} points outside the account folder")
    return base


def create(account, kind, name):
    """Write a new SKILL.md (in its own folder) or agent/command .md from the template; returns its path."""
    _check_name(name)
    if kind not in KINDS:
        raise SystemExit(f"pl: unknown kind {kind!r}")
    base = _base(account, kind)
    dest = base / name if kind == "skill" else base / f"{name}.md"
    if os.path.lexists(dest):
        raise SystemExit(f"pl: {dest} already exists; pl never overwrites")
    if kind == "skill":
        dest.mkdir()
        dest /= "SKILL.md"
    with open(dest, "x", encoding="utf-8") as f:
        f.write(TEMPLATE.format(name=name, kind=kind))
    return dest


def _trash_dir(root, kind):
    """<root>/.pl-trash/<skills|agents|commands>, a folder no harness reads; refused when a part is a link or it
    leaves root."""
    top = Path(root) / ".pl-trash"
    t = top / KINDS[kind]
    for d in (top, t):
        if os.path.islink(d):
            raise SystemExit(f"pl: refused: {d} is a link")
        d.mkdir(exist_ok=True, mode=0o700)
    if not t.resolve().is_relative_to(Path(root).resolve()):
        raise SystemExit(f"pl: refused: {t} points outside the account folder")
    return t


def trash(item):
    """Move a skill folder (or an agent/command file) to <root>/.pl-trash/<kind>/<name>-<time>; returns the new
    path."""
    check_editable(item)
    if item["account"] == LIBRARY and item.get("linked"):
        raise SystemExit(f"pl: refused: {', '.join(item['linked'])} still link to it")
    p = Path(item["path"])
    src = p.parent if item["kind"] == "skill" else p
    if not os.path.lexists(src) or _inside(src.parent, item["root"]) is None:
        raise SystemExit(f"pl: refused: {src} points outside the account folder")
    dest = _stamp(_trash_dir(item["root"], item["kind"]) / item["name"])
    if item["kind"] != "skill":
        dest = dest.with_name(dest.name + ".md")
    os.rename(src, dest)   # a link is moved as a link
    return dest


def _check_tree(src):
    """Refuse a skill folder holding a credential file or a link that leaves it (stat only, nothing opened)."""
    top = Path(src).resolve()
    for d, dirs, files in os.walk(top):
        for n in dirs + files:
            p = os.path.join(d, n)
            if harnesses.SECRET_RE.fullmatch(n) or (os.path.islink(p) and not Path(os.path.realpath(p)).is_relative_to(top)):
                raise SystemExit(f"pl: refused: {p} is a credential file or a link out of the skill folder")


def share(account, name):
    """Move account's skill folder into the library and leave a link in its place; returns the library folder."""
    _check_name(name)
    root = _root(account)
    src = root / "skills" / name
    if src.is_symlink():
        raise SystemExit(f"pl: {src} is a link already (shared, or not this account's own folder)")
    if not (src / "SKILL.md").is_file() or _inside(src, root) is None:
        raise SystemExit(f"pl: {account} has no skill {name!r}")
    if _plugin(root, src):
        raise SystemExit("pl: refused: a plugin's skill is not moved")
    _check_tree(src)
    dest = library() / name
    if os.path.lexists(dest):
        raise SystemExit(f"pl: {dest} already exists; pl never overwrites")
    library().mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    os.symlink(dest, src, target_is_directory=True)
    return dest


def link(name, account):
    """Link library skill name into account's skills/ folder; returns the link."""
    _check_name(name)
    src = library() / name
    if src.is_symlink() or not (src / "SKILL.md").is_file():
        raise SystemExit(f"pl: no library skill {name!r} in {library()}")
    h = "claude" if account == SHARED else harnesses.account_harness(account).name
    if h not in LINKABLE:
        raise SystemExit(f"pl: linking skills is not supported for {h}: pl does not know a skills folder it reads")
    dest = _base(account, "skill") / name
    if os.path.lexists(dest):
        raise SystemExit(f"pl: {dest} already exists; pl never overwrites")
    os.symlink(src, dest, target_is_directory=True)
    return dest


# ---------- launches: Claude loads the library as a plugin; other harnesses get the skill inlined ----------

def plugin_dir():
    """<profile state>/plugin: a Claude plugin (named "pl") whose skills/ links to the library, so a Claude session pl
    starts sees the library as /pl:<name>. Each profile has its own. None when the library holds no skill."""
    try:
        lib = library()
        if not lib.is_dir() or not any((d / "SKILL.md").is_file() for d in lib.iterdir() if not d.name.startswith(".")):
            return None
        p = C.STATE_DIR / "plugin"
        man = p / ".claude-plugin" / "plugin.json"
        man.parent.mkdir(parents=True, exist_ok=True)
        if not man.exists():
            man.write_text(json.dumps({"name": "pl", "description": "pl's shared skills library"}, indent=1) + "\n")
        link_ = p / "skills"
        if link_.is_symlink() and link_.resolve() != lib.resolve():
            link_.unlink()   # pl's own link to an old library folder
        if not os.path.lexists(link_):
            os.symlink(lib, link_, target_is_directory=True)
        return p if link_.is_symlink() and link_.resolve() == lib.resolve() else None
    except (OSError, SystemExit):
        return None


def launch_plugin(h, plugin=True):
    """The plugin folder a launch of h adds with --plugin-dir: Claude only, and only when plugin is true."""
    return plugin_dir() if plugin and h.name == "claude" else None


def with_plugin(argv, p):
    """argv with --plugin-dir p after the program name; argv unchanged when p is None."""
    return [argv[0], "--plugin-dir", str(p), *argv[1:]] if p and argv else argv


def _clean_inlined(d):
    cut = time.time() - INLINE_MAX_AGE
    for f in d.glob("skill-*.md"):
        try:
            if not f.is_symlink() and f.stat().st_mtime < cut:
                f.unlink()
        except OSError:
            pass


def library_prompt(h, account, prompt, plugin=None):
    """A prompt starting with /<name> (or, on Claude, /loop <every> /<name>) of a library skill the account (and the
    work folder) lacks: Claude gets
    /pl:<name> (the plugin's name for it), but only when plugin (this launch's --plugin-dir) is set; any other harness
    gets "Follow the instructions in <file>", the file holding SKILL.md and the prompt's arguments. Anything else, or
    any error, returns the prompt unchanged."""
    m = re.match(r"(\s*/loop\s+(?:[^/\s]\S*\s+)?)?\s*/([a-z0-9][a-z0-9-]*)(?=\s|$)", prompt or "")
    if not m or (m.group(1) and h.name != "claude"):
        return prompt
    loop, name, rest = m.group(1) or "", m.group(2), prompt[m.end():]
    try:
        f = library() / name / "SKILL.md"
        if not f.is_file():
            return prompt
        root = harnesses.config_dir(account)
        if root and os.path.lexists(root / "skills" / name):
            return prompt                          # the account's own skill of that name wins, for every harness
        if h.name == "claude":
            wd = Path(C.WORK_DIR) / ".claude" / "skills" / name if C.WORK_DIR else None
            own = os.path.lexists(root / "commands" / f"{name}.md") if root else False
            return prompt if own or not plugin or (wd and os.path.lexists(wd)) else f"{loop}/pl:{name}{rest}"
        text = _read(_safe(library(), f))
        d = C.STATE_DIR / "launch"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        _clean_inlined(d)
        out = d / f"skill-{name}-{hashlib.sha1(prompt.encode()).hexdigest()[:12]}.md"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)                       # an older file at this path keeps its mode on open
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{text.rstrip()}\n\n## Arguments\n\n{rest.strip() or '(none)'}\n")
        return f"Follow the instructions in {out}."
    except (OSError, SystemExit):
        return prompt


# ---------- built-in stage skills: shipped in the package, copied into the library, never over your edits ----------

def builtin_names():
    return sorted(d.name for d in BUILTIN_DIR.iterdir() if (d / "SKILL.md").is_file())


def _builtin(name):
    """(bytes, version) of a shipped skill."""
    raw = (BUILTIN_DIR / name / "SKILL.md").read_bytes()
    m = VERSION_RE.search(raw.decode("utf-8"))
    return raw, int(m.group(1)) if m else 0


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _state():
    """The library's record of what pl wrote; anything that is not a {name: {...}} entry counts as no record."""
    try:
        v = json.loads((library() / BUILTIN_STATE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: x for k, x in v.items() if isinstance(x, dict)} if isinstance(v, dict) else {}


def _ver(v):
    """A recorded version as an int; anything else is 0."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _save_state(st):
    p = library() / BUILTIN_STATE
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


def _write_builtin(dest, raw):
    """Write raw to dest (a SKILL.md in the library), never through a link."""
    if dest.parent.is_symlink():
        raise OSError(f"{dest.parent} is a link")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")   # a reader never sees half a file
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, dest)   # replaces a link itself, never writes through it
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _install_lock(lib):
    """Yields True while this process holds the library's install lock; False when another install held it for
    LOCK_WAIT seconds."""
    fd = os.open(lib / BUILTIN_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        end = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    yield False
                    return
                time.sleep(0.1)
        yield True
    finally:
        os.close(fd)   # closing releases the lock


def install_builtins():
    """Copy each built-in skill into the library when it is missing, or when the library copy is pl's own older
    version. A copy that differs from what pl wrote last (you edited it, or a skill of yours has that name) is kept;
    a newer shipped version is then reported once. Returns one line per change or kept update."""
    out, lib = [], library()
    try:
        lib.mkdir(parents=True, exist_ok=True)
        with _install_lock(lib) as held:
            return _install(lib) if held else out   # another install is running: it does the work
    except OSError:
        return out


def _install(lib):
    out, st = [], _state()
    before = json.dumps(st, sort_keys=True)
    for name in builtin_names():
        raw, ver = _builtin(name)
        dest, rec = lib / name / "SKILL.md", st.get(name) or {}
        try:
            if (lib / name).is_symlink():
                continue                                    # a link is the user's own arrangement: never written through
            if not dest.exists():
                _write_builtin(dest, raw)
                st[name] = {"version": ver, "sha256": _sha256(raw)}
                out.append(f"installed built-in {name}")
                continue
            have = _sha256(_read_bytes(_safe(lib, dest)))
            if have == _sha256(raw):
                st[name] = {"version": ver, "sha256": have}
            elif have == rec.get("sha256"):                 # pl's own copy, unedited
                if ver > _ver(rec.get("version")):
                    _write_builtin(dest, raw)
                    st[name] = {"version": ver, "sha256": _sha256(raw)}
                    out.append(f"updated built-in {name} to version {ver}")
            elif _ver(rec.get("told")) < ver:           # edited: keep it, say so once per new version
                st[name] = {**rec, "told": ver}
                out.append(f"built-in {name} has an update; your edited copy was kept")
        except (OSError, SystemExit):
            continue
    try:
        if json.dumps(st, sort_keys=True) != before:   # nothing new: the state file is left alone
            _save_state(st)
    except OSError:
        pass
    return out


def reset_builtin(name):
    """Put the shipped version of a built-in back, backing up the library copy first. Returns the backup or None."""
    if name not in builtin_names():
        raise SystemExit(f"pl: {name!r} is not a built-in skill ({', '.join(builtin_names())})")
    raw, ver = _builtin(name)
    dest = library() / name / "SKILL.md"
    if (library() / name).is_symlink():
        raise SystemExit(f"pl: refused: {library() / name} is a link")
    bak = None
    if dest.exists():
        old = _read_bytes(_safe(library(), dest))
        if old != raw:
            bak = _stamp(dest.with_name(dest.name + ".bak"))
            with os.fdopen(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as f:
                f.write(old)
    _write_builtin(dest, raw)
    st = _state()
    st[name] = {"version": ver, "sha256": _sha256(raw)}
    _save_state(st)
    return bak


def _cmd_reset(a):
    if a.name not in builtin_names():
        raise SystemExit(f"pl: {a.name!r} is not a built-in skill ({', '.join(builtin_names())})")
    dest = library() / a.name / "SKILL.md"
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("pl skills reset: no terminal to confirm on; add --yes")
        try:
            ok = input(f"Replace {dest} with the shipped version of {a.name}? A backup is kept. [y/N]: ")
        except EOFError:
            ok = ""
        if ok.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 0
    bak = reset_builtin(a.name)
    print(f"restored built-in {a.name} in {dest}" + (f" (your copy is {bak.name})" if bak else ""))
    return 0


def cmd_skills(a):
    """pl skills list | share NAME [--account A] | link NAME ACCOUNT | reset NAME [--yes]."""
    if a.skills_cmd == "reset":
        return _cmd_reset(a)
    if a.skills_cmd == "share":
        print(f"shared: {share(a.account or harnesses.default_account(), a.name)} (a link is left in its place)")
    elif a.skills_cmd == "link":
        print(f"linked: {link(a.name, a.account)}")
    else:
        rows = [(i["name"], i["account"], i["kind"], i["origin"],
                 ", ".join(i["linked"]) if i["account"] == LIBRARY else ("library" if i["library"] else ""),
                 i["description"]) for i in scan()]
        w = [max([len(r[k]) for r in rows] + [len(h)]) for k, h in enumerate(("name", "account", "kind", "origin", "library"))]
        print("  ".join(h.ljust(w[k]) for k, h in enumerate(("name", "account", "kind", "origin", "library"))) + "  description")
        for r in rows:
            print("  ".join(r[k].ljust(w[k]) for k in range(5)) + "  " + r[5])
        print(f"library: {library()}")
    return 0
