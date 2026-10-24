import asyncio
import concurrent.futures
import hashlib
import json
import os
import tomllib
import tomlkit

from pl import alerts
from pl import manager


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


def load_config() -> dict:
    try:
        return tomllib.loads(_path("connectors.toml").read_text()).get("connectors", {})
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    doc = tomlkit.document()
    doc["connectors"] = cfg
    _save(_path("connectors.toml"), tomlkit.dumps(doc))


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
    if isinstance(cmd, str):
        cmd = [cmd]
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
    cfg = load_config()
    c = {"command": list(command)} if command else ({"url": url} if url else {})
    if pin:
        c["pin"] = pin
    cfg[name] = c
    save_config(cfg)
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
    cfg = load_config()
    cfg.setdefault(name, {})["pin"] = pin
    save_config(cfg)


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


def call(name: str, tool_name: str, args: dict | None = None):
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


def list_connectors(json_out: bool = False):
    cfg, state, rows = load_config(), load_state(), []
    for name, c in cfg.items():
        cs = state.get(name, {})
        ver = cs.get("last_version") or cs.get("approved_version") or ""
        pin = c.get("pin")
        warn = f"warning: connector {name} version {ver} does not match pinned version {pin}" if pin and ver and ver != pin else ""
        rows.append({"name": name, "version": ver, "pin": pin, "warning": warn})
    if json_out:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['name']:20} version {r['version'] or '-'}")
        if r["warning"]:
            print(f"  {r['warning']}")


def cmd_connectors(a):
    sub = getattr(a, "connectors_cmd", None)
    if sub == "approve-tools":
        approve_tools(a.name, yes=a.yes, json_out=getattr(a, "json", False))
    elif sub == "list":
        list_connectors(json_out=getattr(a, "json", False))
    elif sub == "add":
        add(a.name, command=getattr(a, "command", None), url=getattr(a, "url", None), pin=getattr(a, "pin", None))
        print(f"added connector {a.name}")
