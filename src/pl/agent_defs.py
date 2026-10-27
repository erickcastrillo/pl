"""Named agents: built-in definitions plus the profile's agents/<name>/agent.md, and pl agents list|show."""
import json
import sys
from importlib import resources

import yaml

from pl import config as C

FIELDS = ("skills", "connector_roles", "model", "approval", "pod")
APPROVAL = ("send", "publish", "post", "quote-prices")
_LISTS = ("skills", "connector_roles", "approval")


def _builtin() -> dict:
    return yaml.safe_load(resources.files("pl").joinpath("agents_builtin.yaml").read_text())


def _parse(text: str) -> tuple[dict, str]:
    """Split agent.md into (front matter, body); ValueError when it has no valid front matter."""
    parts = text.split("---\n", 2)
    if parts[0] != "" or len(parts) != 3:
        raise ValueError("missing front matter")
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        raise ValueError(str(e)) from e
    if not isinstance(front, dict):
        raise ValueError("front matter must be a mapping")
    return front, parts[2].strip()


def _profile() -> dict:
    found = {}
    root = C.CONFIG_DIR / "agents"
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not C.NAME_RE.match(folder.name):
            continue
        try:
            front, body = _parse((folder / "agent.md").read_text())
        except (OSError, ValueError):
            continue
        found[folder.name] = {**front, "instructions": body}
    return found


def _agent(name: str, source: str, raw: dict) -> dict:
    agent = {"name": name, "source": source, "instructions": str(raw.get("instructions") or "")}
    for field in FIELDS:
        value = raw.get(field)
        agent[field] = (value if isinstance(value, list) else []) if field in _LISTS else value
    return agent


def load() -> dict:
    agents = {n: _agent(n, "built-in", raw) for n, raw in _builtin().items()}
    agents.update({n: _agent(n, "profile", raw) for n, raw in _profile().items()})
    return agents


def describe(agent: dict) -> dict:
    role = next((ln.strip() for ln in agent["instructions"].splitlines() if ln.strip()), "")
    return {"name": agent["name"], "source": agent["source"], "role": role, **{f: agent[f] for f in FIELDS}}


def cmd_agents(a) -> None:
    agents = load()
    if a.agents_cmd == "list":
        rows = [describe(agents[n]) for n in sorted(agents)]
        if a.json:
            print(json.dumps(rows))
        else:
            for r in rows:
                print(f"{r['name']}  {r['source']}  {r['role']}")
        return
    agent = agents.get(a.name)
    if agent is None:
        print(f"no agent named {a.name}", file=sys.stderr)
        raise SystemExit(2)
    if a.json:
        print(json.dumps({**describe(agent), "instructions": agent["instructions"]}))
        return
    print(agent["instructions"])
    print(f"skills: {', '.join(agent['skills'])}")
    print(f"connector roles: {', '.join(agent['connector_roles'])}")
    print(f"model: {agent['model'] or 'stage default'}")
    print(f"approval: {', '.join(agent['approval'])}")
    print(f"pod: {agent['pod'] or ''}")
