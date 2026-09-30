"""pl setup: ask what a new profile needs, then write its config.toml once and check it.

One function per question, ask_<topic>(answers, flags), returning the answer, so another front end can reuse them.
Interactive prompts use input(). With --yes (or no terminal) nothing is asked and a missing answer exits 2 naming
its flag. pl never installs a harness, never signs in for you, never reads inside a gh or harness config folder,
and shows MCP server names only, never their settings.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import MutableMapping
from pathlib import Path

import tomlkit

from pl import config as C
from pl import profiles
from pl.trackers import github

HARNESSES = {"claude": "claude", "codex": "codex", "agy": "antigravity"}   # choice -> built-in harness
BINARIES = {"claude": "claude", "codex": "codex", "agy": "agy"}
CONFIG_DIRS = {"claude": "~/.claude", "codex": "~/.codex", "agy": None}
INSTALL = {"claude": "npm install -g @anthropic-ai/claude-code  (https://docs.claude.com/en/docs/claude-code/setup)",
           "codex": "npm install -g @openai/codex  (https://github.com/openai/codex)",
           "agy": "see https://antigravity.google"}
TRACKERS = ("github-project", "github-issues", "mcp")
STAGES = ("spec", "design", "plan", "run")
MCP_DOC = "docs/mcp-trackers.md"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]+")
REMOTE_RE = re.compile(r"github\.com[:/]([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
LOGIN_RE = re.compile(r"Logged in to \S+ (?:account|as) ([A-Za-z0-9-]+)")
LABELS = {"review": ("pl:auto-review", "1D76DB", "PR opened by a pl agent, waiting for auto review"),   # key -> default,
          "ready": ("pl:ready-for-review", "0E8A16", "Reviewed by pl, ready for a person"),              # color, description
          "merge_ready": ("pl:merge-ready", "5319E7", "Passed every check, ready to merge"),
          "rework": ("pl:needs-rework", "FBCA04", "Needs more work before review"),
          "failed": ("pl:review-failed", "B60205", "Auto review gave up; needs a person")}
LABEL_FLAGS = {k: "--label-" + k.replace("_", "-") for k in LABELS}
# pl runs [paths] attention_cmd as:  <cmd> notify <title> <message>. Values reach osascript and notify-send as argv only.
NOTIFY_SCRIPT = r"""#!/bin/sh
# Written by pl setup. pl runs it as:  notify notify "<title>" "<message>"
# Replace it with your own executable that takes the same arguments, or point [paths] attention_cmd elsewhere.
# macOS: set PL_NOTIFY_SOUND to a sound name (for example Glass) to play a sound.
[ "$1" = notify ] && shift
title=${1:-pl}
message=${2:-}
if [ "$(uname -s)" = Darwin ] && command -v osascript >/dev/null 2>&1; then
  osascript - "$title" "$message" "${PL_NOTIFY_SOUND:-}" >/dev/null 2>&1 <<'EOF' && exit 0
on run argv
  if item 3 of argv is "" then
    display notification (item 2 of argv) with title (item 1 of argv)
  else
    display notification (item 2 of argv) with title (item 1 of argv) sound name (item 3 of argv)
  end if
end run
EOF
fi
if command -v notify-send >/dev/null 2>&1; then
  notify-send -- "$title" "$message" && exit 0
fi
printf '\a%s: %s\n' "$title" "$message" >&2
"""


def _run(argv, env=None, stdout_only=False):
    """The one subprocess seam: argv only, never a shell, no stdin. (exit code, stdout [+ stderr]), or None."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode, r.stdout + ("" if stdout_only else r.stderr)


def _input(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit("pl setup: setup cancelled, nothing written") from None


def _fail(flag, text):
    print(f"pl setup: {flag}: {text}", file=sys.stderr)
    raise SystemExit(2)


def _ask(flags, value, text, default, flag, what, check=None):
    """The flag's value wins; else ask (interactive) or take the default. default None = required, "" = optional."""
    while True:
        if value is not None:
            v = str(value)
        elif not flags.interactive:
            if default is None:
                _fail(flag, f"missing ({what})")
            v = default
        else:
            v = _input(f"{text}{f' [{default}]' if default else ''}: ").strip() or (default or "")
            if not v and default is None:
                continue
        err = check(v) if (v and check) else None
        if not err:
            return v
        if value is not None or not flags.interactive:
            _fail(flag, err)
        print(err)


def _consent(flags, value, prompt, default=True):
    """A --x/--no-x flag wins; else ask (interactive) or take the default (--yes)."""
    if value is not None:
        return value
    if not flags.interactive:
        return default
    v = _input(prompt).strip().lower()
    return default if not v else v in ("y", "yes")


def _tilde(p):
    return profiles._tilde(Path(p).expanduser())


def slug_for(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")
    return s or None


def _git_top(d):
    r = _run(["git", "-C", str(d), "rev-parse", "--show-toplevel"])
    return Path(r[1].strip()) if r and r[0] == 0 and r[1].strip() else None


def _work_default(flags, old=None):
    return Path(flags.work_dir or old).expanduser() if (flags.work_dir or old) else _git_top(Path.cwd())


def _existing(answers, slug):
    """An existing ~/.pl-<slug>: say so once; its settings (answers["old"]) become every question's default."""
    d = Path.home() / f".pl-{slug}"
    p = d / "config.toml"
    answers.update(old={}, old_bytes=None, exists=d.exists())
    if not d.exists():
        return
    if p.exists():
        answers["old_bytes"] = p.read_bytes()
        try:
            answers["old"] = tomllib.loads(answers["old_bytes"].decode())
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise SystemExit(f"pl setup: {p} is not valid TOML ({e}); fix it or move it away") from None
    print(f"Profile {slug!r} already exists; its current settings are the defaults below. Saving updates it"
          + ("; the old config.toml is kept as config.toml.bak-<YYYYmmdd-HHMMSS>." if p.exists() else "."))


def _old(answers, *keys):
    v = answers.get("old") or {}
    for k in keys:
        v = v.get(k) if isinstance(v, dict) else None
    return v


def _remote_repo(d):
    r = _run(["git", "-C", str(d), "remote", "get-url", "origin"]) if d else None
    m = REMOTE_RE.search(r[1].strip()) if r and r[0] == 0 else None
    return m.group(1) if m else None


# ---------- the questions ----------

def ask_name(answers, flags):
    if flags.slug and SLUG_RE.fullmatch(flags.slug):
        _existing(answers, flags.slug)
    name = _ask(flags, flags.name, "Profile name (a friendly name, for example Work)", _old(answers, "name") or flags.slug,
                "--name", "a friendly name for the profile")
    slug = _ask(flags, flags.slug, "Folder name for it (~/.pl-<this>)", slug_for(name), "--slug",
                "lower-case letters, digits and '-'",
                lambda v: None if SLUG_RE.fullmatch(v) else f"{v!r}: use lower-case letters, digits and '-' (at most 32)")
    if "exists" not in answers:
        _existing(answers, slug)
    return name, slug


def _mcp_file(flags, work):
    """The harness MCP file: --mcp-config, else <work>/.mcp.json, else ~/.claude.json if it lists servers."""
    if flags.mcp_config:
        return Path(flags.mcp_config).expanduser()
    for p in ([work / ".mcp.json"] if work else []) + [Path.home() / ".claude.json"]:
        if _server_names(p):
            return p
    return None


def _server_names(p):
    try:
        servers = json.loads(p.read_text()).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return []
    return [str(n) for n in servers] if isinstance(servers, dict) else []


def ask_harnesses(answers, flags):
    """[accounts.<harness>], one per chosen harness. Only checks the CLI is on PATH; never installs or signs in."""
    def check(v):
        bad = [h for h in re.split(r"[,\s]+", v) if h and h not in HARNESSES]
        return f"unknown harness {', '.join(bad)}; choose from {', '.join(HARNESSES)}" if bad else None
    given = " ".join(flags.harness) if flags.harness else None
    had = " ".join(n for n in _old(answers, "accounts") or {} if n in HARNESSES) or None
    picked = _ask(flags, given, f"Harnesses to use ({', '.join(HARNESSES)}; separate with spaces)", had, "--harness",
                  "claude, codex or agy; repeat the flag for more than one", check)
    chosen = list(dict.fromkeys(h for h in re.split(r"[,\s]+", picked) if h))
    dirs = dict(x.split("=", 1) for x in flags.config_dir if "=" in x)
    accounts = {}
    for h in chosen:
        if shutil.which(BINARIES[h]) is None:
            print(f"{BINARIES[h]} is not on your PATH. Install it: {INSTALL[h]}\n"
                  f"setting up {h} is outside pl; pl only uses it once you are signed in")
            answers["todo"].append(f"install {h} and sign in to it: {INSTALL[h]}")
        if CONFIG_DIRS[h] is None:
            d = dirs.get(h) or _old(answers, "accounts", h, "config_dir") or "~"   # agy has no config folder setting: the account just names the harness
        else:
            d = _ask(flags, dirs.get(h), f"{h} config folder",
                     _old(answers, "accounts", h, "config_dir") or CONFIG_DIRS[h], "--config-dir", f"{h}=PATH",
                     lambda v, h=h: None if Path(v).expanduser().is_dir() else
                     f"{v} does not exist: sign in to {h} once (run {BINARIES[h]} and sign in), which creates it, "
                     f"or give another folder with --config-dir {h}=PATH")
        accounts[h] = {"harness": HARNESSES[h], "config_dir": _tilde(d)}
    was = _old(answers, "stages", "spec", "account")
    default = flags.default_account or (was if was in accounts or was in (_old(answers, "accounts") or {}) else chosen[0])
    if default not in accounts and default not in (_old(answers, "accounts") or {}):
        _fail("--default-account", f"{default!r} is not one of the chosen harnesses ({', '.join(accounts)})")
    return accounts, default


def ask_tracker(answers, flags):
    """[tracker]. A create-project answer is kept as tracker["_create"] = title and done after the summary."""
    was = _old(answers, "tracker", "type")
    kind = _ask(flags, flags.tracker, "Where do cards live? github-project, github-issues or mcp",
                was if was in TRACKERS else None, "--tracker",
                "github-project, github-issues or mcp",
                lambda v: None if v in TRACKERS else f"choose github-project, github-issues or mcp, not {v!r}")
    ot = (_old(answers, "tracker") or {}) if kind == was else {}   # the old tracker's answers, same type only
    work = _work_default(flags, _old(answers, "paths", "work_dir"))
    if kind == "mcp":
        p = Path(ot["mcp_config"]).expanduser() if ot.get("mcp_config") and not flags.mcp_config else _mcp_file(flags, work)
        p = Path(_ask(flags, str(p) if p else None, "Harness MCP config file (JSON with mcpServers)", None, "--mcp-config",
                      "the harness's MCP config file")).expanduser()
        names = _server_names(p)
        if not names:
            _fail("--mcp-config", f"{p} lists no MCP servers")
        print(f"MCP servers in {p}: {', '.join(names)}")
        server = _ask(flags, flags.server, "Which server holds the board",
                      ot.get("server") if ot.get("server") in names else names[0] if len(names) == 1 else None, "--server",
                      f"one of {', '.join(names)}", lambda v: None if v in names else f"choose one of {', '.join(names)}")
        return {"type": "mcp", "mcp_config": str(p), "server": server}
    tracker = {"type": kind}
    if kind == "github-project":
        tracker["owner"] = _ask(flags, flags.owner, "GitHub user or organisation that owns the project", ot.get("owner"),
                                "--owner",
                                "the project's owner", lambda v: None if OWNER_RE.fullmatch(v) else f"bad GitHub owner {v!r}")
        sf = flags.status_field if flags.status_field is not None else ot.get("status_field") or github.STAGE_FIELD
        if sf.strip().lower() in ("", "status"):
            _fail("--status-field", "name a field for pl's stages; not blank and never GitHub's built-in Status field")
        if flags.project_number is not None:
            tracker["number"] = flags.project_number
        elif flags.create_project:
            tracker["_create"] = flags.title or f"pl {answers['slug']}"
        elif ot.get("number") is not None and not flags.interactive:
            tracker["number"] = ot["number"]
        elif not flags.interactive:
            _fail("--project-number", "missing (an existing project's number, or --create-project)")
        else:
            n = _ask(flags, None, "Project number (empty = create a new project)", str(ot.get("number") or ""),
                     "--project-number", "",
                     lambda v: None if v.isdigit() else "a project number is a whole number")
            if n:
                tracker["number"] = int(n)
            else:
                tracker["_create"] = _ask(flags, flags.title, "New project's title", f"pl {answers['slug']}", "--title", "")
    if "number" in tracker:
        tracker["status_field"] = sf
    tracker["repo"] = _ask(flags, flags.repo, "GitHub repository for the cards (owner/name)",
                           ot.get("repo") or _remote_repo(work), "--repo",
                           "owner/name; new cards are issues there",
                           lambda v: None if REPO_RE.fullmatch(v) else f"{v!r}: write it as owner/name")
    return tracker


def ask_gh(answers, flags):
    """[code_host] gh_config_dir: the default gh sign-in (None) or a separate folder. Checks exit codes only."""
    r = _run(["gh", "auth", "status"])
    logins = list(dict.fromkeys(LOGIN_RE.findall(r[1]))) if r and r[0] == 0 else []
    if r is None:
        print("gh is not installed: https://cli.github.com (pl uses it for GitHub trackers and PR checks)")
    else:
        print(f"gh sign-ins: {', '.join(logins) or 'none yet'}")
    folder = _ask(flags, flags.gh_config_dir, "GitHub sign-in: Enter for the default gh sign-in, or a folder for a separate one",
                  _old(answers, "code_host", "gh_config_dir") or "", "--gh-config-dir", "")
    if not folder:
        answers["gh_ok"], answers["gh_login"] = bool(logins), (logins or [None])[0]
        if not logins:
            answers["todo"].append(f"{'install gh (https://cli.github.com), then ' if r is None else ''}"
                                   "sign in to gh; run this yourself:  gh auth login -s project")
        return None
    path = Path(folder).expanduser()
    login = f"GH_CONFIG_DIR={shlex.quote(str(path))} gh auth login -s project"
    while (r := _run(["gh", "auth", "status"], env={**os.environ, "GH_CONFIG_DIR": str(path)})) is not None and r[0]:
        print(f"{_tilde(path)} has no gh sign-in yet. Run this yourself (pl never signs in for you):\n  {login}")
        if not flags.interactive or _input("Press Enter when you have signed in (or type skip): ").strip() == "skip":
            break
    answers["gh_ok"] = r is not None and r[0] == 0
    answers["gh_login"] = (LOGIN_RE.findall(r[1]) or [None])[0] if answers["gh_ok"] else None
    if not answers["gh_ok"]:
        answers["todo"].append(f"sign in to gh for {_tilde(path)}; run this yourself:  {login}")
    return _tilde(path)


def ask_user(answers, flags):
    """[user]: login = the chosen gh sign-in (GitHub trackers); email is optional (--email, or asked with Enter = skip).
    An existing profile's [user] values are the defaults."""
    old = _old(answers, "user") or {}
    on_github = answers["tracker"]["type"] != "mcp"
    email = flags.email if flags.email is not None else old.get("email")
    if on_github and flags.email is None and flags.interactive:
        email = _ask(flags, None, "Your email (optional; Enter skips)", old.get("email") or "", "--email", "")
    return {"login": (answers.get("gh_login") if on_github else None) or old.get("login"), "email": email or None}


def _label_check(v):
    """gh gets labels as argv: refuse what it could read as a flag, a comma (a list separator) and blanks."""
    if not v.strip() or v.startswith("-") or "," in v:
        return f"{v!r}: a label name must not be blank, start with '-' or contain ','"
    return None


def ask_labels(answers, flags, repo):
    """[code_host.labels], the five PR labels. None with --no-labels or when no GitHub repo is known."""
    if flags.no_labels or not repo:
        return None
    if not REPO_RE.fullmatch(repo):
        _fail("--repo", f"{repo!r}: write it as owner/name")
    old = _old(answers, "code_host", "labels") or {}
    labels = {k: getattr(flags, "label_" + k) or str(old.get(k) or d) for k, (d, _, _) in LABELS.items()}
    if flags.interactive:
        print("PR labels:\n" + "\n".join(f"  {k:<12} {v}" for k, v in labels.items()))
        if _input("PR labels (Enter keeps the defaults): ").strip():
            labels = {k: _ask(flags, None, f"  {k} label", v, LABEL_FLAGS[k], "", _label_check) for k, v in labels.items()}
    for k, v in labels.items():
        if err := _label_check(v):
            _fail(LABEL_FLAGS[k], err)
    return labels


def create_labels(answers, flags, repo):
    """Create the chosen labels the repo lacks, after consent. Never edits or deletes a label that exists."""
    if flags.create_labels is False:
        return
    gh_dir = answers["gh_config_dir"]
    env = {**os.environ, "GH_CONFIG_DIR": str(Path(gh_dir).expanduser())} if gh_dir else None
    prefix = f"GH_CONFIG_DIR={shlex.quote(str(Path(gh_dir).expanduser()))} " if gh_dir else ""
    argv = {}
    for k, name in answers["labels"].items():   # a name used twice: the first key's color wins
        argv.setdefault(name, ["gh", "label", "create", name, "--repo", repo, "--color", LABELS[k][1],
                               "--description", LABELS[k][2]])
    if answers["tracker"].get("type") == "github-project":   # the default issue intake's label
        argv.setdefault(C.START_LABEL, ["gh", "label", "create", C.START_LABEL, "--repo", repo, "--color", "C2E0C6",
                                        "--description", "Adopt this issue into the pl funnel"])
    have = None
    if answers.get("gh_ok"):
        r = _run(["gh", "label", "list", "--repo", repo, "--limit", "500", "--json", "name"], env=env, stdout_only=True)
        try:
            have = {x["name"] for x in json.loads(r[1])} if r and r[0] == 0 else None
        except (ValueError, TypeError, KeyError):
            have = None
    if have is None:
        answers["todo"].append(f"pl could not list the labels of {repo}; after signing in to gh, create the PR labels "
                               "it lacks; run these yourself:\n" + "\n".join(f"  {prefix}{shlex.join(x)}" for x in argv.values()))
        return
    missing = [n for n in argv if n not in have]
    if not missing:
        return
    yes = flags.create_labels
    if yes is None:
        yes = not flags.interactive or _input(f"Create {len(missing)} labels on {repo}? [Y/n]: ").strip().lower() not in ("n", "no")
    if not yes:
        print(f"Not creating labels. PR checks need them on {repo}:\n" +
              "\n".join(f"  {prefix}{shlex.join(argv[n])}" for n in missing))
        return
    for n in missing:
        r = _run(argv[n], env=env)
        if r and r[0] == 0:
            print(f"created the label {n} on {repo}")
        else:
            answers["todo"].append(f"create the label {n} on {repo}; run this yourself:  {prefix}{shlex.join(argv[n])}")


def ask_work_dir(answers, flags):
    here = _old(answers, "paths", "work_dir") or _git_top(Path.cwd())
    d = _ask(flags, flags.work_dir, "Work folder (where agents run)", str(here) if here else None, "--work-dir",
             "the folder agents run in", lambda v: None if Path(v).expanduser().is_dir() else f"{v} is not a folder")
    return _tilde(d)


def ask_attention(answers, flags):
    """A command given (or kept) wins; with none, offer to write the profile's own notify script."""
    v = None if (flags.notify_script and not flags.attention_cmd) else _ask(flags, flags.attention_cmd, "Notification command (Enter for none)",
             _old(answers, "paths", "attention_cmd") or "", "--attention-cmd", "",
             lambda v: None if Path(v).expanduser().is_file() and os.access(Path(v).expanduser(), os.X_OK)
             else f"{v} is not an executable file")
    if not v and _consent(flags, flags.notify_script, "Create a notification script? [Y/n]: "):
        answers["notify_script"] = True
        return f"~/.pl-{answers['slug']}/notify"
    return _tilde(v) if v else None


def write_notify_script(answers, d, force):
    """<profile>/notify, mode 0700. An existing file is kept unless --force."""
    if not answers.get("notify_script"):
        return
    p = d / "notify"
    if p.exists() and not force:
        print(f"kept the existing {_tilde(p)}; pl setup --force replaces it")
        return
    tmp = d / ".notify.tmp"
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as f:
        f.write(NOTIFY_SCRIPT)
    os.replace(tmp, p)
    print(f"wrote the notification script {_tilde(p)}")


# ---------- write and check ----------

def build_doc(a):
    """The config.toml document, through the same writer as pl profiles new."""
    v = C._defaults()
    v.update(WORK_DIR=Path(a["work_dir"]).expanduser(), ATTENTION=Path(a["attention"]).expanduser() if a["attention"] else None,
             ACCOUNTS=a["accounts"], STAGES={s: {"account": a["default_account"]} for s in STAGES},
             CODE_HOST={"owner": a["owner"], "labels": a["labels"] or {}})
    intake = {**v["INTAKE"], "columns": list(v["INTAKE"]["columns"])}
    doc = {"name": a["name"], **profiles._doc(v, {k: x for k, x in a["tracker"].items() if k != "_create"}, intake)}
    if a["gh_config_dir"]:
        doc["code_host"]["gh_config_dir"] = a["gh_config_dir"]
    if user := profiles._clean(a["user"]):
        doc["user"] = {**doc.get("user", {}), **user}
    if not a["labels"]:
        doc["code_host"].pop("labels", None)
    return doc


def merged_doc(a):
    """The old config.toml with setup's answers merged in; every key setup does not ask about is kept as it was.
    A tracker of another type replaces the old [tracker] table (its keys belong to the old type)."""
    doc = tomlkit.parse(a["old_bytes"].decode())
    t = {k: x for k, x in a["tracker"].items() if k != "_create"}
    if doc.get("tracker", {}).get("type") != t["type"]:
        doc["tracker"] = t
        t = {}
    new = profiles._clean({"name": a["name"], "user": a["user"], "paths": {"work_dir": a["work_dir"], "attention_cmd": a["attention"]},
                           "tracker": t, "accounts": a["accounts"],
                           "stages": {s: {"account": a["default_account"]} for s in STAGES},
                           "code_host": {"owner": a["owner"], "gh_config_dir": a["gh_config_dir"], "labels": a["labels"]}})

    def merge(into, v):
        for k, x in v.items():
            if isinstance(x, dict) and isinstance(into.get(k), MutableMapping):
                merge(into[k], x)
            else:
                into[k] = x
    merge(doc, new)
    if a["no_labels"] and "code_host" in doc:
        doc["code_host"].pop("labels", None)
    return doc


def _gh_as(gh_dir, fn, *args):
    """Run a pl.trackers.github call with the chosen gh sign-in folder (the profile is not loaded yet)."""
    saved = C.GH_CONFIG_DIR
    C.GH_CONFIG_DIR = Path(gh_dir).expanduser() if gh_dir else None
    try:
        return fn(*args)
    finally:
        C.GH_CONFIG_DIR = saved


def _create_project(tracker, owner, gh_dir):
    made = _gh_as(gh_dir, github.GitHubProject.create_project, owner, tracker["_create"], C.COLUMNS)
    print(f"created GitHub Project {made['url']} (stage field \"{made['status_field']}\")")
    return {**tracker, "number": int(made["number"]), "status_field": made["status_field"]}, made["url"]


def check_stage_field(answers, flags):
    """An existing Project: make sure its stage field has every pl column. Returns the Project URL.
    Missing field: created with every column. Missing options: after consent, added in one update that passes every
    existing option with its id unchanged, then checked. GitHub's Status field is never touched."""
    t, gh_dir, cols = answers["tracker"], answers["gh_config_dir"], list(C.COLUMNS)
    owner, number, name = t["owner"], t["number"], t["status_field"]
    if answers.get("gh_ok") is False:
        answers["todo"].append(f"after signing in, make sure project {owner} {number} has a single-select field "
                               f"{name!r} with the options {', '.join(cols)}")
        return f"https://github.com/{owner}?tab=projects (project {number})"
    url = _gh_as(gh_dir, github.project_url, owner, number)
    f = _gh_as(gh_dir, github.stage_field, owner, number, name)
    if f is None:
        _gh_as(gh_dir, github.create_stage_field, owner, number, name, cols)
        print(f"added the single-select field {name!r} to {url} with the options {', '.join(cols)}")
        return url
    if f.get("type", "ProjectV2SingleSelectField") != "ProjectV2SingleSelectField":
        _fail("--status-field", f"the {name!r} field of {url} is not a single-select field; pick another name")
    have = {o.get("name") for o in f.get("options") or []}
    missing = [c for c in cols if c not in have]
    if not missing:
        return url
    print(f"The {name!r} field of {url} lacks these options: {', '.join(missing)}")
    todo = f"add these options to the {name!r} field of {url}, keeping the ones it has: {', '.join(missing)}"
    if not _consent(flags, flags.add_missing_stages, "Add them to the field, keeping every option it has? [Y/n]: "):
        answers["todo"].append(todo)
        return url
    try:
        _gh_as(gh_dir, _add_options, owner, number, name, cols)
        print(f"added {', '.join(missing)} to the {name!r} field")
    except SystemExit as e:
        print(f"pl setup: {e}", file=sys.stderr)
        answers["todo"].append(f"{e} ({todo})")
    return url


def _add_options(owner, number, name, cols):
    """One update: every existing option exactly as it is (id, name, color, description), then the missing ones."""
    p = github.project_graph(owner, number)
    f = next((f for f in p["fields"] if f.get("name") == name and f.get("dataType") == "SINGLE_SELECT"), None)
    if f is None:
        raise SystemExit(f"pl: the Project has no single-select field {name!r}")
    old = f.get("options") or []
    names = {o["name"] for o in old}
    keep = [{"id": o["id"], "name": o["name"], "color": o["color"], "description": o.get("description") or ""} for o in old]
    github.set_field_options(f["id"], keep + [{"name": c, "color": "GRAY", "description": ""} for c in cols if c not in names])
    after = {o["id"] for g in github.project_graph(owner, number)["fields"] if g.get("id") == f["id"]
             for o in g.get("options") or []}
    if lost := [o["name"] for o in old if o["id"] not in after]:
        raise SystemExit(f"pl: the options {', '.join(lost)} of the {name!r} field got new ids, so cards set to them may "
                         "have lost their stage; check the field and those cards in GitHub")


def setup_views(answers, flags, url):
    """The Pipeline board, the Needs you table and, if asked, a Sprint field and a This sprint board.
    Idempotent by name. Nothing is deleted or renamed except a lone default "View 1", which becomes Pipeline.
    Returns True when a Pipeline view exists afterwards. A gh error becomes an ACTION NEEDED line."""
    t = answers["tracker"]
    if answers.get("gh_ok") is False:
        return False
    try:
        return _gh_as(answers["gh_config_dir"], _views, flags, t["owner"], t["number"], t["status_field"])
    except SystemExit as e:
        answers["todo"].append(f"{e}; pl could not finish the Project's views on {url}. Run pl setup again, "
                               "or add them by hand as docs/github.md says")
        return False


def _new_view(owner, number, project_id, name, layout, filter_=None):
    """Create a view, then find it by its new id (the create call takes no filter) and set the filter."""
    before = {v["id"] for v in github.project_graph(owner, number)["views"]}
    github.create_view(project_id, name, layout)
    if filter_:
        new = [v for v in github.project_graph(owner, number)["views"] if v["id"] not in before and v["name"] == name]
        if not new:
            raise SystemExit(f"pl: created the view {name!r} but cannot find it to set its filter {filter_}")
        github.update_view(new[0]["id"], filter=filter_)
    print(f"added the view {name!r}")


def _views(flags, owner, number, sf):
    p = github.project_graph(owner, number)
    views = {v["name"]: v for v in p["views"]}
    fields = {f.get("name") for f in p["fields"]}
    needs = f'"{sf}":' + ",".join(f'"{C.STAGE_DONE_AT[s]}"' for s in ("spec", "plan"))
    if ("Pipeline" not in views or "Needs you" not in views) and \
            _consent(flags, flags.views, "Set up the Project's views (Pipeline board, Needs you)? [Y/n]: "):
        if "Pipeline" not in views:
            if len(p["views"]) == 1 and p["views"][0]["name"] == "View 1":
                github.update_view(p["views"][0]["id"], name="Pipeline", layout="BOARD_LAYOUT")
                print("renamed the view 'View 1' to 'Pipeline' and made it a board")
            else:
                _new_view(owner, number, p["id"], "Pipeline", "BOARD_LAYOUT")
            views["Pipeline"] = True
        if "Needs you" not in views:
            _new_view(owner, number, p["id"], "Needs you", "TABLE_LAYOUT", needs)
    weeks = flags.sprint_weeks
    if ("Sprint" not in fields or "This sprint" not in views) and \
            _consent(flags, flags.sprint, f"Add a {weeks}-week Sprint field and a 'This sprint' board? [y/N]: ", False):
        if "Sprint" not in fields:
            github.create_iteration_field(p["id"], "Sprint", datetime.date.today().isoformat(), 7 * weeks)
            print(f"added the {weeks}-week iteration field 'Sprint'")
        if "This sprint" not in views:
            _new_view(owner, number, p["id"], "This sprint", "BOARD_LAYOUT", "sprint:@current")
    return "Pipeline" in views


def _in_clone():
    """True under `uv run` in a clone: the running pl is <clone>/.venv/bin/pl, which no new terminal will find."""
    return sys.prefix != sys.base_prefix and Path(sys.prefix).name == ".venv"


def path_check(mine=None) -> str | None:
    """None when `pl` on PATH is this install; else what is wrong and the exact fix."""
    if mine is None and _in_clone():
        return _clone_check()
    mine = Path(mine) if mine else Path(sys.executable).parent / "pl"
    return _compare(mine, shutil.which("pl"), "your PATH")


def _clone_check():
    """From a clone, what matters is the uv tool install and the PATH of the user's login shell."""
    r = _run(["uv", "tool", "list"])
    if not (r and r[0] == 0 and re.search(r"^pl-funnel\b", r[1], re.M)):
        return ("This pl runs from the clone's .venv and pl is not installed as a tool, so a new terminal will not find it.\n"
                f"Fix: install it as a tool from the clone:\n  uv tool install {shlex.quote(str(Path(sys.prefix).parent))}")
    r = _run(["uv", "tool", "dir", "--bin"])
    bin_ = Path(r[1].strip().splitlines()[-1]) if r and r[0] == 0 and r[1].strip() else Path.home() / ".local" / "bin"
    r = _run([os.environ.get("SHELL") or "/bin/sh", "-lc", "command -v pl"], stdout_only=True)
    lines = r[1].strip().splitlines() if r and r[0] == 0 else []
    return _compare(bin_ / "pl", lines[-1] if lines else None, "your login shell's PATH")


def _compare(mine, found, where):
    fix = (f"Fix: run  uv tool update-shell  and open a new terminal, or add this line to your shell profile:\n"
           f"  export PATH=\"{mine.parent}:$PATH\"")
    if found is None:
        return f"pl is not on {where}.\n{fix}"
    try:
        if Path(found).resolve() == mine.resolve():
            return None
    except OSError:
        pass
    return f"The pl first on {where} is {found}, not this install ({mine}).\n{fix}"


def _parser():
    ap = argparse.ArgumentParser(prog="pl setup", description="Create a profile by answering a few questions. "
                                 "With --yes every answer comes from the flags and nothing is asked.")
    ap.add_argument("--yes", action="store_true", help="never prompt; a missing required answer exits 2 naming its flag")
    ap.add_argument("--name", help="a friendly name for the profile")
    ap.add_argument("--slug", help="its folder name: ~/.pl-SLUG (default: from --name)")
    ap.add_argument("--harness", action="append", choices=list(HARNESSES), help="a harness to use (repeat for more)")
    ap.add_argument("--config-dir", action="append", default=[], metavar="HARNESS=PATH",
                    help="the harness's config folder (default: claude ~/.claude, codex ~/.codex)")
    ap.add_argument("--default-account", metavar="HARNESS", help="the account every stage uses (default: the first harness)")
    ap.add_argument("--tracker", choices=TRACKERS, help="where cards live")
    ap.add_argument("--owner", help="GitHub user or organisation (GitHub Project owner; PR checks)")
    ap.add_argument("--project-number", type=int, help="an existing GitHub Project's number")
    ap.add_argument("--create-project", action="store_true", help="create a new GitHub Project")
    ap.add_argument("--title", help="the new project's title (default: pl SLUG)")
    ap.add_argument("--status-field", metavar="NAME",
                    help="an existing project's single-select field for pl's stages (default: pl stage; never Status)")
    ap.add_argument("--add-missing-stages", action=argparse.BooleanOptionalAction, default=None,
                    help="add the options the stage field lacks, keeping every option it has and its id "
                         "(default: yes with --yes; asked otherwise)")
    ap.add_argument("--views", action=argparse.BooleanOptionalAction, default=None,
                    help="GitHub Project: add a Pipeline board and a Needs you view (default: yes with --yes; asked otherwise)")
    ap.add_argument("--sprint", action=argparse.BooleanOptionalAction, default=None,
                    help="GitHub Project: add a Sprint iteration field and a This sprint board (default: no)")
    ap.add_argument("--sprint-weeks", type=int, default=2, metavar="N", help="sprint length in weeks (default: 2)")
    ap.add_argument("--repo", help="owner/name: the repository cards live in (default: the work folder's GitHub remote)")
    ap.add_argument("--mcp-config", metavar="PATH", help="the harness's MCP config file (default: WORK/.mcp.json, then ~/.claude.json)")
    ap.add_argument("--server", help="the MCP server that holds the board")
    ap.add_argument("--gh-config-dir", metavar="PATH", help="a separate gh sign-in folder (default: the default gh sign-in)")
    ap.add_argument("--email", help="your email for cards and approvals (optional)")
    ap.add_argument("--work-dir", metavar="PATH", help="where agents run (default: this folder if it is a git repo)")
    ap.add_argument("--attention-cmd", metavar="CMD", help="a notification command (default: none)")
    ap.add_argument("--notify-script", action=argparse.BooleanOptionalAction, default=None,
                    help="with no --attention-cmd, write a notification script to the profile folder and use it "
                         "(default: yes with --yes; asked otherwise)")
    ap.add_argument("--force", action="store_true", help="replace an existing notification script")
    for k, (name, _, _) in LABELS.items():
        ap.add_argument(LABEL_FLAGS[k], metavar="NAME", help=f"the {k} PR label (default: {name})")
    ap.add_argument("--no-labels", action="store_true", help="write no PR labels (pl then does not track PRs)")
    ap.add_argument("--create-labels", action=argparse.BooleanOptionalAction, default=None,
                    help="create the PR labels the repo lacks (default: yes with --yes; asked otherwise)")
    return ap


def cmd_setup(argv: list[str]):
    """`pl setup [flags]`. Runs before any profile loads: the profile does not exist yet."""
    flags = _parser().parse_args(argv)
    flags.interactive = not flags.yes and sys.stdin.isatty()
    if not 1 <= flags.sprint_weeks <= 26:
        _fail("--sprint-weeks", "a sprint is 1 to 26 weeks")
    a = {"todo": []}
    a["name"], a["slug"] = ask_name(a, flags)
    a["accounts"], a["default_account"] = ask_harnesses(a, flags)
    a["tracker"] = ask_tracker(a, flags)
    a["gh_config_dir"] = ask_gh(a, flags)
    a["user"] = ask_user(a, flags)
    repo = a["tracker"].get("repo") or flags.repo
    a["labels"], a["no_labels"] = ask_labels(a, flags, repo), flags.no_labels
    a["owner"] = flags.owner or (a["tracker"].get("repo") or "").partition("/")[0] or _old(a, "code_host", "owner")
    a["work_dir"] = ask_work_dir(a, flags)
    a["attention"] = ask_attention(a, flags)
    d = Path.home() / f".pl-{a['slug']}"
    t = a["tracker"]
    print(f"\nProfile {a['name']} in {_tilde(d)}\n  harnesses  {', '.join(a['accounts'])} (stages use {a['default_account']})\n"
          f"  cards      {t['type']} {t.get('repo') or t.get('server') or ''}{' (new project)' if '_create' in t else ''}\n"
          f"  gh sign-in {a['gh_config_dir'] or 'the default'}\n  work folder {a['work_dir']}\n"
          f"  notifications {a['attention'] or 'none'}\n  PR labels  {', '.join(a['labels'].values()) if a['labels'] else 'none'}" +
          (f"\n  defaults   issue intake and auto-review loop on (assigned to you or labelled {C.START_LABEL}; "
           "off with [intake] or [loops.auto-review] enabled = false)" if t["type"] == "github-project" else ""))
    if flags.interactive and _input("Write this profile? [Y/n]: ").strip().lower() in ("n", "no"):
        raise SystemExit("pl setup: nothing written")
    if not a["exists"]:
        profiles._target(a["slug"])   # appeared while we asked: never overwrite
    url = None
    if "_create" in t:
        a["tracker"], url = _create_project(t, t["owner"], a["gh_config_dir"])
    elif t["type"] == "github-project":
        url = check_stage_field(a, flags)
    if a["old_bytes"] is not None:
        write_notify_script(a, d, flags.force)   # before the check: attention_cmd must be an executable file
        doc = merged_doc(a)
        bak = d / f"config.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        with os.fdopen(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(a["old_bytes"])
        if errs := C.validate(doc):   # checked before the old file is replaced, so it never needs restoring
            print(f"pl setup: not saved; {d / 'config.toml'} is unchanged:\n" + "\n".join(f"  {e}" for e in errs),
                  file=sys.stderr)
            raise SystemExit(3)
        C.save(doc, d / "config.toml")   # temp file 0600, fsync, os.replace
        print(f"updated {_tilde(d)}/config.toml (the old one is {bak.name})")
    else:
        doc = build_doc(a)
        if a["exists"]:   # the folder is there without a config.toml
            with os.fdopen(os.open(d / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(tomlkit.dumps(doc))
        else:
            profiles._write_config(d, doc)
        print(f"created {_tilde(d)}/config.toml")
        write_notify_script(a, d, flags.force)
    errs = C.validate(doc)
    if errs:
        print("pl setup: the profile was written but does not pass its checks; edit "
              f"{d / 'config.toml'}:\n" + "\n".join(f"  {e}" for e in errs), file=sys.stderr)
        raise SystemExit(3)
    if t["type"] == "mcp":
        a["todo"].append(f"add a [tracker.tools] table that maps pl's card actions to {t['server']}'s tools; "
                         f"see {MCP_DOC} in the pl repository (pl list fails until then)")
    optional = []
    if url and setup_views(a, flags, url):
        optional.append(f"one step GitHub's API cannot do, on {url}: open the Pipeline view and set \"Column by\" "
                        f"to the \"{a['tracker']['status_field']}\" field")
    elif url:
        optional.append(f"two steps GitHub's API cannot do, on {url}: 1. switch the view to Board; "
                        f"2. set \"Column by\" to the \"{a['tracker']['status_field']}\" field")
    if a["labels"]:
        create_labels(a, flags, repo)
    if not (doc.get("code_host") or {}).get("labels"):
        print("PR checks are off until [code_host] labels are set in config.toml.")
    if msg := path_check():
        print(msg)
        try:
            yes = flags.interactive and "uv tool update-shell" in msg and \
                input("Run uv tool update-shell now? [y/N]: ").strip().lower() in ("y", "yes")
        except EOFError:
            yes = False
        r = _run(["uv", "tool", "update-shell"]) if yes else None
        if yes:
            print(r[1].strip() if r else "uv is not installed")
        if not (r and r[0] == 0):
            a["todo"].append(msg.replace("\n", " "))
    s = a["slug"]
    print(f"\nNext:\n  alias pl-{s}='PL_CONFIG_DIR=~/.pl-{s} pl'\n  pl --profile {s} list\n  pl --profile {s}")
    for todo in optional:
        print(f"ACTION NEEDED (optional): {todo}")
    for todo in a["todo"]:
        print(f"ACTION NEEDED: {todo}")
    if a["todo"]:
        raise SystemExit(3)
