"""A profile's pipeline: its columns, stages and gates. No [pipeline] table means the engineering pipeline."""
import re

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
