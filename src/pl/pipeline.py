"""A profile's pipeline: its columns, stages and gates. No [pipeline] table means the engineering pipeline."""
import json
import re
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path

from pl import config as C

GATES = ("owner", "none", "auto-merge")
SECTION_RE = re.compile(r"^[A-Z][A-Z ]*$")
RESERVED_SECTIONS = ("INPUT", "REVIEW NOTES")
OUTWARD_REFUSAL = "a stage that writes or sends outward needs a gate"


def engineering() -> dict:
    """The built-in engineering pipeline, built from today's constants."""
    skill = {s: p.split()[0].lstrip("/") for s, p in C.BUILTIN_PROMPTS.items()}
    done = C.STAGE_DONE_AT
    return {"name": "engineering", "template": "engineering", "columns": list(C.COLUMNS), "stages": {
        "spec": {"column": "Inbox", "done": done["spec"], "skill": skill["spec"], "section": "SPEC",
                 "gate": "owner" if C.GATES.get("spec") else "none"},
        "design": {"column": "Spec ready", "done": "Spec ready", "skill": skill["design"], "section": "DESIGN",
                   "gate": "none"},
        "plan": {"column": "Spec ready", "done": done["plan"], "skill": skill["plan"], "section": "PLAN",
                 "gate": "owner"},
        "run": {"column": "Approved", "done": done["run"], "skill": skill["run"], "gate": "auto-merge"},
    }}


def from_toml(t) -> dict:
    if not t:
        return engineering()
    stages = {n: dict(s) for n, s in (t.get("stages") or {}).items() if isinstance(s, dict)}
    return {"name": t.get("name", "pipeline"), "template": t.get("template"), "columns": list(t.get("columns") or []),
            "stages": stages}


def errors(p, connectors=None) -> list[str]:
    """Problems in a pipeline, each starting with its key path."""
    cols, errs, owner = p["columns"], [], {}
    for name, s in p["stages"].items():
        at = f"pipeline.stages.{name}"
        for key in ("column", "done", "next"):
            if key in s and s[key] not in cols:
                errs.append(f"{at}.{key}: {s[key]!r} is not in pipeline.columns")
        if "gate" in s and s["gate"] not in GATES:
            errs.append(f"{at}.gate: must be one of {', '.join(GATES)}")
        sec = s.get("section")
        if sec is not None and not (isinstance(sec, str) and SECTION_RE.match(sec) and sec not in RESERVED_SECTIONS):
            errs.append(f"{at}.section: must be UPPER CASE words, and not {' or '.join(RESERVED_SECTIONS)}")
        col = s.get("column")
        if col in owner:
            errs.append(f"{at}.column: {col!r} is already the column of stage {owner[col]}")
        elif col is not None:
            owner[col] = name
        if s.get("gate") == "none" and _outward(s, connectors):
            errs.append(f"{at}.gate: {OUTWARD_REFUSAL}")
    return errs


def _outward(stage, connectors) -> bool:
    if stage.get("outward") is True:
        return True
    return any((connectors or {}).get(c, {}).get("access") == "read-write" for c in stage.get("connectors") or [])


def templates() -> dict:
    """The built-in pipeline templates by name."""
    found = {}
    for f in resources.files("pl").joinpath("pipelines_builtin").iterdir():
        if f.name.endswith(".toml"):
            p = from_toml(tomllib.loads(f.read_text())["pipeline"])
            found[p["name"]] = p
    return found


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def shown(p) -> dict:
    gates = {n: s["gate"] for n, s in p["stages"].items() if "gate" in s}
    return {"name": p["name"], "template": p["template"], "columns": p["columns"], "stages": p["stages"], "gates": gates}


def _other_profile(name) -> dict:
    r = _run([sys.executable, "-m", "pl", "--profile", name, "pipelines", "show", "--json"])
    if r.returncode:
        raise SystemExit(r.stderr.strip() or f"pl: could not read the pipeline of profile {name}")
    return json.loads(r.stdout)


def _print_show(d):
    print(f"pipeline {d['name']}" + (f" (template {d['template']})" if d["template"] else ""))
    print("columns: " + " → ".join(d["columns"]))
    for n, s in d["stages"].items():
        fields = [f"{k} {s[k]}" for k in ("skill", "account", "section", "gate", "next") if k in s]
        print(f"  {n}: {s.get('column')} → {s.get('done')}; " + ", ".join(fields))
    print("gates: " + (", ".join(f"{n} {g}" for n, g in d["gates"].items()) or "none"))


def _profile_pipeline_name(dir_) -> str:
    try:
        t = tomllib.loads((Path(dir_) / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        t = {}
    return (t.get("pipeline") or {}).get("name", "engineering")


def _list_rows() -> list[dict]:
    from pl import profiles, summary
    return [{"profile": r["name"], "pipeline": _profile_pipeline_name(r["dir"]),
             "summary": summary.read(Path(r["dir"]))} for r in profiles.list_profiles(check_lock=False)]


def cmd_pipelines(a):
    if a.pipelines_cmd == "show":
        d = _other_profile(a.name) if a.name and a.name != C.PROFILE_NAME else shown(C.PIPELINE)
        if a.json:
            print(json.dumps(d))
        else:
            _print_show(d)
    elif a.templates:
        rows = [{"name": n, "stages": len(p["stages"])} for n, p in sorted(templates().items())]
        if a.json:
            print(json.dumps(rows))
        else:
            print("\n".join(f"  {r['name']:<16}{r['stages']} stages" for r in rows))
    else:
        rows = _list_rows()
        if a.json:
            print(json.dumps(rows))
            return
        for r in rows:
            state = "running" if (r["summary"] or {}).get("running") else "stopped"
            print(f"  {r['profile']:<14}{r['pipeline']:<16}{state}")
