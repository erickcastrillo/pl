"""Connectors: MCP servers registered machine-wide in <machine_dir>/connectors.toml as [connectors.<name>] tables.
The file holds no secret: a token is a ${VAR} reference or "keychain", and listings never print one."""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import tomllib
from datetime import date
from urllib.parse import urlsplit

from pl import alerts
from pl import config as C
from pl import connectors_oauth, manager, secrets
from pl.util import ask

ROLES = ("payments", "contacts", "email", "calendar", "messaging", "memory")
AUTHS = ("oauth", "token", "none")
ACCESS = ("read", "read-write")
TIERS = ("vendor", "community", "adapter")
KEYS = {"url", "command", "args", "env", "mcp_config", "server", "auth", "token", "headers", "role", "teams", "access",
        "tier", "enabled", "brain", "read_tools", "write_tools", "tools", "test", "pin", "server_hash"}
VAR_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie", re.I)
ADAPTER_WARNING = "filled by an adapter, not the vendor's own server"
ROLE_PROBES = {"payments": ("payments_since", lambda: {"since": date.today().isoformat()})}


def path():
    return manager.machine_dir() / "connectors.toml"


def load() -> dict:
    """Every connector as {name: table} with access, tier and enabled filled in; {} when there is no file."""
    try:
        table = tomllib.loads(path().read_text()).get("connectors", {})
    except FileNotFoundError:
        return {}
    return {n: {"access": "read", "tier": "vendor", "enabled": True, **c} for n, c in table.items()}


def get(name):
    return load().get(name)


def for_team(team) -> list:
    """The enabled connectors that list this team, as [(name, table)]."""
    return [(n, c) for n, c in load().items() if c["enabled"] and team in c.get("teams", [])]


def _strings(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _connector_errors(name, c) -> list[str]:
    at = f"connectors.{name}"
    if not isinstance(c, dict):
        return [f"{at} must be a table"]
    errs = [f"{at}.{k} is not a known key" for k in c if k not in KEYS]
    servers = [k for k in ("url", "command", "mcp_config") if k in c]
    if len(servers) != 1 or ("mcp_config" in c) != ("server" in c):
        errs.append(f"{at} needs exactly one of url, command, or mcp_config with server")
    url = c.get("url")
    if url is not None:
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            errs.append(f"{at}.url must be an http(s) url without a user or password")
    if c.get("auth", "none") not in AUTHS:
        errs.append(f"{at}.auth must be one of {', '.join(AUTHS)}")
    tok = c.get("token")
    if tok is not None and not (isinstance(tok, str) and (tok == "keychain" or VAR_RE.match(tok))):
        errs.append(f'{at}.token must be ${{VAR}} or "keychain"')
    if c.get("auth") == "token" and tok is None:
        errs.append(f'{at}.token is required with auth = "token": ${{VAR}} or "keychain"')
    for h, v in (c.get("headers") or {}).items():
        if SECRET_HEADER.search(h) and not (isinstance(v, str) and "${" in v):
            errs.append(f"{at}.headers.{h} must use a ${{VAR}} reference, not a literal value")
    if "role" in c and not (_strings(c["role"]) and set(c["role"]) <= set(ROLES)):
        errs.append(f"{at}.role must be a list of: {', '.join(ROLES)}")
    if "teams" in c and not (_strings(c["teams"]) and all(C.NAME_RE.match(t) for t in c["teams"])):
        errs.append(f"{at}.teams must be a list of team names matching {C.NAME_RE.pattern}")
    for key, allowed in (("access", ACCESS), ("tier", TIERS)):
        if key in c and c[key] not in allowed:
            errs.append(f"{at}.{key} must be one of {', '.join(allowed)}")
    for key in ("enabled", "brain"):
        if key in c and not isinstance(c[key], bool):
            errs.append(f"{at}.{key} must be true or false")
    for key in ("args", "read_tools", "write_tools"):
        if key in c and not _strings(c[key]):
            errs.append(f"{at}.{key} must be a list of strings")
    if "tools" in c and not (isinstance(c["tools"], dict) and all(isinstance(v, str) for v in c["tools"].values())):
        errs.append(f"{at}.tools must map a role tool name to a server tool name")
    if "test" in c and not (isinstance(c["test"], dict) and isinstance(c["test"].get("tool"), str)):
        errs.append(f"{at}.test must be a table with a tool name and optional args")
    return errs


def validate(doc) -> list[str]:
    """Problems in a parsed connectors.toml, each led by its key path (empty = fine)."""
    table = doc.get("connectors", {})
    if not isinstance(table, dict):
        return ["connectors must be a table of [connectors.<name>] tables"]
    errs = []
    for name, c in table.items():
        if not C.NAME_RE.match(name):
            errs.append(f"connectors.{name}: name must match {C.NAME_RE.pattern}")
        errs += _connector_errors(name, c)
    return errs


def check() -> None:
    """Config load calls this: a connectors.toml that does not parse or validate prints every problem and exits 2."""
    try:
        errs = validate(tomllib.loads(path().read_text()))
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        errs = [f"{path()}: {e}"]
    if errs:
        print("pl: connectors.toml: " + "\n  ".join(errs), file=sys.stderr)
        raise SystemExit(2)


def save(doc) -> None:
    """Write a tomlkit document over connectors.toml: validated, temp file 0600, fsync, os.replace."""
    import tomlkit
    errs = validate(doc)
    if errs:
        raise SystemExit("pl: not saved: " + "; ".join(errs))
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(tomlkit.dumps(doc))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _server(c) -> str:
    if "url" in c:
        parts = urlsplit(c["url"])
        return f"{parts.scheme}://{parts.hostname}"
    return "stdio" if "command" in c else f"{c['server']} (harness MCP file)"


def _tool_counts() -> dict:
    try:
        return json.loads((manager.machine_dir() / "connector-tools.json").read_text())
    except (OSError, ValueError):
        return {}


SIGNIN_STATUS = {"needs_signin": "needs sign-in", "connected": "connected"}


def _status(c, cs=None) -> str:
    if not c["enabled"]:
        return "disabled"
    auth, tok = c.get("auth", "oauth" if "url" in c else "none"), c.get("token")
    if auth == "oauth" and (cs or {}).get("status") in SIGNIN_STATUS:
        return SIGNIN_STATUS[cs["status"]]
    if auth == "none" or (auth == "token" and VAR_RE.match(tok or "") and os.environ.get(tok[2:-1])):
        return "connected"
    return "not signed in"


def rows() -> list[dict]:
    counts, state = _tool_counts(), load_state()
    out = []
    for n, c in load().items():
        cs = state.get(n, {})
        ver, pin = cs.get("last_version") or cs.get("approved_version") or "", c.get("pin")
        warn = f"connector {n} version {ver} does not match pinned version {pin}" if pin and ver and ver != pin else ""
        out.append({"name": n, "role": c.get("role", []), "teams": c.get("teams", []), "access": c["access"],
                    "tier": c["tier"], "status": _status(c, cs), "tools": counts.get(n), "server": _server(c),
                    "version": ver, "pin": pin, "warning": warn})
    return out


def status() -> dict:
    """connector name -> status text; Home reads this."""
    state = load_state()
    return {n: _status(c, state.get(n)) for n, c in load().items()}


def _cell(v) -> str:
    return (",".join(v) if isinstance(v, list) else "" if v is None else str(v)) or "-"


def list_connectors(json_out: bool = False):
    rs = rows()
    if json_out:
        print(json.dumps({"connectors": rs}, indent=2))
        return
    if not rs:
        print(f"no connectors: add [connectors.<name>] tables to {path()}")
        return
    cols = ("name", "role", "teams", "access", "tier", "status", "tools", "version")
    table = [cols] + [tuple(_cell(r[k]) for k in cols) for r in rs]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
    for r in rs:
        if r["tier"] == "adapter":
            print(f"warning: {r['name']}: {ADAPTER_WARNING}")
        if r["warning"]:
            print(f"warning: {r['warning']}")


def revoke(name: str) -> None:
    """Disable a connector (saved first, so a failure below leaves it off), revoke its tokens at the server, then delete them."""
    cfg = load_config().get(name)
    if cfg is None:
        raise SystemExit(f"pl connectors revoke: no connector {name!r}")
    _edit_config(lambda table: table[name].__setitem__("enabled", False))
    print(f"{name}: disabled")
    try:
        stored = secrets.get(name) or {}
    except Exception:
        stored = {}
    print(_run(connectors_oauth.revoke_at_server(cfg["url"], stored)) if cfg.get("url") else "server: nothing to revoke")
    try:
        secrets.delete(name)
    except Exception as e:
        print(f"tokens: not deleted ({type(e).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
    print("tokens: deleted")
    state = load_state()
    if state.get(name, {}).pop("status", None):
        save_state(state)
    alerts.resolve(f"connector_signin:{name}")
    alerts.resolve(f"connector_tools:{name}")


def _usage_error(msg: str):
    print(f"pl connectors: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _probe(name: str, cfg: dict, listed: list[dict]):
    """(tool, args) of the one read call `pl connectors test` makes: the test mapping, else the role's probe, else the first read tool without required arguments."""
    reads = {t["name"]: t for t in listed if not t["write"]}
    if test := cfg.get("test"):
        if test["tool"] not in reads:
            _usage_error(f"{name}: test tool {test['tool']} is not a read tool of the server")
        return test["tool"], test.get("args") or {}
    for r in cfg.get("role", []):
        if r in ROLE_PROBES:
            std, make_args = ROLE_PROBES[r]
            if (tool := (cfg.get("tools") or {}).get(std, std)) in reads:
                return tool, make_args()
    for tool, t in reads.items():
        if not (t.get("inputSchema") or {}).get("required"):
            return tool, {}
    _usage_error(f"{name}: no read tool to call; set [connectors.{name}] test")


def _masked(text, limit: int = 300) -> str:
    from pl.trackers import mcp
    return mcp._mask(str(text), sorted(mcp._secrets, key=len, reverse=True))[:limit]


def test_connector(name: str, json_out: bool = False) -> None:
    cfg = load_config().get(name)
    if cfg is None:
        _usage_error(f"no connector {name!r}")
    tool = None
    try:
        tool, args = _probe(name, cfg, tools(name))
        start = time.monotonic()
        result = call(name, tool, args)
        ms = round((time.monotonic() - start) * 1000)
    except (Exception, BaseExceptionGroup) as e:
        while isinstance(e, BaseExceptionGroup):
            e = e.exceptions[0]
        error = _masked(e)
        print(json.dumps({"connector": name, "tool": tool, "ok": False, "error": error}) if json_out
              else f"pl connectors test: {name}: {error}", file=sys.stdout if json_out else sys.stderr)
        raise SystemExit(1) from None
    text = _masked(result)
    print(json.dumps({"connector": name, "tool": tool, "ok": True, "ms": ms, "result": text}) if json_out
          else f"{name}: {tool} ok in {ms} ms\n{text}")


def _confirm_revoke(a) -> None:
    if getattr(a, "yes", False):
        return
    if not sys.stdin.isatty():
        _usage_error(f"pass --yes to revoke {a.name} without asking")
    if ask("connectors revoke", f"revoke connector {a.name}: disable it and delete its tokens? [y/N] ").strip().lower() != "y":
        raise SystemExit("pl connectors revoke: cancelled")


def _audit(action, name):
    try:
        from pl import audit
        audit.append(action, target=name)
    except Exception:
        pass


def cmd_connectors(a):
    sub = getattr(a, "connectors_cmd", None)
    if sub == "list":
        list_connectors(json_out=getattr(a, "json", False))
    elif sub == "approve-tools":
        approve_tools(a.name, yes=a.yes, json_out=getattr(a, "json", False))
    elif sub == "revoke":
        _confirm_revoke(a)
        try:
            revoke(a.name)
        finally:
            _audit("connectors.revoke", a.name)
    elif sub == "test":
        test_connector(a.name, json_out=getattr(a, "json", False))
    elif sub == "import":
        import_servers(a.source, add_name=a.add, teams=a.team, roles=a.role, yes=a.yes, json_out=a.json)
    elif sub == "add":
        add_cmd(a)
    elif sub == "serve":
        from pl import connectors_proxy
        connectors_proxy.serve(a.name, a.card, a.stage)
    else:
        raise SystemExit("usage: pl connectors list [--json] | add NAME (COMMAND... | --url URL) [--pin VERSION] | import --from claude [--add NAME] | approve-tools NAME | test NAME [--json] | revoke NAME --yes | serve NAME --card ID --stage S")


def add_cmd(a) -> None:
    command, url = list(getattr(a, "command", None) or []), getattr(a, "url", None)
    if not url and len(command) == 1 and re.match(r"https?://", command[0]):
        url, command = command[0], []
    if a.name in load_config():
        print(f"connector {a.name} already exists, unchanged")
        return
    if not a.yes and ask("connectors add", f"add connector {a.name} ({url or ' '.join(command)}) and sign in? [y/N] ").strip().lower() != "y":
        raise SystemExit("pl connectors add: cancelled")
    try:
        state = add(a.name, command=command or None, url=url, pin=getattr(a, "pin", None), role=getattr(a, "role", None),
                    teams=getattr(a, "team", None), access=getattr(a, "access", None), tier=getattr(a, "tier", None))
    except BaseExceptionGroup as group:
        while isinstance(group, BaseExceptionGroup):
            group = group.exceptions[0]
        raise SystemExit(f"pl connectors add: {a.name}: {group}")
    _audit("connectors.add", a.name)
    count = len(state["approved_snapshot"])
    if getattr(a, "json", False):
        print(json.dumps({"connector": a.name, "status": "connected", "tools": count}))
    else:
        print(f"connector {a.name} connected ({count} tools)")


def load_config() -> dict:
    """The raw [connectors.<name>] tables, no defaults filled in."""
    try:
        return tomllib.loads(path().read_text()).get("connectors", {})
    except (OSError, ValueError):
        return {}


def _edit_config(mutate) -> None:
    """Change the connectors table in place (comments and layout kept) and write it validated."""
    import tomlkit
    try:
        doc = tomlkit.parse(path().read_text())
    except OSError:
        doc = tomlkit.document()
    mutate(doc.setdefault("connectors", tomlkit.table()))
    save(doc)


def _path(name):
    return manager.machine_dir() / name


def _save(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, p)


def load_state() -> dict:
    try:
        return json.loads(_path("connectors-state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    _save(_path("connectors-state.json"), json.dumps(d, indent=2))


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def server_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def resolve_server(cfg: dict) -> dict:
    """cfg with an `mcp_config` + `server` pair replaced by the harness file's entry, ${VAR} expanded as the harness does."""
    if "mcp_config" not in cfg:
        return cfg
    from pl.trackers import mcp
    entry = mcp._from_file(cfg["mcp_config"], cfg["server"])
    if cfg.get("server_hash") and server_hash(entry) != cfg["server_hash"]:
        raise SystemExit(f"pl: MCP server {cfg['server']!r} in {cfg['mcp_config']} changed since it was imported; re-import it to approve the new entry")
    server = mcp._expand_env(entry, [])
    return {k: v for k, v in server.items() if k in ("url", "command", "args", "env", "headers")}


def _auth(name: str, cfg: dict):
    if cfg.get("auth") == "oauth" and cfg.get("url"):
        return connectors_oauth.provider(name, cfg["url"], interactive=False)


def _sign_in_error(e: BaseException):
    """The NeedsSignIn inside e, which the SDK's task groups may wrap in exception groups."""
    if isinstance(e, connectors_oauth.NeedsSignIn):
        return e
    for inner in getattr(e, "exceptions", ()):
        if found := _sign_in_error(inner):
            return found


def _mark_sign_in(name: str, needed: bool) -> None:
    state = load_state()
    cs = state.setdefault(name, {})
    status = "needs_signin" if needed else "connected"
    if cs.get("status") == status:
        return
    cs["status"] = status
    save_state(state)
    if needed:
        alerts.open(f"connector_signin:{name}", "warn", f"Connector {name} needs sign-in", f"pl connectors add {name} --reauth")
    else:
        alerts.resolve(f"connector_signin:{name}")


def _run_signed_in(name: str, cfg: dict, make):
    """_run(make(auth)), with the connector's stored sign-in; a sign-in that cannot be refreshed marks it needs_signin."""
    auth = _auth(name, cfg)
    try:
        result = _run(make(auth))
    except BaseException as e:
        if auth and (found := _sign_in_error(e)):
            _mark_sign_in(name, True)
            raise found from None
        raise
    if auth:
        _mark_sign_in(name, False)
    return result


def active_signin_alerts() -> set:
    return {f"connector_signin:{n}" for n, cs in load_state().items() if cs.get("status") == "needs_signin"}


def _transport(cfg: dict, auth=None):
    cfg = resolve_server(cfg)
    if cfg.get("url"):
        from mcp.client.streamable_http import streamable_http_client
        return streamable_http_client(cfg["url"], http_client=connectors_oauth.http_client(cfg.get("headers") or {}, auth))
    cmd = cfg.get("command") or []
    cmd = [cmd] if isinstance(cmd, str) else list(cmd)
    cmd += list(cfg.get("args") or [])
    env = {**os.environ, **(cfg.get("env") or {})}
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client
    return stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env))


async def _query_raw(cfg: dict, auth=None):
    from mcp import ClientSession
    async with _transport(cfg, auth) as (r, w, *_):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            version = str(getattr(getattr(init, "serverInfo", None), "version", "") or "")
            t_res = await session.list_tools()
            tools = [t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in t_res.tools]
            return version, tools


def _fingerprint_tools(tools: list[dict], version: str):
    items = []
    for t in tools:
        name = t.get("name") or ""
        sh = hashlib.sha256(json.dumps(t.get("inputSchema") or t.get("input_schema") or {}, sort_keys=True).encode()).hexdigest()
        dh = hashlib.sha256((t.get("description") or "").encode()).hexdigest()
        ann = t.get("annotations") or {}
        ah = hashlib.sha256(json.dumps(ann.model_dump() if hasattr(ann, "model_dump") else ann, sort_keys=True).encode()).hexdigest()
        items.append([name, sh, dh, ah])
    sorted_items = sorted(items, key=lambda x: x[0])
    fp = hashlib.sha256(json.dumps({"tools": sorted_items, "version": str(version or "")}, sort_keys=True).encode()).hexdigest()
    return fp, {i[0]: i[1:] for i in sorted_items}


def is_write_tool(name: str, tool_name: str, tool_dict: dict | None = None) -> bool:
    cfg = load_config().get(name, {})
    if tool_name in cfg.get("write_tools", []):
        return True
    if tool_name in cfg.get("read_tools", []):
        return False
    if tool_name in load_state().get(name, {}).get("known_read_tools", []):
        return False
    if tool_dict:
        ann = tool_dict.get("annotations") or {}
        if getattr(ann, "readOnlyHint", False) is True or (isinstance(ann, dict) and ann.get("readOnlyHint") is True):
            return False
    return True


def is_write_forbidden(name: str, tool_name: str) -> bool:
    return bool(load_state().get(name, {}).get("tools_changed")) and is_write_tool(name, tool_name)


def active_tools_alerts() -> set:
    return {f"connector_tools:{n}" for n, cs in load_state().items() if cs.get("tools_changed")}


async def _needs_oauth(url: str) -> bool:
    """A 401 that names its resource metadata (header or well-known document) means the server wants OAuth."""
    async with connectors_oauth.http_client() as http:
        r = await http.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                            headers={"accept": "application/json, text/event-stream"})
        if r.status_code != 401:
            return False
        if "www-authenticate" in r.headers:
            return True
        parts = urlsplit(url)
        return (await http.get(f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource")).status_code == 200


def add(name: str, command=None, url: str | None = None, pin: str | None = None, role=None, teams=None, access=None,
        tier=None) -> dict:
    c = {"command": list(command)} if command else ({"url": url} if url else {})
    c.update({k: v for k, v in (("pin", pin), ("role", role), ("teams", teams), ("access", access), ("tier", tier)) if v})
    _edit_config(lambda table: table.__setitem__(name, c))
    auth = None
    if url and _run(_needs_oauth(url)):
        _edit_config(lambda table: table[name].__setitem__("auth", "oauth"))
        c["auth"], auth = "oauth", connectors_oauth.provider(name, url, interactive=True)
    version, raw = _run(_query_raw(c, auth))
    if auth:
        connectors_oauth.record_sign_in(name)
    fp, snap = _fingerprint_tools(raw, version)
    reads = [t.get("name", "") for t in raw if not is_write_tool(name, t.get("name", ""), t)]
    state = load_state()
    state[name] = {"approved_tools": fp, "approved_snapshot": snap, "approved_version": version,
                   "last_fingerprint": fp, "last_snapshot": snap, "last_version": version,
                   "known_read_tools": reads, "tools_changed": None, **({"status": "connected"} if auth else {})}
    save_state(state)
    alerts.resolve(f"connector_signin:{name}")
    return state[name]


def set_pin(name: str, pin: str) -> None:
    _edit_config(lambda table: table[name].__setitem__("pin", pin) if name in table else table.__setitem__(name, {"pin": pin}))


def tools(name: str) -> list[dict]:
    cfg = load_config().get(name, {})
    version, raw = _run_signed_in(name, cfg, lambda auth: _query_raw(cfg, auth))
    fp, snap = _fingerprint_tools(raw, version)
    state = load_state()
    cs = state.setdefault(name, {})
    ver_mismatch = bool(cfg.get("pin") and version != cfg.get("pin"))
    reads = [t.get("name", "") for t in raw if not is_write_tool(name, t.get("name", ""), t)]
    cs["known_read_tools"] = list(set((cs.get("known_read_tools") or []) + reads))

    if cs.get("approved_tools") is None:
        cs.update(approved_tools=fp, approved_snapshot=snap, approved_version=version,
                  last_fingerprint=fp, last_snapshot=snap, last_version=version, tools_changed=None)
        save_state(state)
    elif fp != cs.get("approved_tools") or ver_mismatch:
        prev = cs.get("approved_snapshot") or {}
        added = sorted(list(set(snap.keys()) - set(prev.keys())))
        removed = sorted(list(set(prev.keys()) - set(snap.keys())))
        changed = sorted([k for k in (set(snap.keys()) & set(prev.keys())) if snap[k] != prev[k]])
        if (version != cs.get("approved_version") or ver_mismatch) and f"version {version}" not in changed:
            changed.append(f"version {version}")
        diff = {"added": added, "removed": removed, "changed": changed}
        cs.update(tools_changed=diff, last_fingerprint=fp, last_snapshot=snap, last_version=version)
        save_state(state)
        names = ", ".join(added + changed + removed) or f"version {version}"
        alerts.open(f"connector_tools:{name}", "warn", f"Connector {name}: unapproved tool change: {names}",
                    f"pl connectors approve-tools {name}")
        try:
            from pl import audit
            audit.append("connector_tools_changed", target=name, detail=diff)
        except (ImportError, AttributeError):
            pass
    return [{**dict(t), "write": is_write_tool(name, t.get("name", ""), t)} for t in raw]


def call(name: str, tool_name: str, args: dict | None = None, mode: str | None = None):   # mode: who calls (trigger, stage); reads and writes are told apart by the tool, not the mode
    if is_write_forbidden(name, tool_name):
        raise PermissionError(f"Connector {name}: write tool {tool_name} forbidden while tools are unapproved")
    if mode in ("trigger", "reader") and is_write_tool(name, tool_name):
        raise PermissionError(f"Connector {name}: write tool {tool_name} is never called in {mode} mode")
    cfg = load_config().get(name, {})
    async def _call(auth):
        from mcp import ClientSession
        async with _transport(cfg, auth) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool_name, args or {})
    res = _run_signed_in(name, cfg, _call)
    return res.content[0].text if getattr(res, "content", None) and hasattr(res.content[0], "text") else res


def approve_tools(name: str, yes: bool = True, json_out: bool = False) -> dict:
    state = load_state()
    cs = state.setdefault(name, {})
    diff = cs.get("tools_changed") or {}
    fp, snap, ver = cs.get("last_fingerprint"), cs.get("last_snapshot"), cs.get("last_version")
    if not fp:
        ver, raw = _run(_query_raw(load_config().get(name, {})))
        fp, snap = _fingerprint_tools(raw, ver)
    cs.update(approved_tools=fp, approved_snapshot=snap, approved_version=ver, tools_changed=None)
    save_state(state)
    alerts.resolve(f"connector_tools:{name}")
    try:
        from pl import audit
        audit.append("connectors.approve_tools", target=name, detail=diff)
    except (ImportError, AttributeError):
        pass
    print(json.dumps({"ok": True, "connector": name, "diff": diff}, indent=2) if json_out
          else f"approved tools for connector {name}: {diff}")
    return diff


def role(name: str):
    """(connector name, table) of the one enabled connector that fills `name` for this pipeline's team."""
    team = (getattr(C, "PIPELINE", None) or {}).get("name") or C.PROFILE_NAME
    hits = [(n, c) for n, c in for_team(team) if name in c.get("role", [])]
    if len(hits) > 1:
        raise SystemExit(f"pl: more than one connector fills role {name!r} for team {team!r}: {', '.join(n for n, _ in hits)}")
    if not hits:
        raise SystemExit(f"pl: no connector fills role {name!r} for team {team!r}")
    return hits[0]


def call_role(role_name: str, std_tool: str, **args):
    """Call a standard role tool on whichever connector fills the role, through that connector's `tools` mapping."""
    name, c = role(role_name)
    return call(name, (c.get("tools") or {}).get(std_tool, std_tool), args)


def claude_servers_in(file) -> dict:
    """The mcpServers of one Claude Code file; {} for a credentials file (never opened) or an unreadable one."""
    from pathlib import Path
    from pl import harnesses
    file = Path(file)
    if harnesses.SECRET_RE.fullmatch(file.name):
        return {}
    try:
        servers = json.loads(file.read_text()).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return {}
    return {n: v for n, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def claude_servers() -> list[dict]:
    """Every server in each account's .claude.json and the work folder's .mcp.json, as {name, file, kind}: no values."""
    from pathlib import Path
    files = [Path(d) / ".claude.json" for d in C.PROFILES.values()] + ([Path(C.WORK_DIR) / ".mcp.json"] if C.WORK_DIR else [])
    out = []
    for f in dict.fromkeys(files):
        out += [{"name": n, "file": str(f), "kind": "url" if "url" in v else "command"} for n, v in claude_servers_in(f).items()]
    return out


def _fail(msg):
    print(f"pl connectors import: {msg}", file=sys.stderr)
    raise SystemExit(2)


def import_servers(source, add_name=None, teams=None, roles=None, yes=False, json_out=False):
    if source != "claude":
        _fail("Codex's TOML MCP file is not supported yet")
    found = claude_servers()
    if not add_name:
        if json_out:
            print(json.dumps(found, indent=2))
        else:
            for r in found:
                print(f"{r['name']}  {r['kind']}  {r['file']}")
            if not found:
                print("no MCP servers found in Claude Code's files")
        return
    match = next((r for r in found if r["name"] == add_name), None)
    if not match:
        _fail(f"no server {add_name!r} in Claude Code's files")
    if bad := [r for r in roles or [] if r not in ROLES]:
        _fail(f"unknown role {', '.join(bad)}; choose from {', '.join(ROLES)}")
    if add_name in load_config():
        _fail(f"connector {add_name!r} already exists")
    if not yes and ask("connectors add", f"add {add_name} from {match['file']}? [y/N] ").strip().lower() != "y":
        _fail("not added")
    from pl.trackers import mcp
    entry = {"mcp_config": match["file"], "server": add_name, "server_hash": server_hash(mcp._from_file(match["file"], add_name)),
             **({"teams": teams} if teams else {}), **({"role": roles} if roles else {})}
    _edit_config(lambda table: table.__setitem__(add_name, entry))
    _audit("connectors.import", add_name)
    print(f"added connector {add_name} (uses {match['file']}; no key copied)")
