"""Connectors: MCP servers registered machine-wide in <machine_dir>/connectors.toml as [connectors.<name>] tables.
The file holds no secret: a token is a ${VAR} reference or "keychain", and listings never print one."""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import tomllib
from urllib.parse import urlsplit

from pl import alerts
from pl import config as C
from pl import manager

ROLES = ("payments", "contacts", "email", "calendar", "messaging", "memory")
AUTHS = ("oauth", "token", "none")
ACCESS = ("read", "read-write")
TIERS = ("vendor", "community", "adapter")
KEYS = {"url", "command", "args", "env", "mcp_config", "server", "auth", "token", "headers", "role", "teams", "access",
        "tier", "enabled", "brain", "read_tools", "write_tools", "tools", "test", "pin"}
VAR_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie", re.I)
ADAPTER_WARNING = "filled by an adapter, not the vendor's own server"


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


def _status(c) -> str:
    if not c["enabled"]:
        return "disabled"
    auth, tok = c.get("auth", "oauth" if "url" in c else "none"), c.get("token")
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
                    "tier": c["tier"], "status": _status(c), "tools": counts.get(n), "server": _server(c),
                    "version": ver, "pin": pin, "warning": warn})
    return out


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


def cmd_connectors(a):
    sub = getattr(a, "connectors_cmd", None)
    if sub == "list":
        list_connectors(json_out=getattr(a, "json", False))
    elif sub == "approve-tools":
        approve_tools(a.name, yes=a.yes, json_out=getattr(a, "json", False))
    elif sub == "add":
        add(a.name, command=getattr(a, "command", None) or None, url=getattr(a, "url", None), pin=getattr(a, "pin", None))
        print(f"added connector {a.name}")
    else:
        raise SystemExit("usage: pl connectors list [--json] | add NAME (COMMAND... | --url URL) [--pin VERSION] | approve-tools NAME")


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


def _transport(cfg: dict):
    if cfg.get("url"):
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        return streamable_http_client(cfg["url"], http_client=create_mcp_http_client(headers=cfg.get("headers") or {}))
    cmd = cfg.get("command") or []
    cmd = [cmd] if isinstance(cmd, str) else list(cmd)
    cmd += list(cfg.get("args") or [])
    env = {**os.environ, **(cfg.get("env") or {})}
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client
    return stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env))


async def _query_raw(cfg: dict):
    from mcp import ClientSession
    async with _transport(cfg) as (r, w):
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


def add(name: str, command=None, url: str | None = None, pin: str | None = None) -> dict:
    c = {"command": list(command)} if command else ({"url": url} if url else {})
    if pin:
        c["pin"] = pin
    _edit_config(lambda table: table.__setitem__(name, c))
    version, raw = _run(_query_raw(c))
    fp, snap = _fingerprint_tools(raw, version)
    reads = [t.get("name", "") for t in raw if not is_write_tool(name, t.get("name", ""), t)]
    state = load_state()
    state[name] = {"approved_tools": fp, "approved_snapshot": snap, "approved_version": version,
                   "last_fingerprint": fp, "last_snapshot": snap, "last_version": version,
                   "known_read_tools": reads, "tools_changed": None}
    save_state(state)
    return state[name]


def set_pin(name: str, pin: str) -> None:
    _edit_config(lambda table: table[name].__setitem__("pin", pin) if name in table else table.__setitem__(name, {"pin": pin}))


def tools(name: str) -> list[dict]:
    cfg = load_config().get(name, {})
    version, raw = _run(_query_raw(cfg))
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
    cfg = load_config().get(name, {})
    async def _call():
        from mcp import ClientSession
        async with _transport(cfg) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool_name, args or {})
    res = _run(_call())
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
