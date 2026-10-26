import argparse
import json
import re
import sys
from typing import Any

from pl import manager

VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MONEY_RE = re.compile(r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)")
ADDRESS_RE = re.compile(r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b", re.IGNORECASE)
DIGITS_RE = re.compile(r"\b\d{5,}\b")

KINDS = {"gates", "alerts", "digest"}
ADAPTERS: dict[str, Any] = {}


def safe_text(s: str) -> str:
    from pl import util
    s = util.mask(str(s))
    for rx in (EMAIL_RE, MONEY_RE, ADDRESS_RE, DIGITS_RE):
        s = rx.sub("…", s)
    return s[:300]


def load() -> dict[str, dict[str, Any]]:
    conns = manager._machine_toml().get("connectors", {})
    return {name: dict(cfg) for name, cfg in conns.items() if isinstance(cfg, dict) and cfg.get("role") == "messaging"}


def add(adapter_name: str, token: str, allow: list[str], name: str | None = None, replace: bool = False, routes: list[str] | None = None) -> tuple[int, str]:
    if not token or not VAR_RE.match(token):
        return 2, "pl chat add: token must be a '${VAR}' reference; quote it (e.g. --token '${TG_TOKEN}') or pass --token-env VAR"
    name = name or adapter_name
    allow_strs, routes_list = [str(x) for x in allow], routes or ["gates"]
    conns = manager._machine_toml().get("connectors", {})
    if existing := conns.get(name):
        same = (existing.get("role") == "messaging" and existing.get("adapter") == adapter_name and
                existing.get("token") == token and existing.get("allow") == allow_strs and
                existing.get("routes", ["gates"]) == routes_list)
        if same:
            return 0, f"connector {name} already exists, unchanged"
        if not replace:
            return 2, f"pl chat add: connector {name} already exists with different settings; pass --replace to overwrite"

    import tomlkit

    def modifier(doc):
        conns_tbl = doc.setdefault("connectors", tomlkit.table())
        tbl = tomlkit.table()
        tbl.update({"role": "messaging", "adapter": adapter_name, "token": token, "allow": allow_strs, "routes": routes_list})
        conns_tbl[name] = tbl

    manager.update_machine_toml(modifier)
    return 0, f"added chat connector {name} ({adapter_name})"


def remove(name: str) -> tuple[int, str]:
    if name not in manager._machine_toml().get("connectors", {}):
        return 1, f"pl chat remove: connector {name} not found"

    def modifier(doc):
        if "connectors" in doc and name in doc["connectors"]:
            del doc["connectors"][name]

    manager.update_machine_toml(modifier)
    return 0, f"removed chat connector {name}"


def route(kind: str, names: list[str]) -> tuple[int, str]:
    if kind not in KINDS:
        return 2, f"pl chat route: unknown kind '{kind}' (must be one of: {', '.join(sorted(KINDS))})"
    conns = load()
    for name in names:
        if name not in conns:
            return 1, f"pl chat route: connector '{name}' not found"

    import tomlkit

    def modifier(doc):
        conns_tbl = doc.setdefault("connectors", tomlkit.table())
        for name, cfg in conns.items():
            tbl = conns_tbl.setdefault(name, tomlkit.table())
            r = list(cfg.get("routes", ["gates"]))
            if name in names:
                if kind not in r:
                    r.append(kind)
            else:
                if kind in r:
                    r.remove(kind)
            tbl["routes"] = r

    manager.update_machine_toml(modifier)
    return 0, f"routed {kind} to {', '.join(names) if names else 'none'}"


def adapter(name: str):
    connectors = load()
    if name in connectors:
        cfg = connectors[name]
        ad_type = cfg.get("adapter", name)
        if ad_type in ADAPTERS:
            return ADAPTERS[ad_type](name, cfg)
        if ad_type == "telegram":
            from pl.chat.telegram import Telegram
            return Telegram(name, cfg)
        if ad_type == "slack":
            from pl.chat.slack import Slack
            return Slack(name, cfg)
        if ad_type == "discord":
            from pl.chat.discord import Discord
            return Discord(name, cfg)
        raise KeyError(f"pl chat: unknown adapter {ad_type!r}")
    if name in ADAPTERS:
        return ADAPTERS[name]
    if name == "telegram":
        from pl.chat.telegram import Telegram
        return Telegram
    if name == "slack":
        from pl.chat.slack import Slack
        return Slack
    if name == "discord":
        from pl.chat.discord import Discord
        return Discord
    raise KeyError(f"pl chat: unknown connector or adapter {name!r}")


def cmd_chat(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pl chat", description="messaging connectors for approvals and alerts")
    sub = ap.add_subparsers(dest="action")
    p_add = sub.add_parser("add")
    p_add.add_argument("adapter", choices=["telegram", "slack", "discord"])
    p_add.add_argument("--token"); p_add.add_argument("--token-env")
    p_add.add_argument("--allow", nargs="+", required=True); p_add.add_argument("--name")
    p_add.add_argument("--replace", action="store_true"); p_add.add_argument("--yes", action="store_true")
    p_list = sub.add_parser("list"); p_list.add_argument("--json", action="store_true")
    p_rm = sub.add_parser("remove"); p_rm.add_argument("name"); p_rm.add_argument("--yes", action="store_true")
    p_test = sub.add_parser("test"); p_test.add_argument("name")
    p_route = sub.add_parser("route")
    p_route.add_argument("kind", choices=["gates", "alerts", "digest"])
    p_route.add_argument("name", nargs="+")

    a = ap.parse_args(argv)
    if a.action == "route":
        code, msg = route(a.kind, a.name)
        print(msg, file=sys.stderr if code else sys.stdout)
        if code:
            raise SystemExit(code)
        return 0
    if a.action == "add":
        if not a.yes and not sys.stdin.isatty():
            print("pl chat add: pass --yes when running without a terminal", file=sys.stderr)
            raise SystemExit(2)
        token = f"${{{a.token_env}}}" if a.token_env else (a.token or "")
        code, msg = add(a.adapter, token, a.allow, a.name, a.replace)
        print(msg, file=sys.stderr if code else sys.stdout)
        if code:
            raise SystemExit(code)
        return 0
    if a.action == "list":
        conns = load()
        if a.json:
            print(json.dumps({"connectors": conns}))
            return 0
        for name, c in conns.items():
            print(f"{name:16} {c.get('adapter', ''):10} token={c.get('token', '')} allow={','.join(c.get('allow', []))} routes={','.join(c.get('routes', []))}")
        return 0
    if a.action == "remove":
        if not a.yes and not sys.stdin.isatty():
            print("pl chat remove: pass --yes when running without a terminal", file=sys.stderr)
            raise SystemExit(2)
        code, msg = remove(a.name)
        print(msg, file=sys.stderr if code else sys.stdout)
        if code:
            raise SystemExit(code)
        return 0
    if a.action == "test":
        if res := adapter(a.name).test():
            raise SystemExit(res)
        return 0
    ap.print_help(file=sys.stderr)
    raise SystemExit(2)
"""Chat adapters for pl messaging connectors."""
