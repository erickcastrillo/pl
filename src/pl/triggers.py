"""Triggers: automations that turn events or timers into pipeline cards."""
import copy
import json
import os
import re
import sys

from pl import config as C

UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
ACCEPTED_FORMS = "accepted forms: like 30s, 5m, 1h, 1d"


def parse_every(val):
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return float(val)
    m = re.fullmatch(r"(\d+)([smhd])", val.strip()) if isinstance(val, str) else None
    return float(int(m[1]) * UNIT[m[2]]) if m and int(m[1]) > 0 else None


def load_state() -> dict:
    try:
        return json.loads((C.STATE_DIR / "triggers.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = C.STATE_DIR / "triggers.json"
    tmp = p.with_name(f".triggers.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, p)


def get_triggers() -> dict:
    if hasattr(C, "TRIGGERS") and isinstance(C.TRIGGERS, dict) and C.TRIGGERS:
        return C.TRIGGERS
    p = getattr(C, "CONFIG_DIR", None)
    if p and (cfg := p / "config.toml").is_file():
        try:
            import tomllib
            return tomllib.loads(cfg.read_text()).get("triggers", {})
        except Exception:
            pass
    return getattr(C, "TRIGGERS", {}) or {}


def validate(doc: dict) -> list[str]:
    """Validate [triggers.<name>] sections in config.toml."""
    errs, conns = [], doc.get("connectors", {})
    for name, t in doc.get("triggers", {}).items():
        if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name)):
            errs.append(f"triggers.{name}: trigger name must match [a-z0-9][a-z0-9_-]{{0,31}}")
            continue
        if not isinstance(t, dict):
            errs.append(f"triggers.{name}: trigger must be a table")
            continue
        conn, pipe = t.get("connector"), t.get("pipeline")
        if not conn:
            errs.append(f"triggers.{name}.connector: connector is required")
        elif conn not in conns:
            errs.append(f"triggers.{name}.connector: unknown connector {conn!r}")
        else:
            c = conns[conn]
            if c.get("role") != "email" and not c.get("tool"):
                errs.append(f"triggers.{name}.connector: connector {conn!r} must fill the email role or specify a tool")
            teams = c.get("teams", [c["team"]] if "team" in c else None)
            team_list = [teams] if isinstance(teams, str) else list(teams) if teams is not None else None
            if team_list is not None and pipe and pipe not in team_list:
                errs.append(f"triggers.{name}.connector: connector {conn!r} is not shared with pipeline {pipe!r}")

        ev = t.get("every")
        if (t.get("kind") != "silence" or ev is not None) and parse_every(ev) is None:
            errs.append(f"triggers.{name}.every: bad interval {ev!r}; {ACCEPTED_FORMS}")
        cap = t.get("cap")
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1):
            errs.append(f"triggers.{name}.cap: cap must be a whole number of at least 1")
        if t.get("kind") != "silence" and not isinstance(t.get("query"), str):
            errs.append(f"triggers.{name}.query: query must be text")
        if "column" in t and not isinstance(t["column"], str):
            errs.append(f"triggers.{name}.column: column must be text")
        if "attach_to" in t and not (isinstance(t["attach_to"], list) and all(isinstance(x, str) for x in t["attach_to"])):
            errs.append(f"triggers.{name}.attach_to: attach_to must be a list of pipeline names")
        if "enabled" in t and not isinstance(t["enabled"], bool):
            errs.append(f"triggers.{name}.enabled: enabled must be true or false")
    return errs


def cmd_triggers(a) -> int:
    cmd = getattr(a, "triggers_cmd", None)
    if cmd == "add":
        return _cmd_add(a)
    if cmd == "list":
        return _cmd_list(a)
    if cmd in ("pause", "resume"):
        return _toggle_pause(a.name, cmd == "pause")
    print("usage: pl triggers {add,list,pause,resume} ...")
    return 0


def _cmd_add(a) -> int:
    import tomlkit
    from pl import config
    p = C.path()
    if p is None:
        raise SystemExit(C.READ_ONLY)
    doc = tomlkit.parse(p.read_text())

    entry = tomlkit.table()
    entry["connector"], entry["query"], entry["every"], entry["pipeline"] = a.connector, a.query, a.every, a.pipeline
    if a.cap is not None:
        entry["cap"] = a.cap
    if a.column:
        entry["column"] = a.column
    if a.attach_to:
        entry["attach_to"] = a.attach_to

    curr = doc.get("triggers", {})
    if a.name in curr and dict(curr[a.name]) == dict(entry):
        print(f"trigger {a.name} is unchanged")
        return 0

    chk = copy.deepcopy(doc)
    chk.setdefault("triggers", {})[a.name] = dict(entry)
    errs = config.validate(chk)
    if errs:
        sys.stderr.write(f"pl: not saved: {'; '.join(errs)}\n")
        raise SystemExit(2)

    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("pl triggers add: no terminal to confirm on; add --yes")
        try:
            ok = input(f"Add trigger {a.name}? [y/N]: ")
        except EOFError:
            ok = ""
        if ok.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 0

    if a.dry_run:
        print(f"would add trigger {a.name}")
        return 0

    doc.setdefault("triggers", tomlkit.table())[a.name] = entry
    C.save(doc)
    C.load(config_dir=str(C.CONFIG_DIR))
    print(f"added trigger {a.name}")
    return 0


def _cmd_list(a) -> int:
    st = load_state()
    records = [{
        "name": n, "connector": c.get("connector"), "query": c.get("query"), "every": c.get("every"),
        "pipeline": c.get("pipeline"), "cap": c.get("cap", 20), "column": c.get("column", "Inbox"),
        "last_run": (st.get(n) or {}).get("last_run"), "cards_today": (st.get(n) or {}).get("cards_today", 0),
        "paused": bool((st.get(n) or {}).get("paused")),
    } for n, c in sorted(get_triggers().items())]

    if getattr(a, "json", False):
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("no triggers configured")
        return 0
    for r in records:
        status = "paused" if r["paused"] else "active"
        print(f"  {r['name']:<16} {r['connector']:<10} {r['every']:<6} pipeline:{r['pipeline']:<10} cards today:{r['cards_today']}/{r['cap']} ({status})")
    return 0


def _toggle_pause(name: str, paused: bool) -> int:
    if name not in get_triggers():
        raise SystemExit(f"pl triggers {'pause' if paused else 'resume'}: no trigger {name!r}")
    st = load_state()
    st.setdefault(name, {})["paused"] = paused
    save_state(st)
    print(f"{'paused' if paused else 'resumed'} trigger {name}")
    return 0
