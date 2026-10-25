"""Board connectors. Every board read and write goes through a Tracker chosen by config.

Card dict shape (all connectors): id, title, description, tags, metadata, list_id (= column id),
updated_at (ISO 8601 with offset; compare with util.parse_iso), assigned_to.
"""
import importlib
from typing import Protocol

from pl import config as C

# type -> "module:Class".
TYPES = {"github-project": "pl.trackers.github:GitHubProject", "github-issues": "pl.trackers.github:GitHubIssues",
         "mcp": "pl.trackers.mcp:Mcp", "local": "pl.trackers.local:Local"}


class Tracker(Protocol):
    def columns(self) -> dict[str, str]: ...
    def cards(self, query: dict | None = None) -> list[dict]:
        """Connectors may ignore query keys they do not understand; callers must still filter client-side."""
        ...
    def card(self, item_id) -> dict: ...
    def create(self, column: str, *, title, description="", tags=(), metadata=None, assigned_to=None) -> dict: ...
    def update(self, item_id, *, verify: bool = True, **fields) -> dict:
        """verify=False sends fields as given (caller merges metadata): no pre-read, no persistence check."""
        ...
    def delete(self, item_id) -> None: ...
    def ensure_column(self, name, *, description="") -> str: ...
    def url(self, item_id) -> str: ...
    def test(self) -> str: ...


# kind -> (the config dict it was built from, tracker). config.load builds new dicts, so an identity
# check on the dict resets the cache on every load without config importing this module.
_cache = {}


_dry = False


def set_dry_run(on=True):
    global _dry
    _dry = on


def dry_run():
    return _dry


class DryRun:
    """Wraps a tracker while --dry-run is on: reads pass through, writes print what they would do."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in ("move", "adopt"):   # only some connectors have them; callers test with hasattr
            def dry(ref, *a, **kw):
                self._would(name, ref, {"column": a[0] if a else kw.get("column")})
                return ref
            return dry
        return attr

    def _would(self, verb, item_id, fields):
        from pl import util
        shown = ", ".join(f"{k}={_cut(v)!r}" for k, v in fields.items() if k != "verify")
        print(util.mask(f"would {verb} {item_id}: {shown}"))

    def create(self, column, **fields):
        self._would("create", "card", {"column": column, **fields})
        return {"id": "dry-run", "title": fields.get("title", ""), "description": fields.get("description", ""),
                "tags": list(fields.get("tags", ())), "metadata": fields.get("metadata") or {}, "list_id": column}

    def update(self, item_id, *, verify=True, **fields):
        self._would("update", item_id, fields)
        return {**self._inner.card(item_id), **fields}

    def delete(self, item_id):
        self._would("delete", item_id, {})

    def ensure_column(self, name, *, description=""):
        self._would("ensure column", name, {})
        return name


def _cut(v):
    return v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v


def get(kind):
    tracker = _build(kind)
    return DryRun(tracker) if _dry else tracker


def _build(kind):
    """The tracker for "tracker" (the pipeline board) or "intake" (the idea board), built from the [tracker] / [intake] settings."""
    if kind not in ("tracker", "intake"):
        raise SystemExit(f"pl: unknown tracker kind {kind!r}")
    cfg = C.TRACKER if kind == "tracker" else C.INTAKE
    hit = _cache.get(kind)
    if hit and hit[0] is cfg:
        return hit[1]
    t = cfg.get("type")
    if not t:
        raise SystemExit(f"pl: set [{kind}] type in {C.path() or 'config.toml'} (one of: {', '.join(sorted(TYPES))})")
    if t not in TYPES:
        raise SystemExit(f"pl: unknown {kind} type {t!r} (valid: {', '.join(sorted(TYPES))})")
    mod, cls = TYPES[t].split(":")
    tracker = getattr(importlib.import_module(mod), cls)(cfg)
    if kind == "tracker":
        if t == "local" and C.SYNC.get("github"):
            for name in ("create", "update", "delete"):
                if hasattr(tracker, name):
                    setattr(tracker, name, _queued(name, getattr(tracker, name)))
        for name in ("create", "update", "delete", "move", "adopt"):
            if hasattr(tracker, name):
                setattr(tracker, name, _marked(getattr(tracker, name), name))
    _cache[kind] = (cfg, tracker)
    return tracker


def _marked(write, name=None):
    """A board write that stops every process of the profile reusing a board read from before it (board.cards)."""
    def run(*a, **kw):
        from pl import audit, board
        board.dirty()
        try:
            res = write(*a, **kw)
            action_name = name or getattr(write, "__name__", "write")
            card_id = res.get("id") if isinstance(res, dict) and "id" in res else (a[0] if a else kw.get("item_id") or kw.get("id") or kw.get("card_id"))
            fields = sorted(k for k in kw.keys() if k != "verify")
            if action_name == "move" and "column" not in fields:
                fields.append("column")
            audit.append(f"card.{action_name}", card=card_id, detail=fields)
            return res
        finally:
            board.dirty()
    return run


def _queued(op, write):
    def run(*a, **kw):
        from pl import outbox
        res = write(*a, **kw)
        try:
            if op == "create":
                fields = {
                    "column": a[0] if len(a) > 0 else kw.get("column"),
                    "title": a[1] if len(a) > 1 else kw.get("title"),
                    "description": a[2] if len(a) > 2 else kw.get("description", ""),
                    "tags": a[3] if len(a) > 3 else kw.get("tags", ()),
                    "metadata": a[4] if len(a) > 4 else kw.get("metadata"),
                    "assigned_to": a[5] if len(a) > 5 else kw.get("assigned_to"),
                }
                outbox.append(res["id"], "create", fields)
            elif op == "update":
                item_id = a[0] if len(a) > 0 else kw["item_id"]
                fields = {k: v for k, v in kw.items() if k != "verify"}
                outbox.append(item_id, "update", fields)
            elif op == "delete":
                item_id = a[0] if len(a) > 0 else kw["item_id"]
                outbox.append(item_id, "delete", {})
        except OSError as e:
            raise SystemExit(f"pl: outbox: {e}")
        return res
    return run


def me(tracker):
    """Who "assigned to me" means on this tracker. GitHub takes a login, never an email: [user] login, else "@me"
    (gh resolves it to the account signed in for the profile's GH_CONFIG_DIR). Other trackers keep the email."""
    from pl.trackers.github import _GitHub
    if isinstance(tracker, _GitHub):
        return C.USER_LOGIN or "@me"
    return C.USER_EMAIL


def reset(kind=None):
    """Drop the cached tracker for kind, or every tracker and what the GitHub connector remembers about projects."""
    if kind is None:
        _cache.clear()
        from pl.trackers import github
        github.forget()
    else:
        _cache.pop(kind, None)
