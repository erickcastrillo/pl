"""Harnesses: the AI coding CLIs an agent runs under (Claude Code, Codex, Antigravity).
pl drives the CLI the user installed and signed in to; it never reads or stores a harness API key."""
import dataclasses
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pl import config as C


@dataclass
class Harness:
    name: str
    bin: str
    env_var: str | None = None
    interactive: list = field(default_factory=list)
    headless: list = field(default_factory=list)
    limit_patterns: list = field(default_factory=list)
    session_registry: bool = False
    experimental: bool = False
    note: str = ""
    resume: list = field(default_factory=list)   # relaunch a session by id in place (pl move-agent); empty = cannot
    unattended: bool = True                      # [harnesses.<name>] unattended = false: pipeline agents ask first
    permission_patterns: list | None = None      # its permission prompt on screen; None = PERMISSION_PATTERNS[name]


SHELLS = ("zsh", "bash", "sh", "fish")


def _run(argv, **kw):
    """The one subprocess seam of this module; tests fake it."""
    return subprocess.run(argv, capture_output=True, text=True, **kw)


BUILTINS = {
    "claude": Harness(
        "claude", "claude", "CLAUDE_CONFIG_DIR",
        ["claude", "--session-id", "{session_id}", "--name", "{label}", "{prompt}"], ["claude", "-p", "{prompt}"],
        [r"You're out of usage credits", r"Usage limit reached ·", r"You've hit your (?:usage )?limit",
         r"Claude usage limit reached", r"You[’']ve hit your (?:[\w-]+ )?limit",
         r"(?:usage|session|weekly|5-hour|hourly|daily) limit reached", r"Stop and wait for limit to reset"],
        session_registry=True, resume=["claude", "--resume", "{session_id}", "--name", "{label}", "{prompt}"]),
    "codex": Harness(
        "codex", "codex", "CODEX_HOME", ["codex", "{prompt}"], ["codex", "exec", "{prompt}"],
        [r"You've hit your usage limit\.", r"You've reached your (?:usage|workspace credit) limit",
         r"Your workspace is out of credits"]),   # Codex's own wording (strings of codex 0.150.1)
    "antigravity": Harness(
        "antigravity", "agy", None, ["agy", "-i", "{prompt}"], ["agy", "-p", "{prompt}"], [], experimental=True,
        note="agy reads an interactive prompt only from -i (--prompt-interactive): a bare prompt argument is refused; "
             "headless (-p) cannot prompt for tool permissions, so allow the tools in agy's own settings (pl never adds "
             "--dangerously-skip-permissions)"),
}
FIELDS = {f.name for f in dataclasses.fields(Harness)}

# The flags that let a pipeline agent or loop work without stopping for permission; never the Assistant. From each
# CLI's --help: claude 2.1.286 (auto: a safety classifier approves routine actions and blocks risky ones), codex 0.150.1
# (never ask, writes only inside the workspace), agy and gemini 0.32.1 (edits only: their one mode that never asks
# skips every check, so pl keeps asking for commands, see STILL_ASKS). pl never uses a yolo, bypass or skip mode.
UNATTENDED = {"claude": ["--permission-mode", "auto"],
              "codex": ["--ask-for-approval", "never", "--sandbox", "workspace-write"],
              "antigravity": ["--mode", "accept-edits"],
              "gemini": ["--approval-mode", "auto_edit"]}
STILL_ASKS = {"antigravity": "allow the commands in agy's own settings",
              "gemini": "allow the commands with a gemini policy file (--policy)"}
# A permission prompt on screen (strings of each CLI's binary at the versions above).
PERMISSION_PATTERNS = {
    "claude": [r"Do you want to (?:proceed|make this edit to|create|allow)", r"Yes, and don't ask again for"],
    "codex": [r"Would you like to (?:run the following command|make the following edits|grant)"],
    "antigravity": [r"Allow (?:access to this|calling this tool|creation of this file|administrator elevation)",
                    r"Approve this action\?", r"Run this command\?", r"Do you want to proceed\?"],
    "gemini": [r"Allow execution of", r"Apply this change\?", r"Do you want to proceed\?"]}
# A first-run folder trust prompt on screen. Only Claude Code asks it; pl never answers it (the person does, once per folder).
TRUST_PATTERNS = {"claude": [r"Accessing workspace", r"Yes, I trust this folder", r"Quick safety check"]}
# A template token that already chooses a permission or sandbox mode (the Assistant refuses such a template too).
PERMISSION_FLAG_RE = re.compile(r"dangerously|bypass|yolo|full-auto|approve-for-me|permission|approval|sandbox|dontask"
                                r"|acceptedits|accept-edits|allowed-?tools|settings|^-[as]$|^--mode(?:=|$)", re.I)


def unattended(h):
    """h with its unattended flags after the program name, for a pipeline agent or loop. h itself when [permissions]
    unattended = false, [harnesses.<name>] unattended = false, it has none, or its template sets a permission flag."""
    flags = UNATTENDED.get(h.name) if h.unattended and C.PERMISSIONS.get("unattended", True) is not False else None
    if not flags or any(PERMISSION_FLAG_RE.search(t) for t in (*h.interactive, *h.resume)):
        return h
    return dataclasses.replace(h, interactive=[*h.interactive[:1], *flags, *h.interactive[1:]],
                               resume=[*h.resume[:1], *flags, *h.resume[1:]] if h.resume else [])


def still_asks(name):
    """A warning when this harness's agents may still wait at a permission prompt, else None."""
    if name not in STILL_ASKS:
        return None
    return (f"{name} agents may wait at a permission prompt for commands: its only mode that never asks skips every "
            f"check, and pl never uses it. To let them run, {STILL_ASKS[name]}.")


def trust_patterns(h):
    return TRUST_PATTERNS.get(h.name, [])


def permission_patterns(h):
    return PERMISSION_PATTERNS.get(h.name, []) if h.permission_patterns is None else h.permission_patterns


ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CACHE = {}   # config path -> validated [harnesses] table, parsed once per process


def _overrides():
    """The profile's [harnesses] table (empty in legacy mode), validated on first read."""
    p = C.path()
    if not p:
        return {}
    if p not in _CACHE:
        try:
            table = tomllib.loads(p.read_text()).get("harnesses", {})
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise SystemExit(f"pl: cannot read [harnesses] from {p}: {e}")
        for name, ov in table.items():
            ev = ov.get("env_var")
            if ev is not None and not (isinstance(ev, str) and ENV_RE.fullmatch(ev)):
                raise SystemExit(f"pl: [harnesses.{name}] env_var {ev!r} is not a variable name ({p})")
            if not isinstance(ov.get("unattended", True), bool):
                raise SystemExit(f"pl: [harnesses.{name}] unattended must be true or false ({p})")
            for k in ("interactive", "headless", "resume", "limit_patterns", "permission_patterns"):
                if k in ov and not (isinstance(ov[k], list) and all(isinstance(x, str) for x in ov[k])):
                    raise SystemExit(f"pl: [harnesses.{name}] {k} must be a list of strings ({p})")
        _CACHE[p] = table
    return _CACHE[p]


def get(name):
    """The built-in harness with any [harnesses.<name>] fields laid over it."""
    ov = {k: v for k, v in _overrides().get(name, {}).items() if k in FIELDS and k != "name"}
    base = BUILTINS.get(name)
    if base is None and not {"bin", "interactive", "headless"} <= ov.keys():
        raise SystemExit(f"pl: unknown harness {name!r}; built in: {', '.join(BUILTINS)} "
                         "(or define bin, interactive and headless under [harnesses.<name>])")
    return dataclasses.replace(base, **ov) if base else Harness(name=name, **ov)


def default_account():
    return next(iter(C.PROFILES), None)


def account_harness(account):
    return get((C.ACCOUNTS.get(account) or {}).get("harness") or "claude")


def stage_pool(stage):
    """The [stages.<stage>] accounts list the stage spreads its agents over, or None."""
    return (C.STAGES.get(stage) or {}).get("accounts") or None


def harness_for(stage, c=None):
    """(Harness, account) for a stage: [stages.<stage>] harness/account, else the card's account and its harness.
    A stage with an accounts pool takes the card's account when it is in the pool (dispatch wrote it), else the first."""
    st = C.STAGES.get(stage) or {}
    want = ((c or {}).get("metadata") or {}).get("profile")
    pool = stage_pool(stage)
    for a in [st.get("account")] + (pool or []):
        if a and a not in C.PROFILES:
            raise SystemExit(f"pl: [stages.{stage}] account {a!r} is not an account; known: {', '.join(C.PROFILES)}")
    if pool:
        account = want if want in pool else pool[0]
    else:
        account = st.get("account") or (want if want in C.PROFILES else default_account())
    if account is None:
        raise SystemExit(f"pl: no harness accounts: add one under [accounts.<name>] in {C.path() or 'config.toml'}")
    return (get(st["harness"]) if st.get("harness") else account_harness(account)), account


def _argv(template, values):
    """Fill a template; an element whose placeholder has no value is dropped, with the flag just before it."""
    out = []
    for t in template:
        keys = re.findall(r"{(\w+)}", t)
        if any(values.get(k) is None for k in keys):
            if out and out[-1].startswith("-"):
                out.pop()
            continue
        out.append(re.sub(r"{(\w+)}", lambda m: str(values[m.group(1)]), t))  # one pass: values are never re-read
    return out


CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _typeable(s):
    """Strip control characters: send-keys types into a live terminal, where Ctrl-C/Ctrl-U/Enter act despite quoting."""
    return CONTROL_RE.sub("", re.sub(r"[\t\n\r]", " ", str(s)))


def launch_script(h, account, prompt, session_id, label, resume=False, plugin=True):
    """The sh script an agent window runs: every element control-stripped and shlex-quoted. It deletes itself first
    (a script still on disk means the launch never ran), then execs the harness in its own place so the pane shows
    the harness and the window's shell comes back when it exits. resume: the harness's resume argv (with prompt when given).
    plugin: false keeps pl's skills library plugin out of this launch."""
    from pl import skills
    pdir = skills.launch_plugin(h, plugin)
    prompt = skills.library_prompt(h, account, prompt, pdir) if prompt else prompt
    argv = skills.with_plugin(_argv(h.resume if resume else h.interactive,
                                    {"prompt": prompt, "session_id": session_id, "label": label}), pdir)
    d = C.PROFILES.get(account)
    lines = ["#!/bin/sh", 'rm -f -- "$0"']
    if h.env_var and d:
        lines.append(f"export {h.env_var}={shlex.quote(_typeable(d))}")
    lines.append("exec " + " ".join(shlex.quote(_typeable(a)) for a in argv))
    return "\n".join(lines) + "\n"


def headless_argv(h, account, prompt):
    """(argv, env) for a one-shot run; no shell."""
    env = dict(os.environ)
    d = C.PROFILES.get(account)
    if h.env_var and d:
        env[h.env_var] = str(d)
    if C.GH_CONFIG_DIR:
        env["GH_CONFIG_DIR"] = str(C.GH_CONFIG_DIR)
    from pl import skills
    pdir = skills.launch_plugin(h)
    return skills.with_plugin(_argv(h.headless, {"prompt": skills.library_prompt(h, account, prompt, pdir)}), pdir), env


def available(h):
    return shutil.which(h.bin) is not None


def pane_command(pane):
    """The foreground command of a tmux pane, or "" when it cannot be read."""
    r = _run(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"])
    return r.stdout.strip() if r.returncode == 0 else ""


def limit_hit(h, screen):
    """The first usage-limit match of this harness's own patterns on a screen, or None."""
    for p in h.limit_patterns:
        m = re.search(p, screen, re.I)
        if m:
            return m
    return None


# ---------- extensions: skills, slash commands, hooks ----------

SUPPORTED = {"claude": {"skills": True, "commands": True, "hooks": True},
             "codex": {"skills": False, "commands": True, "hooks": False}}
COMMAND_DIR = {"claude": "commands", "codex": "prompts"}   # a codex "command" is a custom prompt


def supported(h):
    return SUPPORTED.get(h.name, {"skills": False, "commands": False, "hooks": False})


def config_dir(account):
    d = C.PROFILES.get(account)
    return Path(d) if d else None


def inside(root, p):
    """True when p resolves (links followed) to root or below it."""
    try:
        return Path(p).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError):
        return False


SECRET_RE = re.compile(r"\.credentials.*|auth\.json", re.I)   # harness credential files: never opened or copied


def _roots(root=None):
    """Every account's config dir, resolved."""
    out = set()
    for d in [root, *C.PROFILES.values()]:
        try:
            if d:
                out.add(Path(d).resolve())
        except (OSError, RuntimeError):
            pass
    return out


def _cred_ids(root=None):
    """(st_dev, st_ino) of every account's credential files, from stat only (never read)."""
    ids = set()
    for d in _roots(root):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if SECRET_RE.fullmatch(e.name):
                        st = e.stat()
                        ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return ids


def is_secret_file(root, st):
    """True for a stat result that is a credential file under another name (a hard link)."""
    return st.st_nlink > 1 and (st.st_dev, st.st_ino) in _cred_ids(root)


def covers_root(root, p):
    """True when p is (by file identity, not spelling) an account's config dir or an ancestor of one."""
    try:
        st = os.stat(p)
    except OSError:
        return False
    for x in _roots(root):
        for y in (x, *x.parents):
            try:
                if os.path.samestat(st, os.stat(y)):
                    return True
            except OSError:
                pass
    return False


def real_path(root, p, shape):
    """p resolved (links followed) when the target has the expected shape inside some account's config dir, else None.
    shape: "skills" / "commands" / "prompts" (the dir), "skill" (skills/<name>), "SKILL.md" (skills/<name>/SKILL.md),
    "command:<dir>" (<dir>/<name>.md), "settings.json". A credential file, a config dir or an ancestor of one is refused."""
    try:
        r = Path(p).resolve()
    except (OSError, RuntimeError):
        return None
    roots = _roots(root)
    if SECRET_RE.fullmatch(r.name) or covers_root(root, r):
        return None
    try:
        if r.is_file() and is_secret_file(root, r.stat()):
            return None
    except OSError:
        return None
    kind, _, cdir = shape.partition(":")
    if kind in ("skills", "commands", "prompts"):
        ok = r.name == kind and r.parent in roots
    elif kind == "skill":
        try:
            from pl import skills
            in_lib = r.parent == skills.library().resolve()
        except (OSError, SystemExit):
            in_lib = False
        ok = (r.parent.name == "skills" and r.parent.parent in roots) or in_lib
    elif kind == "SKILL.md":
        try:
            from pl import skills
            in_lib = r.parent.parent == skills.library().resolve()
        except (OSError, SystemExit):
            in_lib = False
        ok = r.name == "SKILL.md" and ((r.parent.parent.name == "skills" and r.parent.parent.parent in roots) or in_lib)
    elif kind == "command":
        ok = r.suffix == ".md" and r.parent.name == cdir and r.parent.parent in roots
    elif kind == "settings.json":
        ok = r.name == "settings.json" and r.parent in roots
    else:
        ok = False
    return r if ok else None


def _head(root, p, shape, limit=8192):
    """The first bytes of a file whose resolved target has the expected shape (see real_path), or None."""
    r = real_path(root, p, shape)
    if r is None:
        return None
    try:
        fd = os.open(r, os.O_RDONLY | os.O_NOFOLLOW)   # a link swapped in after the check is not followed
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or is_secret_file(root, st):
            os.close(fd)
            return None
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _summary(text):
    """Frontmatter `description:` (else the first body line), cut to 120 characters."""
    lines = (text or "").splitlines()
    body = lines
    if lines and lines[0].strip() == "---" and "---" in [ln.strip() for ln in lines[1:]]:
        end = [ln.strip() for ln in lines[1:]].index("---") + 1
        for ln in lines[1:end]:
            k, _, v = ln.partition(":")
            if k.strip() == "description" and v.strip():
                return v.strip().strip("\"'")[:120]
        body = lines[end + 1:]
    return next((ln.strip() for ln in body if ln.strip()), "")[:120]


def _hooks(root, settings):
    """(items, error) from settings.json's `hooks` key only; nothing else in the file is used."""
    text = _head(root, settings, "settings.json", 1 << 20)
    if text is None:
        return [], None
    try:
        hooks = json.loads(text).get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks is not an object")
    except (ValueError, AttributeError) as e:
        return [], f"settings.json: {e}"
    items = []
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            entry = entry if isinstance(entry, dict) else {}
            what = "; ".join(f"{x.get('type', '?')}: {x.get('command', '')}" for x in entry.get("hooks", [])
                             if isinstance(x, dict))
            m = entry.get("matcher")
            items.append({"name": f"{event} {m}" if isinstance(m, str) and m else str(event),
                          "path": str(settings), "summary": what[:120]})
    return items, None


def extensions(h, account):
    """{skills, commands, hooks: [{name, path, summary}], supported: {kind: bool}, error}. Reads only skills/*/SKILL.md,
    the command dir and settings.json's hooks; a link is followed only to that same shape in an account's config dir."""
    out = {"skills": [], "commands": [], "hooks": [], "supported": dict(supported(h)), "error": None}
    root = config_dir(account)
    if root is None or not root.is_dir():
        out["error"] = f"account {account!r} has no config dir"
        return out
    sup = out["supported"]
    if sup["skills"] and real_path(root, root / "skills", "skills") and (root / "skills").is_dir():
        for d in sorted((root / "skills").iterdir()):
            text = _head(root, d / "SKILL.md", "SKILL.md")
            if text is not None:
                out["skills"].append({"name": d.name, "path": str(d / "SKILL.md"), "summary": _summary(text)})
    cname = COMMAND_DIR.get(h.name, "commands")
    cdir = root / cname
    if sup["commands"] and real_path(root, cdir, cname) and cdir.is_dir():
        for f in sorted(cdir.glob("*.md")):
            text = _head(root, f, f"command:{cname}")
            if text is not None:
                out["commands"].append({"name": f.stem, "path": str(f), "summary": _summary(text)})
    if sup["hooks"]:
        out["hooks"], out["error"] = _hooks(root, root / "settings.json")
    return out
