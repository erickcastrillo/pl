"""Pod configuration and active pod lookup."""
from pl import config as C


def current():
    """Active pod {name, file, knowledge, skills, plugins, budgets}."""
    name = getattr(C, "POD", None) or "engineering"
    pods = getattr(C, "PODS", {}) or {}
    if name in pods:
        p = dict(pods[name])
        p.setdefault("name", name)
        p.setdefault("skills", [])
        p.setdefault("plugins", [])
        p.setdefault("budgets", {"company": 2000, "pod": 1500, "client": 1500})
        return p
    return {
        "name": "engineering",
        "skills": ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"],
        "plugins": [],
        "budgets": {"company": 2000, "pod": 1500, "client": 1500},
    }
