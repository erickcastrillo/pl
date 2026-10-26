"""A profile's pipeline: its columns, stages and gates. No [pipeline] table means the engineering pipeline."""
import json
import re
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path

import tomlkit

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


def is_config() -> bool:
    """True for a profile that defines stages of its own; no [pipeline], or one without stages, is engineering."""
    p = C.PIPELINE or {}
    return bool(p.get("stages")) and p.get("template") != "engineering"


def stage_at(col) -> str | None:
    return next((n for n, s in C.PIPELINE["stages"].items() if s.get("column") == col), None)


def complete(col, stage) -> bool:
    cols = C.PIPELINE["columns"]
    return cols.index(col) >= cols.index(C.PIPELINE["stages"][stage]["done"])


def prompt(stage, card_id) -> str:
    if override := C.PROMPTS.get(stage):
        return override.format(id=card_id, worktrees_dir=C.WORKTREES_DIR or "")
    return f"/{C.PIPELINE['stages'][stage]['skill']} {card_id} stage={stage}"


def writable() -> tuple:
    """The sections stage agents write; INPUT and REVIEW NOTES are a person's."""
    if not is_config():
        return ("SPEC", "DESIGN", "PLAN")
    return tuple(dict.fromkeys(s["section"] for s in C.PIPELINE["stages"].values() if s.get("section")))


def past_gate(col, section) -> bool:
    """True when the card has left the owner gate of the stage that writes `section`."""
    cols = C.PIPELINE["columns"]
    return any(s.get("section") == section and s.get("gate") == "owner" and cols.index(col) > cols.index(s["done"])
               for s in C.PIPELINE["stages"].values())


def gate_for(col) -> tuple | None:
    """(name, stage) of the owner-gated stage that finishes in column `col`."""
    return next(((n, s) for n, s in C.PIPELINE["stages"].items() if s.get("gate") == "owner" and s.get("done") == col), None)


def approved_column(stage) -> str:
    """Where approval sends a card: the stage's `next`, else the column after `done`, else `done` itself."""
    if stage.get("next"):
        return stage["next"]
    cols = C.PIPELINE["columns"]
    i = cols.index(stage["done"])
    return cols[min(i + 1, len(cols) - 1)]


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


def _fail(msg):
    print(f"pl: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _tracker(board):
    if board == "local":
        return {"type": "local"}
    if board.startswith("mcp:") and board[4:]:
        return {"type": "mcp", "server": board[4:]}
    _fail(f"--board must be local or mcp:<server>, not {board!r}")


def _add(name, p, a):
    """Create the profile NAME with pipeline p; an existing profile is left as it is."""
    from pl import profiles
    if (Path.home() / f".pl-{name}").exists():
        print(f"profile {name} already exists, unchanged")
        return
    p = {k: v for k, v in {**p, "name": name}.items() if v is not None}
    chosen = getattr(a, "accounts", None)
    for n in chosen or ():
        if n not in C.ACCOUNTS:
            _fail(f"unknown account {n!r}")
    acc = {n: C.ACCOUNTS[n] for n in chosen} if chosen else C.ACCOUNTS
    account = a.account or (chosen[0] if chosen and len(chosen) == 1 else None)
    if account:
        if account not in C.ACCOUNTS:
            _fail(f"unknown account {account!r}")
        p["stages"] = {n: {"account": account, **s} for n, s in p["stages"].items()}
    tracker = getattr(a, "tracker", None) or _tracker(a.board)
    if a.dry_run:
        print(tomlkit.dumps(profiles.pipeline_config(p, tracker, acc)), end="")
    elif not (a.yes or sys.stdin.isatty()):
        _fail(f"pl pipelines {a.pipelines_cmd} would create profile {name}: pass --yes")
    else:
        profiles.new_profile(name, pipeline_doc=p, tracker_doc=tracker, accounts=acc)


def _pipeline_of(name) -> dict:
    try:
        t = tomllib.loads((Path.home() / f".pl-{name}" / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        _fail(f"no profile {name}")
    return from_toml(t.get("pipeline"))


def _import(a):
    try:
        t = tomllib.loads(Path(a.file).read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        _fail(f"{a.file}: {e}")
    if "pipeline" not in t:
        _fail(f"{a.file}: pipeline: missing [pipeline] table")
    p = from_toml(t["pipeline"])
    if errs := errors(p, t.get("connectors")):
        _fail("\n".join(errs))
    _add(a.name, p, a)


def cmd_pipelines(a):
    if a.pipelines_cmd == "add":
        if a.template not in templates():
            _fail(f"unknown template {a.template!r}: pl pipelines list --templates")
        _add(a.name, templates()[a.template], a)
    elif a.pipelines_cmd == "export":
        p = {k: v for k, v in _pipeline_of(a.name).items() if v is not None}
        print(tomlkit.dumps({"pipeline": p}), end="")
    elif a.pipelines_cmd == "import":
        _import(a)
    elif a.pipelines_cmd == "show":
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
