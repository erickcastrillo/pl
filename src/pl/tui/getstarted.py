"""Home's "Get started" checklist: six steps from an empty profile to a first pull request, ticked from profile state."""
import json
import time

from rich.text import Text

from pl import config as C
from pl import summary
from pl.board import _save

ITEMS = [
    ("board", "board connected", "B", "settings"),
    ("accounts", "an account for each stage", "C", "settings"),
    ("idea", "a first idea added", "W", "ideas"),
    ("spec", "its spec approved", "V", "cards"),
    ("plan", "its plan approved", "V", "cards"),
    ("pr", "its first PR opened", "Y", "prs"),
]


def _path():
    return C.STATE_DIR / "getstarted.json"


def marks() -> dict:
    try:
        got = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def mark(**kv):
    _save(_path(), {**marks(), **kv})


def _after(col, cols, at=False) -> bool:
    """A card sits in a column after `col` (or at it, with at=True). Manual needs a person, so it never counts."""
    if col not in C.COLUMNS:
        return False
    idx = C.COLUMNS.index(col)
    return any(c in C.COLUMNS and c != "Manual" and (C.COLUMNS.index(c) > idx or (at and C.COLUMNS.index(c) == idx))
               for c in cols)


def _accounts_ready() -> bool:
    stages = {**C.STAGES, **(C.PIPELINE or {}).get("stages", {})}
    named = [s["account"] for s in stages.values() if isinstance(s, dict) and s.get("account")]
    return bool(C.ACCOUNTS) and all(a in C.ACCOUNTS or C.is_local_account(a) for a in named)


def _cards(data) -> list[dict]:
    snap = (data or {}).get("snapshot") or {}
    return [r for r in [*snap.get("rows", []), *snap.get("done", [])] if r.get("card")]


def ticks(data) -> dict[str, bool]:
    cards = _cards(data)
    cols = {r.get("col") for r in cards}
    return {
        "board": bool((C.TRACKER or {}).get("type")),
        "accounts": _accounts_ready(),
        "idea": bool(cards),
        "spec": _after(C.STAGE_DONE_AT["spec"], cols),
        "plan": _after(C.STAGE_DONE_AT["plan"], cols),
        "pr": _after(C.STAGE_DONE_AT["run"], cols, at=True) or any(r.get("pr") for r in cards),
    }


def active(data) -> bool:
    """Whether the checklist shows now. It starts only for a profile with no cards, then stays until done or dismissed."""
    m = marks()
    if m.get("dismissed") or m.get("done"):
        return False
    t = ticks(data)
    if not m.get("started"):
        if _cards(data):
            return False
        mark(started=time.time())
    if all(t.values()):
        mark(done=time.time())
        return False
    return True


def wanted() -> bool:
    """Whether Home's tab should exist before the first refresh has run."""
    m = marks()
    if m.get("dismissed") or m.get("done"):
        return False
    if m.get("started"):
        return True
    s = summary.read_file(C.STATE_DIR / "summary.json")
    if s is None:
        return True
    cols = s.get("columns") or []
    counts = [c.get("cards", 0) for c in cols if isinstance(c, dict)] if isinstance(cols, list) else list(cols.values())
    return sum(n for n in counts if isinstance(n, int)) == 0


def text(t, collapsed) -> Text:
    heading = f"Get started ({sum(t.values())}/{len(ITEMS)})"
    if collapsed:
        return Text.assemble((heading, "bold"), ("   G expand · Z dismiss", "dim"))
    out = Text(heading, style="bold")
    for item_id, label, key, _ in ITEMS:
        out.append("\n")
        out.append(f"✓ {label}", style="dim") if t[item_id] else out.append(f"○ {label}   press {key}", style="bold")
    out.append("\nG collapse · Z dismiss", style="dim")
    return out
