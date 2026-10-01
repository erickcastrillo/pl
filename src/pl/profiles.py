"""pl profiles: list them, warn about shared resources, create one."""
import argparse
import ast
import fcntl
import json
import os
import re
import tomllib
from pathlib import Path

import tomlkit

from pl import config as C

def _tilde(p):
    s = str(p)
    h = str(Path.home())
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def _is_locked(lock: Path) -> bool:
    """True when another process holds the dispatch lock. Never creates the file."""
    try:
        f = open(lock)
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        f.close()
    return False


def _row(name, d, lock, session, tracker, accounts, check_lock=True):
    running = check_lock and _is_locked(lock)
    try:
        holder = lock.read_text().strip() if running else ""
    except OSError:
        holder = ""
    return {"name": name, "dir": str(d), "running": running, "holder": holder, "tmux_session": session,
            "tracker": f"{tracker.get('type') or '?'} {tracker.get('board_id') or tracker.get('owner') or '?'}", "board_id": tracker.get("board_id"),
            "accounts": {n: str(Path(a.get("config_dir", f"~/.claude-{n}")).expanduser()) for n, a in accounts.items()}}


def list_profiles(check_lock=True) -> list[dict]:
    """Every ~/.pl-* folder with a config.toml. check_lock=False: never touch the dispatch locks (running is False)."""
    home = Path.home()
    base = C._defaults()
    rows = []
    for d in sorted(home.glob(".pl-*")):
        name = d.name.removeprefix(".pl-")
        if not (d / "config.toml").is_file() or not C.NAME_RE.match(name):
            continue
        try:
            t = tomllib.loads((d / "config.toml").read_text())
        except (OSError, tomllib.TOMLDecodeError):
            t = {}
        session = t.get("tmux_session") if isinstance(t.get("tmux_session"), str) else f"pl-{name}"
        rows.append(_row(name, d, d / "state" / "pl-dispatch.lock", session, {**base["TRACKER"], **t.get("tracker", {})},
                         t.get("accounts", base["ACCOUNTS"]), check_lock))
    return rows


def current_row() -> dict:
    """The profile this process is running as, shaped like a list_profiles row and counted as running."""
    return {"name": C.PROFILE_NAME or "-", "dir": str(C.CONFIG_DIR or C.ATTN), "running": True, "holder": "",
            "tmux_session": C.TMUX_SESSION, "tracker": f"{C.TRACKER.get('type') or '?'} {C.BOARD or C.TRACKER.get('owner') or '?'}", "board_id": C.BOARD,
            "accounts": {n: str(d) for n, d in C.PROFILES.items()}}


def _join(names):
    return f"{names[0]} and {names[1]} both" if len(names) == 2 else f"{', '.join(names[:-1])} and {names[-1]} all"


def shared_warnings(rows: list[dict], current: dict | None = None) -> list[str]:
    """One line per resource shared by running profiles (the current one counts as running). Warnings only."""
    def ident(r):
        try:
            return str(Path(r["dir"]).expanduser().resolve())
        except OSError:
            return str(r["dir"])

    live = [r for r in rows if r["running"] and not (current and ident(r) == ident(current))]
    if current:
        live.append(current)
    names = [r["name"] for r in live]
    label = {ident(r): (_tilde(r["dir"]) if names.count(r["name"]) > 1 else r["name"]) for r in live}
    out = []

    def group(key_of, text):
        seen = {}
        for r in live:
            for k in key_of(r):
                seen.setdefault(k, [])
                if label[ident(r)] not in seen[k]:
                    seen[k].append(label[ident(r)])
        out.extend(text(k, ns) for k, ns in seen.items() if len(ns) > 1)

    group(lambda r: set(r["accounts"].values()),
          lambda k, ns: f"{_join(ns)} use {_tilde(k)}: they share one subscription's usage limit")
    group(lambda r: {r["board_id"]} if r["board_id"] else set(),
          lambda k, ns: f"{_join(ns)} use tracker board {k}: they will both pick up its cards")
    group(lambda r: {r["tmux_session"]},
          lambda k, ns: f"{_join(ns)} use tmux session {k}: their agent windows will mix")
    return out


def _values(from_current: bool) -> dict:
    return {k: v for k, v in (vars(C) if from_current else C._defaults()).items() if k.isupper()}


def _clean(v):
    """tomlkit cannot write None: drop None values (recursively)."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x) for x in v]
    return v


def _own_plans(plans):
    """plans_dir only when it is a folder of its own: the neutral default or a profile's <dir>/plans stays unset,
    so each profile gets its own <profile dir>/plans."""
    shared = {C._defaults()["PLANS"], *((C.CONFIG_DIR / "plans",) if C.CONFIG_DIR else ())}
    return None if Path(plans) in shared else _tilde(plans)


def _doc(v, tracker, intake):
    stages = {s: {"prompt": p} for s, p in v["PROMPTS"].items() if p}
    return _clean({
        "user": {"name": v["USER_NAME"], "email": v["USER_EMAIL"], "uuid": v["USER_UUID"]},
        "paths": {"work_dir": _tilde(v["WORK_DIR"]), "plans_dir": _own_plans(v["PLANS"]),
                  "attention_cmd": _tilde(v["ATTENTION"]) if v.get("ATTENTION") else None},
        "tracker": tracker,
        "intake": intake,
        "code_host": {"owner": (v.get("CODE_HOST") or {}).get("owner"), "labels": dict((v.get("CODE_HOST") or {}).get("labels") or {})},
        "dispatch": dict(v["DISPATCH"]),
        "gates": {**v["GATES"], "spec": True},
        "accounts": {n: dict(a) for n, a in v["ACCOUNTS"].items()},
        "stages": {**{s: dict(x) for s, x in (v.get("STAGES") or {}).items()},
                   **{s: {**(v.get("STAGES") or {}).get(s, {}), **x} for s, x in stages.items()}},
        "loops": {n: {"prompt": s["prompt"], **({"account": s["profile"]} if s.get("profile") else {}),
                      **({"max_context": s["max_context"]} if "max_context" in s else {})} for n, s in v["SERVICES"].items()
                  if not s.get("builtin")} | {n: dict(x) for n, x in (v.get("LOOPS_OFF") or {}).items()},
        "usage": dict(v.get("USAGE") or {}) or None,
    })


def _write_config(d, doc):
    d.mkdir(mode=0o700)
    fd = os.open(d / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tomlkit.dumps(doc))


def _target(name):
    if not C.NAME_RE.match(name):
        raise SystemExit(f"pl: bad profile name {name!r}")
    d = Path.home() / f".pl-{name}"
    if d.exists():
        raise SystemExit(f"pl: profile {name!r} exists ({d}); pick another name or edit its config.toml")
    return d


def new_profile(name: str, from_current: bool = False, github_owner: str | None = None, github_title: str | None = None,
                github_repo: str | None = None):
    """Write ~/.pl-<name>/config.toml with the effective (or neutral default) values and [gates] spec = true.
    With github_owner, first create a GitHub Project with a "pl stage" field and make it the tracker."""
    d = _target(name)
    v = _values(from_current)
    if not from_current:
        v["PROMPTS"] = dict(C.BUILTIN_PROMPTS)   # a new profile runs pl's built-in stage skills
    tracker = dict(v["TRACKER"])
    if github_owner is not None:
        from pl.trackers.github import GitHubProject
        made = GitHubProject.create_project(github_owner, github_title or f"pl {name}", v["COLUMNS"])
        tracker = {"type": "github-project", "owner": github_owner, "number": made["number"], "status_field": made["status_field"],
                   **({"repo": github_repo} if github_repo else {})}
    intake = {**v["INTAKE"], "columns": list(v["INTAKE"]["columns"]) if not isinstance(v["INTAKE"]["columns"], str) else v["INTAKE"]["columns"]}
    _write_config(d, _doc(v, tracker, intake))
    if github_owner is not None:
        print(f"created GitHub Project {made['url']} (stage field \"{made['status_field']}\")")
    print(f"created {d}/config.toml\nalias pl-{name}='PL_CONFIG_DIR=~/.pl-{name} pl'")


# ---------- --from-legacy: read the one-file pl script's settings ----------

LEGACY_SCRIPT = "~/.local/bin/pl"
LEGACY_REQUIRED = ("BOARD", "PROFILES", "USER_EMAIL", "WORK_DIR", "PROMPTS", "SERVICES")
# The legacy board server's tools, mapped onto pl's card actions (see trackers/mcp.py for the format).
LEGACY_TOOLS = {
    "columns": {"tool": "manage_boards", "args": {"action": "list_list", "board_id": "{board_id}", "per_page": 50},
                "result": "lists"},
    "cards": {"tool": "manage_boards", "args": {"action": "item_list", "board_id": "{board_id}", "fields": "*", "per_page": 10},
              "result": "list_items", "page": "page", "page_size": 10, "query": {"assigned_to": "assigned_to"}},
    "card": {"tool": "manage_boards", "args": {"action": "item_get", "item_id": "{item_id}", "full_payload": True},
             "result": "list_item"},
    "create": {"tool": "manage_boards", "args": {"action": "item_create", "list_id": "{column_id}", "*": "{fields}"},
               "result": "list_item"},
    "update": {"tool": "manage_boards", "args": {"action": "item_update", "item_id": "{item_id}", "*": "{fields}"},
               "result": "list_item"},
    "move": {"tool": "manage_boards", "args": {"action": "item_move", "item_id": "{item_id}", "target_list_id": "{column_id}"},
             "result": "list_item"},
    "delete": {"tool": "manage_boards", "args": {"action": "item_delete", "item_id": "{item_id}"}},
    "ensure_column": {"tool": "manage_boards", "args": {"action": "list_create", "board_id": "{board_id}", "title": "{column}",
                                                        "description": "{description}"}, "result": "list"},
}


class _Skip(Exception):
    pass


def _eval(node, env):
    """The few expression forms the legacy constants use; anything else is skipped, never executed."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise _Skip
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_eval(e, env) for e in node.elts]
        return set(items) if isinstance(node, ast.Set) else items
    if isinstance(node, ast.Dict):
        if None in node.keys:
            raise _Skip
        return {_eval(k, env): _eval(v, env) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if isinstance(left, Path) and isinstance(right, str):
            return left / right
        raise _Skip
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.keywords:
        f, args = node.func, [_eval(a, env) for a in node.args]
        if ast.unparse(f) == "os.environ.get" and all(isinstance(a, str) for a in args) and len(args) in (1, 2):
            return os.environ.get(*args)
        obj = _eval(f.value, env)
        if isinstance(obj, str) and f.attr in ("split", "strip") and all(isinstance(a, str) for a in args):
            return getattr(obj, f.attr)(*args)
        raise _Skip
    if isinstance(node, ast.ListComp) and len(node.generators) == 1:
        g = node.generators[0]
        if not isinstance(g.target, ast.Name) or g.is_async:
            raise _Skip
        out = []
        for x in _eval(g.iter, env):
            inner = {**env, g.target.id: x}
            if all(_eval(c, inner) for c in g.ifs):
                out.append(_eval(node.elt, inner))
        return out
    raise _Skip


def legacy_values(script: Path) -> dict:
    """The legacy script's settings, read from its source (module constants and a few literal strings)."""
    try:
        src = script.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"pl: cannot read the legacy script {script}: {e}")
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        tree = ast.Module(body=[], type_ignores=[])   # a shell wrapper, say: reported below as missing settings
    env = {"HOME": Path.home()}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                env[node.targets[0].id] = _eval(node.value, env)
            except (_Skip, TypeError, ValueError, AttributeError):
                pass
    missing = [k for k in LEGACY_REQUIRED if k not in env]
    if missing:
        raise SystemExit(f"pl: {script} is not the legacy pl script ({', '.join(missing)} not found); "
                         "point --legacy-script at the old one-file pl")
    one = lambda pattern: (m.group(1) if (m := re.search(pattern, src)) else None)  # noqa: E731
    url = lambda fn, const, key: (u.replace("{" + const + "}", "{" + key + "}")  # noqa: E731
                                  if (u := one(r'def ' + fn + r'\(item_id\):\s*return f"([^"]+)"')) else None)
    labels = re.findall(r'"--label", "([^"]+)"', src)
    repos = one(r"repos = \[t for t in tags if t in (\([^)]*\))\]")
    env["CODE_HOST"] = {"owner": one(r'"--owner", "([^"]+)"'), "labels": {k: v for k, v in {
        "review": one(r"add the (\S+) label back"), "ready": labels[0] if labels else None,
        "merge_ready": one(r'"merge" if "([^"]+)" in ls'), "rework": one(r'"rework" if "([^"]+)" in ls'),
        "failed": labels[1] if len(labels) > 1 else None}.items() if v}}
    env["_card_url"] = url("card_url", "BOARD", "board_id")
    env["_intake_url"] = url("product_url", "PRODUCT_BOARD", "board_id")
    env["_server"] = one(r'\["mcpServers"\]\["([^"]+)"\]')
    env["_repo_tags"] = list(ast.literal_eval(repos)) if repos else []
    return env


def legacy_server(mcp_json: Path, name: str | None) -> str:
    """The board server's name in an .mcp.json. Only the name is used: the harness's file keeps the server's settings."""
    try:
        servers = json.loads(mcp_json.read_text())["mcpServers"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"pl: cannot read the MCP servers in {mcp_json}: {type(e).__name__}: {e}")
    if name is None and len(servers) == 1:
        name = next(iter(servers))
    if name not in servers:
        raise SystemExit(f"pl: {mcp_json} has no server {name!r}; pass --server (one of: {', '.join(servers)})")
    return name


def from_legacy(name: str, script: Path, mcp_json: Path | None = None, server: str | None = None):
    """Write ~/.pl-<name>/config.toml from the legacy script's settings, with the board reached over MCP.
    The file names the harness's .mcp.json and the server; pl reads the server's settings from it at run time."""
    d = _target(name)
    v = legacy_values(script)
    mcp_json = (mcp_json or Path(v["WORK_DIR"]) / ".mcp.json").resolve()
    srv = {"mcp_config": str(mcp_json), "server": legacy_server(mcp_json, server or v["_server"])}
    base = C._defaults()
    base.update({k: val for k, val in v.items() if k in base})
    base["ACCOUNTS"] = {n: {"harness": "claude", "config_dir": _tilde(p)} for n, p in v["PROFILES"].items()}
    tracker = {"type": "mcp", "board_id": v["BOARD"], **srv, "tools": LEGACY_TOOLS,
               **({"card_url": v["_card_url"]} if v["_card_url"] else {})}
    intake = {"type": "mcp", "board_id": v.get("PRODUCT_BOARD"), **srv, "tools": LEGACY_TOOLS,
              "columns": list(v.get("PRODUCT_INTAKE_COLUMNS") or base["PRODUCT_INTAKE_COLUMNS"]),
              "skip_tags": sorted(v.get("PRODUCT_SKIP_TAGS") or base["PRODUCT_SKIP_TAGS"]), "repo_tags": v["_repo_tags"],
              **({"card_url": v["_intake_url"]} if v["_intake_url"] else {})}
    if not intake["board_id"]:
        intake = {}
    _write_config(d, _doc(base, tracker, intake))
    print(f"created {d}/config.toml from {script}\nalias pl-{name}='PL_CONFIG_DIR=~/.pl-{name} pl'")


def cmd_new(argv: list[str], profile: str | None = None):
    """`pl profiles new NAME [--from-current | --from-legacy]`. Runs before config.load(NAME): the folder does not exist yet."""
    ap = argparse.ArgumentParser(prog="pl profiles new")
    ap.add_argument("name")
    ap.add_argument("--from-current", action="store_true", help="copy the profile you are running as, not the defaults")
    ap.add_argument("--from-legacy", action="store_true", help="copy the settings of the old one-file pl script; the board is reached over MCP")
    ap.add_argument("--legacy-script", default=LEGACY_SCRIPT, help=f"the old script (default {LEGACY_SCRIPT})")
    ap.add_argument("--mcp-json", help="the .mcp.json holding the board's MCP server (default: <work dir>/.mcp.json)")
    ap.add_argument("--server", help="the server's name in that file (default: the one the old script used)")
    ap.add_argument("--github-project", choices=["create"], help="create a GitHub Project as this profile's tracker")
    ap.add_argument("--owner", help="GitHub user or organisation that owns the new project")
    ap.add_argument("--title", help="the new project's title (default: pl NAME)")
    ap.add_argument("--repo", help="owner/name: the repository the new project's cards are issues in")
    a = ap.parse_args(argv)
    if a.github_project and a.owner is None:
        ap.error("--github-project create needs --owner")
    if a.from_legacy:
        if a.from_current or a.github_project:
            ap.error("--from-legacy cannot be combined with --from-current or --github-project")
        return from_legacy(a.name, Path(a.legacy_script).expanduser(), Path(a.mcp_json).expanduser() if a.mcp_json else None, a.server)
    if a.from_current:
        C.load(profile)
    new_profile(a.name, a.from_current, a.owner if a.github_project else None, a.title, a.repo)


def cmd_profiles(a):
    rows = list_profiles()
    if not rows:
        print("no profiles yet: create one with pl profiles new <name>")
        return
    print(f"  {'profile':<14}{'state':<9}{'tmux session':<20}{'tracker':<44}accounts")
    for r in rows:
        acc = ", ".join(f"{n} {_tilde(d)}" for n, d in r["accounts"].items()) or "none"
        print(f"  {r['name']:<14}{'running' if r['running'] else 'stopped':<9}{r['tmux_session']:<20}{r['tracker']:<44}{acc}")
        if r["running"]:
            print(f"    dispatcher  {r['holder']}")
        print(f"    folder  {_tilde(r['dir'])}")
    for w in shared_warnings(rows):
        print(f"warning: {w}")
