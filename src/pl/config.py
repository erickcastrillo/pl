"""Settings. Other modules read them through the config module alias at call time, never by copying names."""
import os
import re
import socket
from pathlib import Path
import tomllib

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
    STAGES = {}
    # memory: agents start only while min_free_memory is free; a window past max_agent_processes/_memory is stopped
    MEMORY_DEFAULTS = {"min_free_memory": "15%", "max_agent_processes": 150, "max_agent_memory": "25%", "kill_runaway": True}
    DISPATCH = {"max_runs": 3, "max_prep": 2, "interval": 120, "autostart": True, **MEMORY_DEFAULTS}  # autostart: the console starts the dispatcher
    GATES = {"spec": False}
    ASSISTANT = {}        # [assistant] enabled, account, proactive: the Assistant tab (on unless enabled = false)
    USAGE = {}            # [usage] prices = {model = dollars per million tokens}, windows = {model = context tokens}
    out = {k: v for k, v in locals().items() if k.isupper()}
    _state_paths(out)
    return out


globals().update(_defaults())


START_LABEL = "pl:start"   # an open issue with this label joins the funnel (the default issue intake)


def _review_prompt(repo, lab, account):
    """The built-in auto-review loop's prompt for a github-project profile. Claude repeats it with /loop."""
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
        g["SERVICES"] = {n: {"prompt": v["prompt"], "profile": v.get("account"),
                             **({"max_context": v["max_context"]} if "max_context" in v else {})} for n, v in t["loops"].items()
                         if v.get("enabled", True) is not False}
        g["LOOPS_OFF"] = {n: dict(v) for n, v in t["loops"].items() if v.get("enabled", True) is False}
    ch = t.get("code_host", {})
    repos = [r for r in (ch.get("repos") if isinstance(ch.get("repos"), list) else []) if isinstance(r, str) and r]
    g["CODE_HOST"] = {"owner": ch.get("owner") or None, "labels": dict(ch.get("labels") or {}), **({"repos": repos} if repos else {})}
    if tr.get("type") == "github-project" and tr.get("repo"):   # both defaults; [intake] enabled / [loops.auto-review] enabled = false turn one off
        if not it.get("type") and it.get("enabled", True) is not False:
            g["ISSUE_INTAKE"] = {"repo": tr["repo"], "start_label": START_LABEL}
        lab = g["CODE_HOST"]["labels"]
        if "auto-review" not in t.get("loops", {}) and g["ACCOUNTS"] and lab.get("review") and lab.get("ready"):
            first, acct = next(iter(g["ACCOUNTS"].items()))
            g["SERVICES"] = {**g["SERVICES"], "auto-review": {"prompt": _review_prompt(tr["repo"], lab, acct), "profile": first,
                                                             "builtin": True}}
    if isinstance(ch.get("gh_config_dir"), str) and ch["gh_config_dir"]:
        g["GH_CONFIG_DIR"] = (g["CONFIG_DIR"] or Path()) / x(ch["gh_config_dir"])
    g["DISPATCH"] = {**g["DISPATCH"], **t.get("dispatch", {})}
    g["GATES"] = {**g["GATES"], **t.get("gates", {})}
    g["USAGE"] = dict(t.get("usage", {}))
    g["ASSISTANT"] = dict(t.get("assistant", {}))


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
    if not (isinstance(g["TMUX_SESSION"], str) and NAME_RE.match(g["TMUX_SESSION"])):
        raise SystemExit(f"pl: bad tmux_session {g['TMUX_SESSION']!r} in {toml}")
    g["STATE_DIR"] = g["ATTN"] = d / "state"
    g["STATE_DIR"].mkdir(parents=True, exist_ok=True, mode=0o700)
    g["STATE_DIR"].chmod(0o700)
    _state_paths(g)


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
    for key, low in (("max_runs", 0), ("max_prep", 0), ("interval", 1), ("max_agent_processes", 1)):
        v = doc.get("dispatch", {}).get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            errs.append(f"dispatch.{key} must be a whole number of at least {low}")
    for key in ("autostart", "kill_runaway"):
        if not isinstance(doc.get("dispatch", {}).get(key, True), bool):
            errs.append(f"dispatch.{key} must be true or false")
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
    for key in ("prices", "windows"):
        for model, n in (doc.get("usage", {}).get(key) or {}).items():
            if isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0:
                errs.append(f"usage.{key}.{model} must be a number above 0")
    mc = doc.get("tracker", {}).get("max_card_chars")
    if mc is not None and (isinstance(mc, bool) or not isinstance(mc, int) or mc < 1000):
        errs.append("tracker.max_card_chars must be a whole number of at least 1000")
    for kind in ("tracker", "intake"):
        t = doc.get(kind, {})
        if "board_id" in t and not str(t["board_id"]).strip():
            errs.append(f"{kind}.board_id must not be empty")
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
            if table == "loops" and v.get("enabled", True) is not False and not str(v.get("prompt") or "").strip():
                errs.append(f"loops.{name}: prompt is required")
            if accounts and v.get("account") and v["account"] not in accounts:
                errs.append(f"{table}.{name}: account {v['account']!r} is not one of {', '.join(accounts)}")
            if v.get("harness") and v["harness"] not in known:
                errs.append(f"{table}.{name}: unknown harness {v['harness']!r}; known: {', '.join(known)}")
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
