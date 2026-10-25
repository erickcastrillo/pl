"""Connectors: MCP servers registered machine-wide in <machine_dir>/connectors.toml as [connectors.<name>] tables.
The file holds no secret: a token is a ${VAR} reference or "keychain", and listings never print one."""
import json
import os
import re
import sys
import tomllib
from urllib.parse import urlsplit

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
    counts = _tool_counts()
    return [{"name": n, "role": c.get("role", []), "teams": c.get("teams", []), "access": c["access"],
             "tier": c["tier"], "status": _status(c), "tools": counts.get(n), "server": _server(c)}
            for n, c in load().items()]


def _cell(v) -> str:
    return (",".join(v) if isinstance(v, list) else "" if v is None else str(v)) or "-"


def cmd_connectors(a):
    if a.connectors_cmd != "list":
        raise SystemExit("usage: pl connectors list [--json]")
    rs = rows()
    if a.json:
        print(json.dumps({"connectors": rs}, indent=2))
        return
    if not rs:
        print(f"no connectors: add [connectors.<name>] tables to {path()}")
        return
    cols = ("name", "role", "teams", "access", "tier", "status", "tools")
    table = [cols] + [tuple(_cell(r[k]) for k in cols) for r in rs]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
    for r in rs:
        if r["tier"] == "adapter":
            print(f"warning: {r['name']}: {ADAPTER_WARNING}")
