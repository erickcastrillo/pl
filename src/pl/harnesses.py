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
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pl import config as C
from pl import events


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
        "antigravity", "agy", None, ["agy", "-i", "{prompt}"], ["agy", "-p", "{prompt}"],
        # agy 1.3.1 shows the server's wording (not in its binary), wrapped at any width: "⚠ Individual quota reached.
        # Please upgrade your subscription to increase your limits. Resets in 4h9m46s." (C.RESET_IN_RE reads the time)
        [r"Individual\s+quota\s+reached", r"quota\s+reached\.\s+Please\s+upgrade\s+your\s+subscription"],
        experimental=True,
        note="agy reads an interactive prompt only from -i (--prompt-interactive): a bare prompt argument is refused; "
             "headless (-p) cannot prompt for tool permissions, so allow the tools in agy's own settings (pl never adds "
             "--dangerously-skip-permissions)"),
}
FIELDS = {f.name for f in dataclasses.fields(Harness)}

LOGIN = {"claude": ["claude", "/login"], "codex": ["codex", "login"], "antigravity": ["agy"]}   # sign-in command, run inside the sandbox VM


def login_argv(h):
    return list(LOGIN.get(h.name, []))


# The flags that let a pipeline agent or loop work without stopping for permission; never the Assistant. From each
# CLI's --help: claude 2.1.286 (auto: a safety classifier approves routine actions and blocks risky ones), codex 0.150.1
# (never ask, writes only inside the workspace), agy and gemini 0.32.1 (edits only: their one mode that never asks
# skips every check, so pl keeps asking for commands, see STILL_ASKS). pl never uses a yolo, bypass or skip mode.
UNATTENDED = {"claude": ["--permission-mode", "auto"],
              "codex": ["--ask-for-approval", "never", "--sandbox", "workspace-write"],
              "antigravity": ["--mode", "accept-edits"],
              "gemini": ["--approval-mode", "auto_edit"]}
# The harness's own read-only mode, for a loop with writes = false. A harness missing here cannot run one.
READ_ONLY = {"claude": ["--permission-mode", "plan"], "codex": ["--sandbox", "read-only"]}
STILL_ASKS = {"antigravity": "allow the commands in agy's own settings",
              "gemini": "allow the commands with a gemini policy file (--policy)"}
# A permission prompt on screen (strings of each CLI's binary at the versions above; claude's read-outside prompt from
# claude 2.1.295: "Allow this read outside the working directories?" / "1. Yes, and keep allowing any reads outside
# the working directories"). Each pattern matches one screen line, so it stays short enough not to wrap.
PERMISSION_PATTERNS = {
    "claude": [r"Do you want to (?:proceed|make this edit to|create|allow)", r"Yes, and don't ask again for",
               r"Allow\s+this\s+read\s+outside", r"Yes,\s+and\s+keep\s+allowing"],
    "codex": [r"Would you like to (?:run the following command|make the following edits|grant)"],
    "antigravity": [r"Allow (?:access to this|calling this tool|creation of this file|administrator elevation)",
                    r"Approve this action\?", r"Run this command\?", r"Do you want to proceed\?"],
    "gemini": [r"Allow execution of", r"Apply this change\?", r"Do you want to proceed\?"]}
# A first-run folder trust prompt on screen. Only Claude Code asks it; pl never answers it (the person does, once per folder).
TRUST_PATTERNS = {"claude": [r"Accessing workspace", r"Yes, I trust this folder", r"Quick safety check"]}
# A template token that already chooses a permission or sandbox mode (the Assistant refuses such a template too).
PERMISSION_FLAG_RE = re.compile(r"dangerously|bypass|yolo|full-auto|approve-for-me|permission|approval|sandbox|dontask"
                                r"|acceptedits|accept-edits|allowed-?tools|settings|^-[as]$|^--mode(?:=|$)", re.I)
BYPASS_RE = re.compile(r"dangerously-skip|bypass|yolo|danger-full-access|full-auto|dontask", re.I)


# Model and reasoning effort, from each CLI's --help: claude 2.1.295 (--model takes an alias such as 'fable', 'opus'
# or 'sonnet', or a full name; --effort low..max), agy 1.3.1 (--model, --effort low..max; `agy models` lists them),
# codex 0.150.1 (-c model=..., -c model_reasoning_effort=...; `codex debug models --bundled` prints its catalog as JSON
# without a network call). A harness missing from MODEL_FLAGS or EFFORT_FLAGS takes no model or no effort.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_FLAGS = {"claude": ["--model", "{model}"], "antigravity": ["--model", "{model}"], "codex": ["-c", 'model="{model}"']}
EFFORT_FLAGS = {"claude": ["--effort", "{effort}"], "antigravity": ["--effort", "{effort}"],
                "codex": ["-c", 'model_reasoning_effort="{effort}"']}
EFFORT_LEVELS = {"claude": EFFORTS, "antigravity": EFFORTS, "codex": EFFORTS}
CLAUDE_MODELS = ["opus", "sonnet", "haiku", "fable", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                 "claude-fable-5-1"]
MODEL_LIST_ARGV = {"antigravity": ["agy", "models"], "codex": ["codex", "debug", "models", "--bundled"]}
MODEL_LIST_TIMEOUT = 20
MODEL_TTL = 600          # seconds a model list read from a CLI is kept
_MODELS = {}             # harness name -> (monotonic time, [model ids])
# a template token that already chooses the model or the effort (then pl adds none)
MODEL_FLAG_RE = re.compile(r"^(?:--model|-m)(?:=|$)|^model=", re.I)
EFFORT_FLAG_RE = re.compile(r"^--effort(?:=|$)|reasoning_effort=", re.I)
# The folder agents make their git worktrees in ([paths] worktrees_dir), added to the workspace so reading and writing
# there never asks. Each CLI's --help: claude 2.1.296 (--add-dir <directories...>: the = form keeps it to one value),
# codex 0.150.1 (writable alongside the workspace), agy 1.3.2 (added to the workspace).
ADD_DIR_FLAGS = {h: ["--add-dir={worktrees_dir}"] for h in ("claude", "codex", "antigravity")}
ADD_DIR_RE = re.compile(r"^--add-dir(?:=|$)")
WORKTREES_ENV = "PL_WORKTREES_DIR"   # the same folder for the agent's shell commands (the built-in skills use it)
XDIST_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
# a model id pl passes: no whitespace, quote or backslash, and no leading dash (it is never read as a flag)
MODEL_RE = re.compile(r"[^\s\"'\\-][^\s\"'\\]*")


def shared_levels(names):
    """The effort levels every one of these harnesses takes, in order (all of EFFORTS for none)."""
    return [x for x in EFFORTS if all(x in EFFORT_LEVELS.get(n, ()) for n in names)]


def _parse_models(name, out):
    if name == "codex":
        return [m["slug"] for m in json.loads(out)["models"] if m.get("visibility") == "list" and m.get("slug")]
    return [ln.split("\t")[0].strip() for ln in out.splitlines() if "\t" in ln and ln.split("\t")[0].strip()]


def models(name):
    """The model ids an account of this harness can pick: claude's built-in list; agy's and codex's from their CLI,
    kept MODEL_TTL seconds; [] when the list cannot be read (the person types one)."""
    if name == "claude":
        return list(CLAUDE_MODELS)
    if name not in MODEL_LIST_ARGV:
        return []
    hit = _MODELS.get(name)
    if hit and time.monotonic() - hit[0] < MODEL_TTL:
        return list(hit[1])
    try:
        r = _run(MODEL_LIST_ARGV[name], timeout=MODEL_LIST_TIMEOUT)
        out = _parse_models(name, r.stdout) if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        out = []
    if out:
        _MODELS[name] = (time.monotonic(), out)
    return list(out)


BUDGET_RE = re.compile(r"^budget:\s*(\d+)\s+files?\s+changed\b.*?\babout\s+(\d+)\s+lines?\b", re.M | re.I)


def plan_budget(text):
    """(files, lines) of a PLAN's budget line, None when it has none pl can read."""
    m = BUDGET_RE.search(text or "")
    return (int(m[1]), int(m[2])) if m else None


def model_effort(h, account, stage=None, small=False):
    """(model, effort) a launch of h under account passes, None for each that is unset or that h does not take.
    The model is the stage's small-change one for h's harness when small, else the stage's for h's harness, else
    the account's (only when h is the account's own harness); the effort is the stage's, else the account's."""
    a = C.ACCOUNTS.get(account) or {}
    cfg = C.STAGES.get(stage) or {}
    sm = cfg.get("models") if stage else None
    sm = sm.get(h.name) if isinstance(sm, dict) else None
    if small and h.name in MODEL_FLAGS:
        small_models = (cfg.get("small") or {}).get("models")
        pick = small_models.get(h.name) if isinstance(small_models, dict) else None
        if isinstance(pick, str) and MODEL_RE.fullmatch(pick):
            sm = pick
    if h.name in MODEL_FLAGS and isinstance(sm, str) and MODEL_RE.fullmatch(sm):
        model = sm
    else:
        model = a.get("model") if (a.get("harness") or "claude") == h.name and h.name in MODEL_FLAGS else None
        model = model if isinstance(model, str) and MODEL_RE.fullmatch(model) else None
    effort = (C.STAGES.get(stage) or {}).get("effort") if stage else None
    effort = effort or a.get("effort")
    return model, (effort if effort in EFFORT_LEVELS.get(h.name, ()) else None)


def model_reason(h, account, stage=None, small=None, rework=False):
    """(model, reason) for the record of a launch: the model model_effort picks and where it came from. small is the
    (lines, files) budget of a small change; a template that sets its own model flag is reported as "template"."""
    if any(MODEL_FLAG_RE.search(t) for t in h.interactive or ()):
        return "template", f"[harnesses.{h.name}] template"
    model, _ = model_effort(h, account, stage, bool(small))
    cfg = C.STAGES.get(stage) or {}
    small_models = (cfg.get("small") or {}).get("models") or {}
    stage_models = cfg.get("models") or {}
    if not model:
        return "default", "harness default"
    if small and small_models.get(h.name) == model:
        reason = f"small change: {small[0]} lines, {small[1]} files"
    elif stage_models.get(h.name) == model:
        reason = f"stage {stage}, harness {h.name}"
    else:
        reason = f"account {account}"
    return model, reason + (" (rework)" if rework else "")


def worktrees_dir():
    """The profile's worktrees folder, made (0700) when missing; None when unset or it cannot be made."""
    d = C.WORKTREES_DIR
    if not d:
        return None
    try:
        Path(d).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return None
    return Path(d)


def _with_model(template, h, account, stage, worktrees=None, small=False):
    """(template, values): the model, effort and worktrees folder flags after the program name, unless the template
    sets that flag."""
    model, effort = model_effort(h, account, stage, small)
    add = []
    if model and not any(MODEL_FLAG_RE.search(t) for t in template):
        add += MODEL_FLAGS[h.name]
    if effort and not any(EFFORT_FLAG_RE.search(t) for t in template):
        add += EFFORT_FLAGS[h.name]
    if worktrees and h.name in ADD_DIR_FLAGS and not any(ADD_DIR_RE.search(t) for t in template):
        add += ADD_DIR_FLAGS[h.name]
    return [*template[:1], *add, *template[1:]], {"model": model, "effort": effort, "worktrees_dir": worktrees}


def unattended(h):
    """h with its unattended flags after the program name, for a pipeline agent or loop. h itself when [permissions]
    unattended = false, [harnesses.<name>] unattended = false, it has none, or its template sets a permission flag."""
    flags = UNATTENDED.get(h.name) if h.unattended and C.PERMISSIONS.get("unattended", True) is not False else None
    if not flags or any(PERMISSION_FLAG_RE.search(t) for t in (*h.interactive, *h.resume)):
        return h
    return dataclasses.replace(h, interactive=[*h.interactive[:1], *flags, *h.interactive[1:]],
                               resume=[*h.resume[:1], *flags, *h.resume[1:]] if h.resume else [])


def refuse_bypass(h):
    """Raise SystemExit when h's template bypasses permissions, naming the [harnesses.<name>] key."""
    if any(BYPASS_RE.search(t) for t in (*h.interactive, *h.headless, *h.resume)):
        raise SystemExit(f"pl: [harnesses.{h.name}] template bypasses permissions; stages and loops refuse bypass modes")
    return h


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
    h = get(st["harness"]) if st.get("harness") else account_harness(account)
    if st.get("reader") and h.name not in ("claude", "codex"):
        raise SystemExit(f"pl: [stages.{stage}] reader stages support only claude and codex, not {h.name}")
    return h, account


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


def mcp_flags(h, servers, profile, card_id, stage, window):
    """The flags that give a harness only these connectors, each as a `pl connectors serve` proxy command (no URL, no token)."""
    if not servers:
        return []
    if h.name not in ("claude", "codex"):
        events.emit("error", card_id, message=f"{h.name} cannot take connectors; none given to stage {stage}")
        return []
    def cmd(name):
        return [sys.executable, "-m", "pl", "--profile", profile, "connectors", "serve", name,
                "--card", str(card_id), "--stage", str(stage)]
    if h.name == "codex":
        return [f for n in servers for f in ("-c", f"mcp_servers.{n}.command={json.dumps(cmd(n)[0])}",
                                             "-c", f"mcp_servers.{n}.args={json.dumps(cmd(n)[1:])}")]
    f = C.STATE_DIR / "launch" / (re.sub(r"[^A-Za-z0-9._-]", "_", window) + ".mcp.json")
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"mcpServers": {n: {"command": cmd(n)[0], "args": cmd(n)[1:]} for n in servers}}, out)
    return [f"--mcp-config={f}", "--strict-mcp-config"]


# Without pl's python or package (upgraded, environment replaced) the agent's gh still runs: the next gh on the PATH.
_SHIM = """#!/bin/sh
dir=$(dirname "$0")
if {py} -c 'import importlib.util as u, sys; sys.exit(u.find_spec("pl.ghwrap") is None)' 2>/dev/null; then
  PL_GH_SHIM_DIR=$dir exec {py} -m pl.ghwrap "$@"
fi
PATH=$(printf %s "$PATH" | tr ':' '\\n' | grep -vxF "$dir" | paste -sd: -)
export PATH
exec gh "$@"
"""


def gh_shim():
    """Write the gh wrapper agents find first on their PATH (the real gh stays behind it); returns its folder."""
    folder = C.STATE_DIR / "bin"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    tmp = folder / f".gh.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(_SHIM.format(py=shlex.quote(sys.executable)))
    os.replace(tmp, folder / "gh")
    return folder


def _ledger_account():
    """The GitHub ledger key the wrapper records under (a login and a short token hash, never a token); None if unknown."""
    from pl import ghquota
    try:
        return ghquota.account()
    except (SystemExit, OSError):
        return None


def xdist_auto_workers():
    """pytest -n auto's worker count for one agent: the machine's test worker budget split over max_live_agents, at least 1."""
    from pl import manager, worker_budget
    return max(1, worker_budget.max_workers() // manager._limits(manager.managed())["max_live_agents"])


def launch_script(h, account, prompt, session_id, label, resume=False, plugin=True, stage=None, worktrees=True, cwd=None, card_id=None, hooks=None, small=False, servers=None):
    """The sh script an agent window runs: every element control-stripped and shlex-quoted. It deletes itself first
    (a script still on disk means the launch never ran), then execs the harness in its own place so the pane shows
    the harness and the window's shell comes back when it exits. resume: the harness's resume argv (with prompt when given).
    plugin: false keeps pl's skills library plugin out of this launch. stage: its [stages.<stage>] effort wins.
    small: the stage's small-change model for h's harness wins over its normal one.
    worktrees: the agent may make git worktrees, so the worktrees folder is made, added and exported (not the Assistant).
    hooks: (card id, window) of a stage agent; a Claude one then reports its events to pl hook.
    servers: connector names this stage agent's harness gets, through the pl proxy."""
    from pl import hooks as pl_hooks, pods, skills
    pod = pods.launch_pod()
    pdir = skills.launch_plugin(h, plugin, pod)
    prompt = skills.library_prompt(h, account, prompt, pdir, pod) if prompt else prompt
    wt = worktrees_dir() if worktrees else None
    template, values = _with_model(h.resume if resume else h.interactive, h, account, stage, wt, small)
    argv = skills.with_plugin(_argv(template, {**values, "prompt": prompt, "session_id": session_id, "label": label}),
                              pdir)
    if hooks and stage and h.name == "claude" and pl_hooks.enabled():
        argv = [argv[0], "--settings", pl_hooks.settings_json(*hooks), *argv[1:]]
    if servers and stage and hooks:
        argv = [argv[0], *mcp_flags(h, servers, C.PROFILE_NAME, hooks[0], stage, hooks[1]), *argv[1:]]
    d = C.PROFILES.get(account)
    lines = ["#!/bin/sh", 'rm -f -- "$0"']
    n = str(xdist_auto_workers())
    from pl import sandbox
    sb = sandbox.settings()
    if sb.get("enabled") and stage:
        vm = sandbox.vm_name()
        workdir = cwd or wt or C.WORK_DIR
        env_args = [f"{XDIST_ENV}={n}"]
        if stage:
            env_args.append(f"PL_STAGE={_typeable(str(stage))}")
        if wt:
            env_args.append(f"{WORKTREES_ENV}={_typeable(wt)}")
        exec_argv = ["multipass", "exec", vm, "--working-directory", str(workdir), "--", "env", *env_args, *argv]
        lines.append("exec " + " ".join(shlex.quote(_typeable(a)) for a in exec_argv))
    else:
        lines.append(f'export PATH={shlex.quote(_typeable(gh_shim()))}:"$PATH"')
        if card := (hooks[0] if hooks else card_id):
            lines.append(f"export PL_CARD={shlex.quote(_typeable(card))}")
        if not stage:
            lines.append(f"export PL_LOOP={shlex.quote(_typeable(label))}")
        if ledger := _ledger_account():
            lines.append(f"export PL_GH_LEDGER={shlex.quote(_typeable(ledger))}")
        if stage:
            lines.append(f"export PL_STAGE={shlex.quote(str(stage))}")
        if h.env_var and d:
            lines.append(f"export {h.env_var}={shlex.quote(_typeable(d))}")
        if wt:
            lines.append(f"export {WORKTREES_ENV}={shlex.quote(_typeable(wt))}")
        lines.append(f"export {XDIST_ENV}={n}")
        lines.append("exec " + " ".join(shlex.quote(_typeable(a)) for a in argv))
    return "\n".join(lines) + "\n"


def _shown(path):
    p = str(path)
    home = str(C.HOME)
    if p == home:
        return "~"
    if p.startswith(home + "/"):
        return "~/" + shlex.quote(p[len(home) + 1:])
    if p.startswith("~/"):
        return "~/" + shlex.quote(p[2:])
    return shlex.quote(p)


def trust_command(h, account, folder):
    """The command that saves folder trust where this account's agent reads it (same env as launch_script)."""
    if folder != "~" and not folder.startswith(("/", "~/")):
        folder = str(C.WORK_DIR)
    d = C.PROFILES.get(account)
    env = f"{h.env_var}={_shown(d)} " if h.env_var and d else ""
    return f"cd {_shown(folder)} && {env}{h.bin}"


def claude_json(account):
    d = config_dir(account)
    return (d or C.HOME) / ".claude.json"


def folder_trust(account, folder):
    """(trusted, reason): Claude's saved trust for folder or a parent, read from the account's .claude.json; reads only that file."""
    f = claude_json(account)
    if SECRET_RE.fullmatch(f.name):
        return False, "unreadable"
    try:
        projects = json.loads(f.read_text()).get("projects")
    except FileNotFoundError:
        return False, "file missing"
    except (OSError, ValueError, AttributeError):
        return False, "unreadable"
    if not isinstance(projects, dict):
        return False, "unreadable"
    given = Path(folder).expanduser()
    resolved = given.resolve()
    for q in (given, resolved, *resolved.parents):
        entry = projects.get(str(q))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True, None
    return False, None


def claude_trusts(account, folder):
    """True only when the account's Claude config marks exactly this folder trusted. pl only reads this one file and
    keeps nothing from it but the answer; it never writes it."""
    d = C.PROFILES.get(account)
    p = (Path(d) if d else C.HOME) / ".claude.json"
    if folder != "~" and not folder.startswith(("/", "~/")):
        folder = str(C.WORK_DIR)
    elif folder.startswith("~"):
        folder = str(C.HOME) + folder[1:]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    ent = projects.get(folder.removesuffix("/")) if isinstance(projects, dict) else None
    return isinstance(ent, dict) and ent.get("hasTrustDialogAccepted") is True


def headless_argv(h, account, prompt, stage=None):
    """(argv, env) for a one-shot run; no shell."""
    env = dict(os.environ)
    if stage:
        env["PL_STAGE"] = str(stage)
    d = C.PROFILES.get(account)
    if h.env_var and d:
        env[h.env_var] = str(d)
    if C.GH_CONFIG_DIR:
        env["GH_CONFIG_DIR"] = str(C.GH_CONFIG_DIR)
    env["PATH"] = f"{gh_shim()}{os.pathsep}{env.get('PATH', '')}"
    if ledger := _ledger_account():
        env["PL_GH_LEDGER"] = ledger
    from pl import pods, skills
    pod = pods.launch_pod()
    pdir = skills.launch_plugin(h, pod=pod)
    template, values = _with_model(h.headless, h, account, stage)
    return skills.with_plugin(_argv(template, {**values, "prompt": skills.library_prompt(h, account, prompt, pdir, pod)}),
                              pdir), env


def write_run_record(log_path, record_path, started_at, ended_at, exit_code):
    """What a headless loop run leaves for the dispatcher: the last non-empty output line as its one-line result, and
    the tokens from the last JSON line that carries a usage object ("n/a" when the harness gives none)."""
    lines = [ln.strip() for ln in Path(log_path).read_text(errors="replace").splitlines() if ln.strip()]
    tokens = "n/a"
    for ln in reversed(lines):
        try:
            usage = json.loads(ln).get("usage")
        except (ValueError, AttributeError):
            continue
        if isinstance(usage, dict):
            tokens = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int))
            break
    fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"started_at": started_at, "ended_at": ended_at, "result": (lines[-1] if lines else "")[:200],
                   "tokens": tokens, "exit": int(exit_code)}, f)


def headless_script(h, account, prompt, name, log_path, record_path, writes=True):
    """The sh script a scheduled loop's window runs: the harness once, headless and unattended (read-only when writes
    is false), its output in a 0600 log, then a 0600 run record (start, end, result, tokens, exit status). It deletes
    itself first."""
    argv, env = headless_argv(unattended_headless(h) if writes else read_only_headless(h), account, prompt)
    lines = ["#!/bin/sh", 'rm -f -- "$0"', "umask 077"]
    lines.append(f'export PATH={shlex.quote(_typeable(gh_shim()))}:"$PATH"')
    if "PL_GH_LEDGER" in env:
        lines.append(f"export PL_GH_LEDGER={shlex.quote(_typeable(env['PL_GH_LEDGER']))}")
    lines += [f"export {k}={shlex.quote(v)}" for k, v in loop_env(name, writes).items()]
    lines.append(f"export {XDIST_ENV}={xdist_auto_workers()}")
    if h.env_var and C.PROFILES.get(account):
        lines.append(f"export {h.env_var}={shlex.quote(_typeable(C.PROFILES[account]))}")
    if C.GH_CONFIG_DIR:
        lines.append(f"export GH_CONFIG_DIR={shlex.quote(_typeable(C.GH_CONFIG_DIR))}")
    now = "date -u +%Y-%m-%dT%H:%M:%S+00:00"
    log, record = (shlex.quote(str(p)) for p in (log_path, record_path))
    lines += [f"started=$({now})", " ".join(shlex.quote(_typeable(a)) for a in argv) + f" > {log} 2>&1 < /dev/null", "code=$?",
              f"ended=$({now})",
              f"{shlex.quote(sys.executable)} -I -c 'import sys; from pl.harnesses import write_run_record as w; w(*sys.argv[1:])' "
              f"{log} {record} \"$started\" \"$ended\" \"$code\""]
    return "\n".join(lines) + "\n"


def unattended_headless(h):
    """h with its unattended flags after the program name in its headless argv, unless it already sets a permission flag."""
    flags = UNATTENDED.get(h.name) if h.unattended and C.PERMISSIONS.get("unattended", True) is not False else None
    if not flags or any(PERMISSION_FLAG_RE.search(t) for t in h.headless):
        return h
    return dataclasses.replace(h, headless=[*h.headless[:1], *flags, *h.headless[1:]])


def read_only_headless(h):
    """h with its read-only flags after the program name in its headless argv, or None when it has no read-only mode
    or its template already sets a permission flag (pl never runs a read-only loop with writes)."""
    flags = READ_ONLY.get(h.name)
    if not flags or any(PERMISSION_FLAG_RE.search(t) for t in h.headless):
        return None
    return dataclasses.replace(h, headless=[*h.headless[:1], *flags, *h.headless[1:]])


def loop_env(name, writes, dry_run=False):
    """The environment a loop run gets; pl's own commands read PL_LOOP_WRITES and PL_DRY_RUN to gate their writes."""
    return {"PL_LOOP": name, **({} if writes else {"PL_LOOP_WRITES": "0"}), **({"PL_DRY_RUN": "1"} if dry_run else {})}


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
