"""Named agents: built-in definitions plus the profile's agents/<name>/agent.md, and pl agents list|show."""
import json
import os
import re
import shutil
import sys
import tempfile
from importlib import resources

import yaml

from pl import config as C
from pl import connectors, harnesses, skills, util

FIELDS = ("skills", "connector_roles", "model", "approval", "pod")
APPROVAL = ("send", "publish", "post", "quote-prices")
_LISTS = ("skills", "connector_roles", "approval")
SECRET_KEY = re.compile(r"auth|key|token|secret|cookie", re.I)
TEMPLATE = """---
skills: []
connector_roles: []
model: null
approval: [send, publish, post, quote-prices]
pod: null
---
Describe what this agent does, what it may decide alone and what a person must approve.
"""


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


def _well_typed(front: dict) -> bool:
    for field in _LISTS:
        value = front.get(field, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return False
    if not set(front.get("approval", [])) <= set(APPROVAL):
        return False
    return all(front.get(f) is None or isinstance(front[f], str) for f in ("model", "pod"))


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
        if not _well_typed(front):
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
    for n, raw in _profile().items():
        agent = _agent(n, "profile", raw)
        if n in agents:
            agent["approval"] = sorted({*agents[n]["approval"], *agent["approval"]})
        agents[n] = agent
    return agents


def describe(agent: dict) -> dict:
    role = next((ln.strip() for ln in agent["instructions"].splitlines() if ln.strip()), "")
    return {"name": agent["name"], "source": agent["source"], "role": role, **{f: agent[f] for f in FIELDS}}


def _literal_secrets(name: str, node: dict, path: str = "") -> list[str]:
    found = []
    for key, value in node.items():
        here = f"{path}{key}"
        if isinstance(value, dict):
            found += _literal_secrets(name, value, f"{here}.")
        elif isinstance(value, str) and SECRET_KEY.search(str(key)) and "${" not in value:
            found.append(f"{name}: {here} must use a ${{VAR}} reference, not a literal value")
    return found


def _unknown_model(model: str) -> bool:
    if model in C.PROFILES:
        return False
    listed = [harnesses.models(harnesses.account_harness(acct).name) for acct in C.ACCOUNTS]
    return all(listed) and not any(model in ids for ids in listed)


def validate(name: str, d: dict) -> list[str]:
    """Every problem in an agent definition (front matter dict) as '<name>: <problem>'."""
    known_skills = set(skills.builtin_names()) | {s["name"] for s in skills.scan()}
    problems = [f"{name}: unknown skill {s}" for s in d.get("skills") or [] if s not in known_skills]
    problems += [f"{name}: unknown connector role {r}" for r in d.get("connector_roles") or [] if r not in connectors.ROLES]
    if d.get("model") and _unknown_model(d["model"]):
        problems.append(f"{name}: unknown model or account {d['model']}")
    approval = d.get("approval") or []
    problems += [f"{name}: unknown approval action {x}" for x in approval if x not in APPROVAL]
    if name in _builtin():
        problems += [f"{name}: approval cannot drop {x} from built-in {name}"
                     for x in _builtin()[name].get("approval") or [] if x not in approval]
    front = {k: v for k, v in d.items() if k != "instructions"}
    return problems + _literal_secrets(name, front)


def problems_in_profile() -> list[str]:
    """Problems of every agent folder in the profile, including folders that fail to load."""
    root = C.CONFIG_DIR and C.CONFIG_DIR / "agents"
    found = []
    for folder in sorted(root.iterdir()) if root and root.is_dir() else []:
        try:
            front, _ = _parse((folder / "agent.md").read_text())
        except (OSError, ValueError) as e:
            found.append(f"{folder.name}: {e}")
            continue
        found += validate(folder.name, front)
    return found


def _fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _show(agent: dict, problems: list[str]) -> dict:
    return {**describe(agent), "instructions": agent["instructions"], "problems": problems}


def _problems_of(name: str, text: str) -> list[str]:
    try:
        front, body = _parse(text)
    except ValueError as e:
        return [f"{name}: {e}"]
    return validate(name, {**front, "instructions": body})


def _add(a) -> None:
    name = a.name
    if not C.NAME_RE.match(name):
        _fail(f"pl: invalid agent name {name}", 2)
    target = C.CONFIG_DIR / "agents" / name
    if target.exists():
        _fail(f"pl: agent {name} already exists")
    shipped = _builtin().get(name)
    text = TEMPLATE if shipped is None else _builtin_text(shipped)
    problems = _problems_of(name, text)
    if problems:
        _fail("\n".join(problems))
    target.mkdir(parents=True)
    fd, tmp = tempfile.mkstemp(dir=target, prefix=".agent-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, target / "agent.md")
    if a.json:
        print(json.dumps(_show(load()[name], [])))
    else:
        print(f"added {name}")


def _builtin_text(raw: dict) -> str:
    front = yaml.safe_dump({f: raw.get(f) for f in FIELDS}, sort_keys=False)
    return f"---\n{front}---\n{raw.get('instructions') or ''}\n"


def _remove(a) -> None:
    name = a.name
    root = (C.CONFIG_DIR / "agents").resolve()
    target = root / name
    if not C.NAME_RE.match(name) or target.parent != root or not target.is_dir():
        _fail("pl: built-in agents cannot be removed" if name in _builtin() else f"pl: no agent named {name}")
    if not a.yes:
        try:
            answer = util.ask("agents remove", f"remove agent {name}? [y/N] ")
        except EOFError:
            answer = ""
        except util.BadInput:
            _fail(f"pl: pass --yes to remove {name} without a prompt")
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return
    shutil.rmtree(target)
    print(f"removed {name}")
    if name in _builtin():
        print(f"{name} is the built-in again")


def cmd_agents(a) -> None:
    if a.agents_cmd == "add":
        return _add(a)
    if a.agents_cmd == "remove":
        return _remove(a)

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
    problems = _problems_of(a.name, _builtin_text(agent) if agent["source"] == "built-in" else
                            (C.CONFIG_DIR / "agents" / a.name / "agent.md").read_text())
    if a.json:
        print(json.dumps(_show(agent, problems)))
        return
    print(agent["instructions"])
    print(f"skills: {', '.join(agent['skills'])}")
    print(f"connector roles: {', '.join(agent['connector_roles'])}")
    print(f"model: {agent['model'] or 'stage default'}")
    print(f"approval: {', '.join(agent['approval'])}")
    print(f"pod: {agent['pod'] or ''}")
    if problems:
        print("problems:")
        for p in problems:
            print(f"  {p}")
