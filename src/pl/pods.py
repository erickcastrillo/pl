"""Pods: a pipeline's rules (pod.md), knowledge, skills, plugins and layer budgets."""
import json
import sys
from pathlib import Path

from pl import config as C

BUILTIN_DIR = Path(__file__).parent / "pods_builtin"
BUDGETS = {"knowledge": 2000, "pod": 1500, "client": 1500}   # tokens; the role and card layers are uncut
ENGINEERING_SKILLS = ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]


def _resolve(path):
    p = Path(path).expanduser()
    return p if p.is_absolute() or C.CONFIG_DIR is None else C.CONFIG_DIR / p


def _builtin():
    return {"name": "engineering", "file": BUILTIN_DIR / "engineering" / "pod.md", "knowledge": [],
            "skills": list(ENGINEERING_SKILLS), "plugins": [], "budgets": dict(BUDGETS)}


def _configured(name):
    v = C.PODS[name]
    return {"name": name, "file": _resolve(v["file"]) if v.get("file") else None,
            "knowledge": [_resolve(k) for k in v.get("knowledge", [])], "skills": list(v.get("skills", [])),
            "plugins": [{**p, "hooks": p.get("hooks", True)} for p in v.get("plugins", [])],
            "budgets": {**BUDGETS, **v.get("budgets", {})}}


def find(name):
    if name in C.PODS:
        return _configured(name)
    return _builtin() if name == "engineering" else None


def current():
    """The pipeline's pod: [pipeline] pod, else the profile's name, else the built-in engineering pod."""
    pod = find(C.POD or C.PROFILE_NAME)
    if pod is None and C.POD:
        raise SystemExit(f"pl: [pipeline] pod names {C.POD!r}, which is not in [pods]")
    return pod or _builtin()


def names():
    return sorted({"engineering", *C.PODS})


def rules(pod):
    if not pod["file"] or not pod["file"].is_file():
        return []
    return [ln.strip()[2:].strip() for ln in pod["file"].read_text().splitlines() if ln.strip()[:2] in ("- ", "* ")]


def describe(pod):
    return {"name": pod["name"], "file": str(pod["file"]) if pod["file"] else None, "rules": rules(pod),
            "knowledge": [str(k) for k in pod["knowledge"]], "skills": pod["skills"], "plugins": pod["plugins"],
            "budgets": pod["budgets"], "version": 1}


def cmd_pods(a):
    """pl pods list [--json] | show NAME [--json]."""
    if a.pods_cmd == "list":
        print(json.dumps(names()) if a.json else "\n".join(names()))
        return 0
    pod = find(a.name)
    if pod is None:
        print(f"pl: no pod {a.name!r}; pods: {', '.join(names())}", file=sys.stderr)
        raise SystemExit(2)
    d = describe(pod)
    if a.json:
        print(json.dumps(d, indent=2))
        return 0
    print(f"{d['name']}  (version {d['version']})  {d['file'] or 'no pod.md'}")
    print("rules:\n" + "\n".join(f"  - {r}" for r in d["rules"]))
    for k in ("knowledge", "skills", "plugins"):
        print(f"{k}: " + (", ".join(x["name"] if isinstance(x, dict) else x for x in d[k]) or "none"))
    print("budgets: " + ", ".join(f"{k} {v}" for k, v in d["budgets"].items()))
    return 0
