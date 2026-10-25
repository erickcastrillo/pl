"""Settings. Other modules read them through the config module alias at call time, never by copying names."""
import os
import re
import shlex
import socket
import sys
from pathlib import Path
import tomllib

from pl import schedule

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _state_paths(g):
    for k, name in (("STATE_FILE", "pl-dispatch.json"), ("PAUSE_FILE", "pl-paused.json"),
                    ("SEEN_FILE", "pl-product-seen.json"), ("PROFILE_STATE", "pl-profiles.json"),
                    ("LOCK_FILE", "pl-dispatch.lock")):
        g[k] = g["ATTN"] / name


def _defaults():
    """Neutral defaults, computed from HOME now. Company values (boards, identity, prompts, loops) come only
    from a profile's config.toml; a missing required one fails where it is needed, naming the key."""
    HOME = Path.home()
    COLUMNS = ["Inbox", "Spec ready", "Plan for review", "Manual", "Approved", "In progress", "PR open", "Done"]  # Manual: needs a person, no agent runs there
    PLANS = HOME / ".pl" / "plans"            # a profile defaults to <profile dir>/plans
    ATTN = HOME / ".pl"                       # replaced by <profile dir>/state once a profile loads
    PROFILES = {}
    USER_EMAIL = USER_NAME = USER_UUID = USER_LOGIN = None
    WORK_DIR = HOME
    WORKTREES_DIR = None                      # [paths] worktrees_dir; a profile defaults to <work_dir>-pl-worktrees
    TMUX_SESSION = "pl"
    ATTENTION = None                          # [paths] attention_cmd; unset = no notifications
    HOST = socket.gethostname()
    # Intake board bridge: cards assigned to you there become funnel ideas.
    BOARD = PRODUCT_BOARD = None
    PRODUCT_INTAKE_COLUMNS = ["Triage", "Backlog"]
    PRODUCT_SKIP_TAGS = {"question", "hold", "no-funnel"}
    PROMPTS = {"spec": "", "design": "", "plan": "", "run": ""}   # [stages.<stage>] prompt
    STAGE_DONE_AT = {"spec": "Spec ready", "plan": "Plan for review", "run": "PR open"}  # design: DESIGN section

    LIMIT_RE = re.compile(r"You're out of usage credits|Usage limit reached ·|You[’']ve hit your (?:[\w-]+ )?limit|Claude usage limit reached"
                          r"|(?:usage|session|weekly|5-hour|hourly|daily) limit reached|Stop and wait for limit to reset", re.I)
    # the reset time on a limit screen: an optional date ("Oct 4", "October 4", "4 Oct"), then 9pm, 9:30pm or 21:30 (local time)
    _MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    RESET_RE = re.compile(r"(?:resets?|continu\w+ automatically|resum\w+)\s*(?:(?:at|on)\s+)?"
                          rf"(?:(?:(?P<mon>{_MON})\s+(?P<day>\d{{1,2}})|(?P<day2>\d{{1,2}})\s+(?P<mon2>{_MON}))\s*,?\s*(?:at\s+)?)?"
                          r"(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\b|(?P<h24>\d{1,2}):(?P<m24>\d{2}))", re.I)
    del _MON
    # a reset given as a time from now (Antigravity: "Resets in 4h9m46s", "Resets in 9m", "Resets in 2h")
    RESET_IN_RE = re.compile(r"resets?\s+in\s+(?=\d)(?:(?P<d>\d+)\s*d\s*)?(?:(?P<h>\d+)\s*h\s*)?(?:(?P<m>\d+)\s*m(?!s)\s*)?"
                             r"(?:(?P<s>\d+)\s*s\b)?", re.I)
    LIMIT_COOLDOWN = int(os.environ.get("PL_LIMIT_COOLDOWN", "3600"))  # seconds, when the screen names no reset time
    LIMIT_RESTART_WINDOW = int(os.environ.get("PL_LIMIT_RESTART_WINDOW", "600"))  # an agent younger than this is restarted on the other account; older ones auto-resume

    PRODUCT_COLUMNS = ["Triage", "Backlog", "Spec/Design", "In Progress", "Needs Testing", "Needs Human Review", "Done"]

    # Long-lived loops the dispatcher keeps alive in the tmux session, one window each (window name = key): [loops].
    SERVICES = {}
    LOOPS_OFF = {}        # [loops.<name>] enabled = false: not run, but written back by `pl profiles new --from-current`
    ISSUE_INTAKE = None   # a github-project profile with no [intake] type: {repo, start_label}; see product.pull_issues
    STATE_DIR = ATTN
    CONFIG_DIR = PROFILE_NAME = None
    TRACKER = {}
    INTAKE = {"columns": PRODUCT_INTAKE_COLUMNS, "skip_tags": sorted(PRODUCT_SKIP_TAGS)}
    ACCOUNTS = {}
    CODE_HOST = {"owner": None, "labels": {}}  # [code_host] owner, labels = {review, ready, merge_ready, rework, failed}
    GH_CONFIG_DIR = None                       # [code_host] gh_config_dir: this profile's own gh sign-in folder
    PR_LIMITS = {"pr_max_lines": 300, "pr_max_files": 10, "pr_auto_merge_lines": 150, "pr_auto_merge_files": 6}
    STAGES = {}
    # memory: agents start only while min_free_memory is free; a window past max_agent_processes/_memory is stopped
    MEMORY_DEFAULTS = {"min_free_memory": "15%", "max_agent_processes": 150, "max_agent_memory": "25%", "kill_runaway": True}
    # autostart: the console starts the dispatcher. account_checks: minutes between health checks of a parked account
    # (the last repeats; [] is off). hooks: false adds no Claude Code hooks to stage agents. release_waiting_after: minutes a run agent may wait (a GATE line or an idle
    # screen) before pl stops it and holds its card back (0 is off)
    DISPATCH = {"max_runs": 3, "max_prep": 2, "interval": 120, "autostart": True, "account_checks": [10, 30, 60],
                "release_waiting_after": 30, "rework_rounds": 3, "hooks": True, **MEMORY_DEFAULTS}
    GATES = {"spec": False}
    PIPELINE = None       # [pipeline]: columns, stages and gates; the engineering pipeline when absent
    ASSISTANT = {}        # [assistant] enabled, account, proactive: the Assistant tab (on unless enabled = false)
    PERMISSIONS = {}      # [permissions] unattended = false: pipeline agents and loops ask before each action
    UPDATES = {}          # [updates] check = false: no daily look at pl's release tags
    SKILLS = {}           # [skills] library: the shared skills folder (default ~/.local/share/pl/skills)
    LOCAL_MODEL = {}      # [local_model] enabled, url, model, timeout: an optional Ollama model on this machine (off)
    MODELS = {}           # [models.local] host, allow_lan, timeout, route/summarise/title/embed, [models.local.fallback]
    PODS = {}             # [pods.<name>] file, knowledge, skills, plugins, budgets
    POD = None            # [pipeline] pod: the pipeline's pod (default the profile name)
    SANDBOX = {}          # [sandbox] enabled, cpus, memory, disk: optional Multipass VM for stage agents
    CAPS = {}             # [caps.<stage>] minutes, tokens ("2M", "500k" or a number): a stage's agent is stopped at the cap
    SYNC = {}
    USAGE = {}            # [usage] prices = {model = dollars per million tokens}, windows = {model = context tokens}
    TRIGGERS = {}         # [triggers.<name>]
    CONNECTORS = {}       # [connectors.<name>]
    RETENTION = {"email_days": 90}  # [retention] email_days = 90
    WORKTREES = {"cleanup": True}
    out = {k: v for k, v in locals().items() if k.isupper()}
    _state_paths(out)
    return out


globals().update(_defaults())


START_LABEL = "pl:start"   # an open issue with this label joins the funnel (the default issue intake)


# pl's built-in stage skills (src/pl/skills_builtin): new profiles' stage prompts. pl setup --use-builtin-stages
# switches an existing profile to them.
BUILTIN_PROMPTS = {"spec": "/pl-spec {id}", "design": "/pl-design {id}", "plan": "/pl-plan {id}", "run": "/pl-run {id}"}


def _review_text(repo, lab, account):
    """The inline review prompt, used while the pl-review skill is not in the library yet."""
    rework, failed = lab.get("rework") or "pl:needs-rework", lab.get("failed") or "pl:review-failed"
    rv, ok = lab["review"], lab["ready"]
    text = (f"Review pull requests on {repo}. List them with: gh pr list --repo {repo} --state open --search "
            f"\"label:{rv}\". For each one: check out its branch in a separate git worktree, review the diff against "
            "the PR's base branch for bugs and security problems, and run the repo's tests. "
            f"Review passes: remove the label {rv} and add {ok}. "
            f"Review finds problems: post them as one PR comment in plain words, remove {rv}, and add both {ok} and {rework}. "
            f"Review cannot run: comment why, remove {rv} and add {failed}; a person decides, then adds {rv} back. "
            "Never merge, never push to the base branch, never create or edit labels.")
    return f"/loop 30m {text}" if (account.get("harness") or "claude") == "claude" else text


def _review_prompt(repo, lab, account):
    """The built-in auto-review loop's prompt for a github-project profile: the pl-review skill with the repo and the
    labels. Claude repeats it with /loop."""
    rework, failed = lab.get("rework") or "pl:needs-rework", lab.get("failed") or "pl:review-failed"
    q = shlex.quote   # a label with spaces stays one value
    text = f"/pl-review repo={q(repo)} review={q(lab['review'])} ready={q(lab['ready'])} rework={q(rework)} failed={q(failed)}"
    return f"/loop 30m {text}" if (account.get("harness") or "claude") == "claude" else text


TOKEN_UNITS = {"": 1, "k": 10**3, "m": 10**6, "b": 10**9}


def parse_tokens(v):
    """A token count from 2_000_000, "2M", "500k" or "1.5b"; None when it is none of those."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmb]?)", str(v).strip().lower())
    return int(float(m[1]) * TOKEN_UNITS[m[2]]) if m else None


def parse_caps(raw):
    """{stage: {"minutes": n, "tokens": n}} from [caps.<stage>]; a value that is not a number is left out."""
    out = {}
    for stage, v in (raw or {}).items():
        minutes = v.get("minutes") if isinstance(v, dict) else None
        caps = {"minutes": minutes} if isinstance(minutes, int) and not isinstance(minutes, bool) else {}
        if isinstance(v, dict) and (tokens := parse_tokens(v["tokens"]) if "tokens" in v else None) is not None:
            caps["tokens"] = tokens
        if caps:
            out[stage] = caps
    return out


def without_repo_caps(data, source):
    """A repo file's settings without `caps`: caps come only from the profile or the owner, never from a repo."""
    if "caps" in data:
        print(f"pl: {source} sets caps; ignored (caps come only from the profile)", file=sys.stderr)
    return {k: v for k, v in data.items() if k != "caps"}


def _apply(g, t):
    """Lay config.toml's tables over the defaults. Missing keys keep the default; unknown keys are ignored."""
    x = lambda v: Path(v).expanduser()  # noqa: E731
    if "tmux_session" in t:
        g["TMUX_SESSION"] = t["tmux_session"]
    u = t.get("user", {})
    for key, name in (("name", "USER_NAME"), ("email", "USER_EMAIL"), ("uuid", "USER_UUID"), ("login", "USER_LOGIN")):
        if key in u:
            g[name] = u[key]
    pa = t.get("paths", {})
    for key, name in (("work_dir", "WORK_DIR"), ("plans_dir", "PLANS"), ("attention_cmd", "ATTENTION")):
        if key in pa:
            g[name] = x(pa[key]) if pa[key] else None
    g["WORKTREES_DIR"] = x(pa["worktrees_dir"]) if isinstance(pa.get("worktrees_dir"), str) and pa["worktrees_dir"] \
        else default_worktrees(g["WORK_DIR"]) if g["WORK_DIR"] else None
    if "accounts" in t:
        g["ACCOUNTS"] = dict(t["accounts"])
        g["PROFILES"] = {n: x(a.get("config_dir", f"~/.claude-{n}")) for n, a in t["accounts"].items()}
    tr = {**g["TRACKER"], **t.get("tracker", {})}
    g["TRACKER"], g["BOARD"] = tr, tr.get("board_id")
    it = {**g["INTAKE"], **t.get("intake", {})}
    g["INTAKE"], g["PRODUCT_BOARD"] = it, it.get("board_id")
    g["PRODUCT_INTAKE_COLUMNS"], g["PRODUCT_SKIP_TAGS"] = [it["columns"]] if isinstance(it["columns"], str) else list(it["columns"]), set(it["skip_tags"])
    g["STAGES"] = dict(t.get("stages", {}))
    g["PROMPTS"] = {**g["PROMPTS"], **{s: v["prompt"] for s, v in g["STAGES"].items() if "prompt" in v}}
    if "loops" in t:
        g["SERVICES"] = {n: {"prompt": v.get("prompt") or (f"/{v['skill']}" if v.get("skill") else ""), "profile": v.get("account"),
                             **({"max_context": v["max_context"]} if "max_context" in v else {}),
                             **({"pinned_only": True} if v.get("fallback") is False else {}),
                             **{k: v[k] for k in ("every", "hours", "skill", "team") if k in v},
                             **({"writes": v.get("writes", False)} if "every" in v or "writes" in v else {})}
                         for n, v in t["loops"].items() if v.get("enabled", True) is not False}
        g["LOOPS_OFF"] = {n: dict(v) for n, v in t["loops"].items() if v.get("enabled", True) is False}
    ch = t.get("code_host", {})
    repos = [r for r in (ch.get("repos") if isinstance(ch.get("repos"), list) else []) if isinstance(r, str) and r]
    g["CODE_HOST"] = {"owner": ch.get("owner") or None, "labels": dict(ch.get("labels") or {}), **({"repos": repos} if repos else {})}
    g["PR_LIMITS"] = {k: ch.get(k, v) for k, v in g["PR_LIMITS"].items()}
    if tr.get("type") == "github-project" and tr.get("repo"):   # both defaults; [intake] enabled / [loops.auto-review] enabled = false turn one off
        if not it.get("type") and it.get("enabled", True) is not False:
            g["ISSUE_INTAKE"] = {"repo": tr["repo"], "start_label": START_LABEL}
        lab = g["CODE_HOST"]["labels"]
        if "auto-review" not in t.get("loops", {}) and g["ACCOUNTS"] and lab.get("review") and lab.get("ready"):
            first, acct = next(iter(g["ACCOUNTS"].items()))
            g["SERVICES"] = {**g["SERVICES"], "auto-review": {"prompt": _review_prompt(tr["repo"], lab, acct), "profile": first,
                                                             "fallback": _review_text(tr["repo"], lab, acct),
                                                             "builtin": True}}
    if isinstance(ch.get("gh_config_dir"), str) and ch["gh_config_dir"]:
        g["GH_CONFIG_DIR"] = (g["CONFIG_DIR"] or Path()) / x(ch["gh_config_dir"])
    g["DISPATCH"] = {**g["DISPATCH"], **t.get("dispatch", {})}
    g["GATES"] = {**g["GATES"], **t.get("gates", {})}
    from pl import pipeline
    g["PIPELINE"] = pipeline.from_toml(t.get("pipeline"))
    if t.get("pipeline"):
        g["COLUMNS"] = g["PIPELINE"]["columns"]
    g["USAGE"] = dict(t.get("usage", {}))
    g["CAPS"] = parse_caps(t.get("caps", {}))
    g["PERMISSIONS"] = dict(t.get("permissions", {}))
    g["ASSISTANT"] = dict(t.get("assistant", {}))
    g["SKILLS"] = dict(t.get("skills", {}))
    g["UPDATES"] = dict(t.get("updates", {}))
    g["LOCAL_MODEL"] = dict(t.get("local_model", {}))
    g["MODELS"] = dict(t.get("models", {}))
    g["PODS"] = {n: dict(v) for n, v in t.get("pods", {}).items()}
    g["POD"] = t.get("pipeline", {}).get("pod")
    g["TRIGGERS"] = dict(t.get("triggers", {}))
    g["CONNECTORS"] = dict(t.get("connectors", {}))
    g["RETENTION"] = {**g["RETENTION"], **t.get("retention", {})}
    g["SANDBOX"] = dict(t.get("sandbox", {}))
    g["WORKTREES"] = {"cleanup": True, **t.get("worktrees", {})}
    g["SYNC"] = dict(t.get("sync", {}))


def default_worktrees(work_dir) -> Path:
    """The agents' worktrees folder when [paths] worktrees_dir is unset: a sibling of the work folder."""
    work_dir = Path(work_dir)
    return work_dir.parent / f"{work_dir.name}-pl-worktrees"


def _worktrees_errors(paths) -> list[str]:
    """[paths] worktrees_dir: a folder of its own, outside the work folder and not above it (nor / or HOME)."""
    v = paths.get("worktrees_dir")
    if v is None or v == "":
        return []
    if not isinstance(v, str):
        return ["paths.worktrees_dir must be a folder path (text)"]
    wt = Path(v).expanduser().resolve()
    work = Path(str(paths.get("work_dir") or Path.home())).expanduser().resolve()
    if wt == Path(wt.anchor) or wt == Path.home().resolve() or wt == work or wt.is_relative_to(work) \
            or work.is_relative_to(wt):
        return [f"paths.worktrees_dir {v} must be a folder of its own outside the work folder {work}: not the work "
                "folder, a folder inside it or above it, / or your home folder"]
    return []


def load(profile: str | None = None, config_dir: str | None = None) -> None:
    """Pick the settings: config_dir, else ~/.pl-<profile>, else $PL_CONFIG_DIR. None of them leaves the neutral
    defaults with no profile loaded (CONFIG_DIR None); the CLI refuses to run most commands then."""
    if profile is not None and not NAME_RE.match(profile):
        raise SystemExit("pl: bad profile name")
    g = globals()
    g.update(_defaults())
    d = config_dir or (str(g["HOME"] / f".pl-{profile}") if profile else None) or os.environ.get("PL_CONFIG_DIR") or None
    if d is None:
        return
    d = Path(d).expanduser()
    name = d.name.removeprefix(".pl-")
    if not (d / "config.toml").is_file():
        raise SystemExit(f'pl: no profile "{name}" ({d}): create it with pl profiles new {name}')
    if not NAME_RE.match(name):
        raise SystemExit(f"pl: bad profile name {name!r} (folder {d})")
    g["CONFIG_DIR"], g["PROFILE_NAME"] = d, name
    g["TMUX_SESSION"] = f"pl-{name}"
    g["PLANS"] = d / "plans"
    toml = d / "config.toml"
    try:
        t = tomllib.loads(toml.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"pl: {toml}: {e}")
    _apply(g, t)
    if t.get("pipeline"):
        from pl import pipeline
        bad = pipeline.errors(g["PIPELINE"], t.get("connectors"))
        for e in bad:
            print(f"pl: {toml}: {e}", file=sys.stderr)
        if bad:
            raise SystemExit(2)
    if not (isinstance(g["TMUX_SESSION"], str) and NAME_RE.match(g["TMUX_SESSION"])):
        raise SystemExit(f"pl: bad tmux_session {g['TMUX_SESSION']!r} in {toml}")
    g["STATE_DIR"] = g["ATTN"] = d / "state"
    g["STATE_DIR"].mkdir(parents=True, exist_ok=True, mode=0o700)
    g["STATE_DIR"].chmod(0o700)
    _state_paths(g)
    from pl import connectors
    connectors.check()


def gh_env() -> dict | None:
    """The env for a gh subprocess: a copy of os.environ plus GH_CONFIG_DIR when the profile sets one, else None
    (inherit). Only the folder path is passed; pl never reads what is inside it."""
    d = globals()["GH_CONFIG_DIR"]
    return {**os.environ, "GH_CONFIG_DIR": str(d)} if d else None


def path() -> Path | None:
    """The profile's config.toml, or None when no profile is loaded."""
    d = globals()["CONFIG_DIR"]
    return d / "config.toml" if d else None


READ_ONLY = "pl: no profile loaded, so settings are read-only: create one with pl profiles new <name>"


def _pr_limit_errors(ch) -> list[str]:
    def whole(v):
        return isinstance(v, int) and not isinstance(v, bool) and v >= 1
    errs = [f"code_host.{k} must be a whole number of at least 1" for k in _defaults()["PR_LIMITS"] if not whole(ch[k])]
    for kind in ("lines", "files"):
        top, auto = ch[f"pr_max_{kind}"], ch[f"pr_auto_merge_{kind}"]
        if whole(top) and whole(auto) and auto > top:
            errs.append(f"code_host.pr_auto_merge_{kind} must not be above code_host.pr_max_{kind}")
    return errs


def validate(doc) -> list[str]:
    """Problems that stop a save (empty = fine). Values are checked as written; ${VAR} references are never expanded."""
    from pl import harnesses
    errs = []
    x = lambda v: Path(str(v)).expanduser()  # noqa: E731
    import tomllib
    import tomlkit
    try:
        _apply(_defaults(), tomllib.loads(tomlkit.dumps(doc)))
    except Exception as e:  # noqa: BLE001 - whatever load would choke on
        return [f"the settings would not load ({type(e).__name__}: {e})"]
    gd = doc.get("code_host", {}).get("gh_config_dir")
    if gd is not None and not isinstance(gd, str):
        errs.append("code_host.gh_config_dir must be a folder path (text)")
    errs += _pr_limit_errors({**_defaults()["PR_LIMITS"], **doc.get("code_host", {})})
    for key, low in (("max_runs", 0), ("max_prep", 0), ("interval", 1), ("max_agent_processes", 1),
                     ("release_waiting_after", 0)):
        v = doc.get("dispatch", {}).get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            errs.append(f"dispatch.{key} must be a whole number of at least {low}")
    if not isinstance(doc.get("permissions", {}).get("unattended", True), bool):
        errs.append("permissions.unattended must be true or false")
    if "cleanup" in doc.get("worktrees", {}) and not isinstance(doc.get("worktrees", {}).get("cleanup"), bool):
        errs.append("worktrees.cleanup must be true or false")
    for key in ("autostart", "kill_runaway"):
        if not isinstance(doc.get("dispatch", {}).get(key, True), bool):
            errs.append(f"dispatch.{key} must be true or false")
    ac = doc.get("dispatch", {}).get("account_checks")
    if ac is not None and not (isinstance(ac, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                            and x > 0 for x in ac)):
        errs.append("dispatch.account_checks must be a list of minutes above 0, like [10, 30, 60] ([] turns checks off)")
    from pl.memory import parse_size
    for key in ("min_free_memory", "max_agent_memory"):
        v = doc.get("dispatch", {}).get(key)
        try:
            v is None or parse_size(v, 1)
        except ValueError:
            errs.append(f"dispatch.{key} must be a size like \"4GB\" or a share of memory like \"15%\"")
    ts = doc.get("tmux_session")
    if ts is not None and not (isinstance(ts, str) and NAME_RE.match(ts)):
        errs.append(f"tmux_session {ts!r} must match {NAME_RE.pattern}")
    for key in ("work_dir", "plans_dir"):
        v = doc.get("paths", {}).get(key)
        if v is not None and not x(v).is_dir():
            errs.append(f"paths.{key} {v} does not exist")
    errs += _worktrees_errors(doc.get("paths", {}))
    ac = doc.get("paths", {}).get("attention_cmd")
    if ac and not (x(ac).is_file() and os.access(x(ac), os.X_OK)):
        errs.append(f"paths.attention_cmd {ac} is not an executable file")
    hs = doc.get("harnesses", {})
    for name, h in hs.items():
        ev = h.get("env_var")
        if ev is not None and not (isinstance(ev, str) and harnesses.ENV_RE.fullmatch(ev)):
            errs.append(f"harnesses.{name} env_var {ev!r} is not a variable name")
    known = [*harnesses.BUILTINS, *(n for n, h in hs.items() if {"bin", "interactive", "headless"} <= h.keys())]
    accounts = doc.get("accounts", {})
    for name, a in accounts.items():
        if not NAME_RE.match(name):
            errs.append(f"account name {name!r} must match {NAME_RE.pattern}")
        if not x(a.get("config_dir", f"~/.claude-{name}")).is_dir():
            errs.append(f"account {name}: config_dir {a.get('config_dir', f'~/.claude-{name}')} does not exist")
        if (a.get("harness") or "claude") not in known:
            errs.append(f"account {name}: unknown harness {a.get('harness')!r}; known: {', '.join(known)}")
        errs += _model_errors(name, a)
    for key in ("prices", "windows"):
        for model, n in (doc.get("usage", {}).get(key) or {}).items():
            if isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0:
                errs.append(f"usage.{key}.{model} must be a number above 0")
    errs += _local_model_errors(doc.get("local_model", {}))
    errs += _models_local_errors(doc.get("models", {}).get("local", {}), doc.get("accounts", {}))
    if doc.get("pipeline"):
        from pl import pipeline
        errs += pipeline.errors(pipeline.from_toml(doc["pipeline"]), doc.get("connectors"))
    ed = doc.get("retention", {}).get("email_days")
    if ed is not None and (isinstance(ed, bool) or not isinstance(ed, int) or ed < 0):
        errs.append("retention.email_days must be a whole number of at least 0")
    errs += _sandbox_errors(doc.get("sandbox", {}))
    sync = doc.get("sync") if isinstance(doc.get("sync"), dict) else {}
    if "github" in sync:
        gh = sync["github"]
        if not (isinstance(gh, str) and re.match(r"^[^/\s]+/projects/\d+$", gh)):
            errs.append("sync.github must look like owner/projects/N")
        if doc.get("tracker", {}).get("type") != "local":
            errs.append('sync.github needs [tracker] type = "local"')
    if "git" in sync:
        git = sync["git"]
        if not (isinstance(git, str) and git.strip()):
            errs.append("sync.git must be a folder path")
    mc = doc.get("tracker", {}).get("max_card_chars")
    if mc is not None and (isinstance(mc, bool) or not isinstance(mc, int) or mc < 1000):
        errs.append("tracker.max_card_chars must be a whole number of at least 1000")
    for kind in ("tracker", "intake"):
        t = doc.get(kind, {})
        if "board_id" in t and not str(t["board_id"]).strip():
            errs.append(f"{kind}.board_id must not be empty")
        if t.get("type") == "local":
            p = t.get("path")
            if p is not None and (not isinstance(p, str) or Path(p).is_absolute()):
                errs.append(f"{kind}.path must be a relative path")
        if t.get("type") == "github-project":
            if not str(t.get("owner") or "").strip():
                errs.append(f"{kind}.owner must not be empty")
            if "number" in t and not str(t["number"]).strip():
                errs.append(f"{kind}.number must not be empty")
    for table in ("loops", "stages"):
        for name, v in doc.get(table, {}).items():
            if table == "loops" and not NAME_RE.match(name):
                errs.append(f"loop name {name!r} must match {NAME_RE.pattern}")
            if table == "loops" and name == "assistant":
                errs.append("loop name 'assistant' is taken: it is the Assistant tab's tmux window")
            mc = v.get("max_context") if table == "loops" else None
            if mc is not None and (isinstance(mc, bool) or not isinstance(mc, int) or not 0 <= mc <= 100):
                errs.append(f"loops.{name}: max_context must be a whole percent from 0 (off) to 100")
            if table == "loops" and not isinstance(v.get("fallback", True), bool):
                errs.append(f"loops.{name}: fallback must be true or false")
            if table == "loops" and v.get("enabled", True) is not False and not str(v.get("prompt") or v.get("skill") or "").strip():
                errs.append(f"loops.{name}: prompt or skill is required")
            if table == "loops":
                errs += _loop_schedule_errors(name, v)
            if table == "stages" and is_local_account(v.get("account")):
                if why := local_stage_problem(name, v):
                    errs.append(why)
                continue
            if accounts and v.get("account") and v["account"] not in accounts:
                errs.append(f"{table}.{name}: account {v['account']!r} is not one of {', '.join(accounts)}")
            if v.get("harness") and v["harness"] not in known:
                errs.append(f"{table}.{name}: unknown harness {v['harness']!r}; known: {', '.join(known)}")
            if table == "stages":
                errs += _stage_effort_errors(name, v, accounts)
            if table == "stages" and "accounts" in v:
                errs += _pool_errors(name, v, accounts)
            elif table == "stages" and accounts and v.get("harness") in known:
                errs += _stage_harness_errors(name, v, accounts)
    errs += _pod_errors(doc.get("pods", {}), globals()["CONFIG_DIR"])
    from pl import triggers
    errs += triggers.validate(doc)
    return errs


LOCAL_PREFIX = "local:"


def is_local_account(account) -> bool:
    return isinstance(account, str) and account.startswith(LOCAL_PREFIX)


def local_stage_problem(name, v) -> str | None:
    """Why [stages.<name>] cannot run on its local:<model> account, or None. pl never guesses which stages need tools:
    the stage says so with tools = false, and the built-in stages always need them."""
    from pl import harnesses
    model = v["account"][len(LOCAL_PREFIX):]
    if name in ("spec", "design", "plan", "run") or v.get("tools") is not False:
        return (f"stages.{name}: a local model cannot use tools; set tools = false only for a stage that needs none")
    if not harnesses.MODEL_RE.fullmatch(model):
        return f"stages.{name}: account must be local:<model>, a model name like \"local:gemma4:e4b\""
    return None


def _pod_path_error(label, v, root):
    """A pod file or knowledge path: absolute = an existing file; relative = a path that stays inside the profile folder."""
    if not isinstance(v, str) or not v.strip():
        return f"{label} must be a file path (text)"
    p = Path(v).expanduser()
    if p.is_absolute():
        return None if p.is_file() else f"{label} {v} is not an existing file"
    if root is not None and not (root / p).resolve().is_relative_to(root.resolve()):
        return f"{label} {v} is outside the profile folder"
    return None


def _pod_errors(pods, root) -> list[str]:
    from pl.skills import NAME_RE as SKILL_NAME
    whole = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0  # noqa: E731
    errs = []
    for name, v in pods.items():
        if not NAME_RE.match(name):
            errs.append(f"pod name {name!r} must match {NAME_RE.pattern}")
        if "file" in v and (e := _pod_path_error(f"pods.{name}.file (the pod.md)", v["file"], root)):
            errs.append(e)
        know = v.get("knowledge", [])
        if not isinstance(know, list):
            errs.append(f"pods.{name}.knowledge must be a list of file paths")
        else:
            errs += [e for k in know if (e := _pod_path_error(f"pods.{name}.knowledge", k, root))]
        sk = v.get("skills", [])
        if not (isinstance(sk, list) and all(isinstance(x, str) and SKILL_NAME.fullmatch(x) for x in sk)):
            errs.append(f"pods.{name}.skills must be a list of skill names")
        pl = v.get("plugins", [])
        if not (isinstance(pl, list) and all(isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"]
                                             and isinstance(x.get("hooks", True), bool) for x in pl)):
            errs.append(f"pods.{name}.plugins must be a list of {{name, hooks = true|false}}")
        b = v.get("budgets", {})
        if not (isinstance(b, dict) and all(whole(x) for x in b.values())):
            errs.append(f"pods.{name}.budgets must hold whole numbers")
    return errs


def _loop_schedule_errors(name, v) -> list[str]:
    errs = []
    for key, parse in (("every", schedule.parse_every), ("hours", schedule.parse_hours)):
        if key in v:
            try:
                parse(v[key])
            except ValueError as e:
                errs.append(f"loops.{name}: {e}")
    if not isinstance(v.get("writes", False), bool):
        errs.append(f"loops.{name}: writes must be true or false")
    return errs


def _model_errors(name, a) -> list[str]:
    """[accounts.<name>] model and effort: a model id and a level the account's harness takes. Offline: agy's and
    codex's model lists are never read here, so any id passes for them (new models appear)."""
    from pl import harnesses
    h, errs = a.get("harness") or "claude", []
    if "model" in a:
        m = a["model"]
        if h not in harnesses.MODEL_FLAGS:
            errs.append(f"accounts.{name}: harness {h} takes no model; remove model")
        elif not (isinstance(m, str) and harnesses.MODEL_RE.fullmatch(m)):
            errs.append(f"accounts.{name}: model must be a model id like \"opus\" (no spaces or quotes, not starting "
                        "with -), or remove it to use the harness's default")
        elif h != "claude" and m in harnesses.CLAUDE_MODELS:
            errs.append(f"accounts.{name}: model {m!r} is a claude model but the account uses harness {h}; a model "
                        "belongs to its account's harness, so change or remove it when the harness changes")
    if "effort" in a:
        levels = harnesses.EFFORT_LEVELS.get(h)
        if not levels:
            errs.append(f"accounts.{name}: harness {h} takes no effort; remove effort")
        elif a["effort"] not in levels:
            errs.append(f"accounts.{name}: effort {a['effort']!r} is not one of {', '.join(levels)}")
    return errs


def stage_harnesses(v, accounts) -> set[str]:
    """The harness names a [stages.<name>] table can run on: its harness, else its pool's or its account's, else
    every account's (an unpinned stage runs on the card's account)."""
    if v.get("harness"):
        return {v["harness"]}
    pool = v.get("accounts") if isinstance(v.get("accounts"), list) else [v["account"]] if v.get("account") else accounts
    return {(accounts.get(a) or {}).get("harness") or "claude" for a in pool if a in accounts}


def _stage_effort_errors(name, v, accounts) -> list[str]:
    """[stages.<name>] effort: a level every harness the stage can run on takes. models: a model per harness."""
    from pl import harnesses
    errs = []
    if "model" in v:
        errs.append(f"stages.{name}: model is not a stage setting; set one per harness in [stages.{name}.models], "
                    "like claude = \"opus\" (a stage may pool accounts of different harnesses)")
    if "models" in v:
        models = v["models"]
        if not isinstance(models, dict):
            errs.append(f"stages.{name}.models must be a table of harness = \"model\"")
        else:
            for h, m in models.items():
                if h not in harnesses.MODEL_FLAGS:
                    errs.append(f"stages.{name}.models.{h}: harness {h} takes no model; "
                                f"models take {', '.join(harnesses.MODEL_FLAGS)}")
                elif not (isinstance(m, str) and harnesses.MODEL_RE.fullmatch(m)):
                    errs.append(f"stages.{name}.models.{h}: must be a model id like \"opus\" "
                                "(no spaces or quotes, not starting with -)")
                elif h != "claude" and m in harnesses.CLAUDE_MODELS:
                    errs.append(f"stages.{name}.models.{h}: {m!r} is a claude model name; use a model {h} offers")
    if "effort" not in v:
        return errs
    hs = stage_harnesses(v, accounts)
    levels = harnesses.shared_levels(hs)
    if v["effort"] not in levels:
        errs.append(f"stages.{name}: effort {v['effort']!r} is not a level its harness{'es' if len(hs) > 1 else ''} "
                    f"({', '.join(sorted(hs)) or 'any'}) take{'' if len(hs) > 1 else 's'}: "
                    f"{', '.join(levels) or 'none'}")
    return errs


def _stage_harness_errors(name, v, accounts) -> list[str]:
    """[stages.<name>] harness without a pool: it must match its pinned account's harness, or, unpinned, some account's
    (else pl would launch that CLI under an account signed in to another one)."""
    h, a = v["harness"], v.get("account")
    if a and a in accounts:
        ah = accounts[a].get("harness") or "claude"
        return [] if ah == h else [f"stages.{name}: harness {h!r} differs from account {a}'s harness {ah!r}; "
                                   "remove the stage's harness line (the account sets it)"]
    if a or any((x.get("harness") or "claude") == h for x in accounts.values()):
        return []
    return [f"stages.{name}: no account uses harness {h!r} (accounts: {', '.join(accounts)}); "
            "remove the stage's harness line or add an account of that harness"]


def _pool_errors(name, v, accounts) -> list[str]:
    """[stages.<name>] accounts: a list of known account names, without account, and no harness that differs from theirs."""
    pool = v["accounts"]
    if not (isinstance(pool, list) and pool and all(isinstance(a, str) for a in pool)):
        return [f"stages.{name}: accounts must be a list of account names, like [\"main\", \"agy\"]"]
    errs = [f"stages.{name}: account {a!r} in accounts is not one of {', '.join(accounts) or 'no accounts'}"
            for a in pool if a not in accounts]
    if v.get("account"):
        errs.append(f"stages.{name}: set account or accounts, not both; remove account")
    off = [a for a in pool if a in accounts and v.get("harness") and (accounts[a].get("harness") or "claude") != v["harness"]]
    if off:
        errs.append(f"stages.{name}: harness {v['harness']!r} differs from the harness of {', '.join(off)}; "
                    "remove harness (each account uses its own)")
    return errs


def _local_model_errors(lm) -> list[str]:
    """[local_model]: the four known keys only (there is no API key), and a loopback url."""
    from pl import local_model
    errs = [f"local_model.{k} is not a setting; the keys are {', '.join(local_model.DEFAULTS)}"
            for k in lm if k not in local_model.DEFAULTS]
    if not isinstance(lm.get("enabled", False), bool):
        errs.append("local_model.enabled must be true or false")
    if "url" in lm and (why := local_model.url_problem(lm["url"])):
        errs.append(f"local_model.{why}")
    if "model" in lm and not (isinstance(lm["model"], str) and lm["model"].strip()):
        errs.append("local_model.model must be a model name like \"gemma4\"")
    t = lm.get("timeout")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0):
        errs.append("local_model.timeout must be a number of seconds above 0")
    return errs


def _models_local_errors(ml, accounts) -> list[str]:
    """[models.local]: known keys only, a loopback host (a private one with allow_lan = true), a model name per
    job, and fallbacks that are skip, person or an account."""
    from pl import local_model
    keys = ("host", "allow_lan", "timeout", "route_min_confidence", "fallback", *local_model.JOBS)
    errs = [f"models.local.{k} is not a setting; the keys are {', '.join(keys)}" for k in ml if k not in keys]
    if not isinstance(ml.get("allow_lan", False), bool):
        errs.append("models.local.allow_lan must be true or false")
    if "host" in ml and (why := local_model.url_problem(ml["host"], ml.get("allow_lan") is True)):
        errs.append(f"models.local.host {why}")
    for job in local_model.JOBS:
        if job in ml and not (isinstance(ml[job], str) and ml[job].strip()):
            errs.append(f"models.local.{job} must be a model name like \"gemma4:e4b\"")
    t = ml.get("timeout")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0):
        errs.append("models.local.timeout must be a number of seconds above 0")
    c = ml.get("route_min_confidence")
    if c is not None and (isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1):
        errs.append("models.local.route_min_confidence must be a number from 0 to 1")
    for job, v in (ml.get("fallback") or {}).items():
        if job not in local_model.JOBS:
            errs.append(f"models.local.fallback.{job} is not a job; the jobs are {', '.join(local_model.JOBS)}")
        elif v not in ("skip", "person") and v not in accounts:
            errs.append(f"models.local.fallback.{job} must be skip, person or an account name, not {v!r}")
    return errs


def _sandbox_errors(sb) -> list[str]:
    """[sandbox]: enabled, cpus, memory, disk, allow."""
    from pl import sandbox
    errs = [f"sandbox.{k} is not a setting; the keys are {', '.join(sandbox.DEFAULTS)}"
            for k in sb if k not in sandbox.DEFAULTS]
    if "enabled" in sb and not isinstance(sb["enabled"], bool):
        errs.append("sandbox.enabled must be true or false")
    if "cpus" in sb:
        c = sb["cpus"]
        if isinstance(c, bool) or not isinstance(c, int) or c < 1:
            errs.append("sandbox.cpus must be a whole number of at least 1")
    for key in ("memory", "disk"):
        if key in sb:
            v = sb[key]
            if not (isinstance(v, str) and sandbox.SIZE_RE.fullmatch(v)):
                errs.append(f'sandbox.{key} must be a size like "4G" or "512M"')
    if "allow" in sb:
        al = sb["allow"]
        if not isinstance(al, list):
            errs.append("sandbox.allow must be a list of host names or CIDRs")
        else:
            for item in al:
                if not sandbox.is_host_or_cidr(item):
                    errs.append(f"sandbox.allow entry {item!r} must be a host name or CIDR")
    return errs


def save(doc, target=None) -> None:
    """Write a tomlkit document over the profile's config.toml: validated, temp file 0600 in the same folder,
    fsync, os.replace. A refused or failed save leaves the old file untouched."""
    import tomlkit
    from pl import harnesses
    target = Path(target) if target else path()
    if target is None:
        raise SystemExit(READ_ONLY)
    errs = validate(doc)
    if errs:
        raise SystemExit("pl: not saved: " + "; ".join(errs))
    text = tomlkit.dumps(doc)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    harnesses._CACHE.clear()


# pl manager's machine.toml [limits]: caps across every profile on the machine
MACHINE_DEFAULTS = {"max_live_agents": 8, "max_agents_memory": "60%", "min_free_memory": "15%", "kill_runaway": True}


def validate_machine(t) -> list[str]:
    """Problems in machine.toml's [manager] and [limits] (empty = fine), checked with the [dispatch] size rules."""
    from pl.memory import parse_size
    errs = [f"{k} must be a table ([{k}])" for k in ("manager", "limits") if not isinstance(t.get(k, {}), dict)]
    lim = t.get("limits") if isinstance(t.get("limits"), dict) else {}
    man = t.get("manager") if isinstance(t.get("manager"), dict) else {}
    v = lim.get("max_live_agents")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        errs.append("limits.max_live_agents must be a whole number of at least 1")
    if not isinstance(man.get("enabled", True), bool):
        errs.append("manager.enabled must be true or false")
    if not isinstance(lim.get("kill_runaway", True), bool):
        errs.append("limits.kill_runaway must be true or false")
    for key in ("max_agents_memory", "min_free_memory"):
        try:
            lim.get(key) is None or parse_size(lim[key], 1)
        except ValueError:
            errs.append(f"limits.{key} must be a size like \"4GB\" or a share of memory like \"60%\"")
    return errs
