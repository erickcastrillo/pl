import argparse
import json
import re
import sys
from typing import Any

from pl import manager

VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
MONEY_RE = re.compile(
    r"([$€£¥]\s*\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY)\b|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\s*\d+(?:[.,]\d+)?)"
)
ADDRESS_RE = re.compile(
    r"\b\d+\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+)?\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b",
    re.IGNORECASE,
)
DIGITS_RE = re.compile(r"\b\d{5,}\b")


def safe_text(s: str) -> str:
    from pl import util
    s = util.mask(str(s))
    s = EMAIL_RE.sub("…", s)
    s = MONEY_RE.sub("…", s)
    s = ADDRESS_RE.sub("…", s)
    s = DIGITS_RE.sub("…", s)
    return s[:300]


def load() -> dict[str, dict[str, Any]]:
    conns = manager._machine_toml().get("connectors", {})
    return {
        name: dict(cfg)
        for name, cfg in conns.items()
        if isinstance(cfg, dict) and cfg.get("role") == "messaging"
    }


def add(
    adapter_name: str,
    token: str,
    allow: list[str],
    name: str | None = None,
    replace: bool = False,
    routes: list[str] | None = None,
) -> tuple[int, str]:
    if not token or not VAR_RE.match(token):
        return 2, "pl chat add: token must be a '${VAR}' reference; quote it (e.g. --token '${TG_TOKEN}') or pass --token-env VAR"

    name = name or adapter_name
    allow_strs = [str(x) for x in allow]
    routes_list = routes or ["gates"]

    conns = manager._machine_toml().get("connectors", {})
    existing = conns.get(name)
    if existing is not None:
        same = (
            existing.get("role") == "messaging"
            and existing.get("adapter") == adapter_name
            and existing.get("token") == token
            and existing.get("allow") == allow_strs
            and existing.get("routes", ["gates"]) == routes_list
        )
        if same:
            return 0, f"connector {name} already exists, unchanged"
        if not replace:
            return 2, f"pl chat add: connector {name} already exists with different settings; pass --replace to overwrite"

    import tomlkit

    def modifier(doc):
        if "connectors" not in doc:
            doc["connectors"] = tomlkit.table()
        table = tomlkit.table()
        table["role"] = "messaging"
        table["adapter"] = adapter_name
        table["token"] = token
        table["allow"] = allow_strs
        table["routes"] = routes_list
        doc["connectors"][name] = table

    manager.update_machine_toml(modifier)
    return 0, f"added chat connector {name} ({adapter_name})"


def remove(name: str) -> tuple[int, str]:
    conns = manager._machine_toml().get("connectors", {})
    if name not in conns:
        return 1, f"pl chat remove: connector {name} not found"

    def modifier(doc):
        if "connectors" in doc and name in doc["connectors"]:
            del doc["connectors"][name]

    manager.update_machine_toml(modifier)
    return 0, f"removed chat connector {name}"


def adapter(name: str):
    connectors = load()
    if name in connectors:
        cfg = connectors[name]
        ad_type = cfg.get("adapter", name)
        if ad_type == "telegram":
            from pl.chat.telegram import Telegram
            return Telegram(name, cfg)
        raise KeyError(f"pl chat: unknown adapter {ad_type!r}")
    if name == "telegram":
        from pl.chat.telegram import Telegram
        return Telegram
    raise KeyError(f"pl chat: unknown connector or adapter {name!r}")


def cmd_chat(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pl chat", description="messaging connectors for approvals and alerts")
    sub = ap.add_subparsers(dest="action")

    p_add = sub.add_parser("add")
    p_add.add_argument("adapter", choices=["telegram", "slack", "discord"])
    p_add.add_argument("--token")
    p_add.add_argument("--token-env")
    p_add.add_argument("--allow", nargs="+", required=True)
    p_add.add_argument("--name")
    p_add.add_argument("--replace", action="store_true")
    p_add.add_argument("--yes", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    p_rm.add_argument("--yes", action="store_true")

    p_test = sub.add_parser("test")
    p_test.add_argument("name")

    a = ap.parse_args(argv)

    if a.action == "add":
        if not a.yes and not sys.stdin.isatty():
            print("pl chat add: pass --yes when running without a terminal", file=sys.stderr)
            raise SystemExit(2)
        token = f"${{{a.token_env}}}" if a.token_env else (a.token or "")
        code, msg = add(
            adapter_name=a.adapter,
            token=token,
            allow=a.allow,
            name=a.name,
            replace=a.replace,
        )
        out = sys.stderr if code != 0 else sys.stdout
        print(msg, file=out)
        if code != 0:
            raise SystemExit(code)
        return 0

    if a.action == "list":
        conns = load()
        if a.json:
            print(json.dumps({"connectors": conns}))
            return 0
        for name, c in conns.items():
            allow_str = ",".join(c.get("allow", []))
            routes_str = ",".join(c.get("routes", []))
            print(f"{name:16} {c.get('adapter', ''):10} token={c.get('token', '')} allow={allow_str} routes={routes_str}")
        return 0

    if a.action == "remove":
        if not a.yes and not sys.stdin.isatty():
            print("pl chat remove: pass --yes when running without a terminal", file=sys.stderr)
            raise SystemExit(2)
        code, msg = remove(a.name)
        out = sys.stderr if code != 0 else sys.stdout
        print(msg, file=out)
        if code != 0:
            raise SystemExit(code)
        return 0

    if a.action == "test":
        ad = adapter(a.name)
        res = ad.test()
        if res:
            raise SystemExit(res)
        return 0

    ap.print_help(file=sys.stderr)
    raise SystemExit(2)
