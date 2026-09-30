"""Board connectors. Every board read and write goes through a Tracker chosen by config.

Card dict shape (all connectors): id, title, description, tags, metadata, list_id (= column id),
updated_at (ISO 8601 with offset; compare with util.parse_iso), assigned_to.
"""
import importlib
from typing import Protocol

from pl import config as C

# type -> "module:Class".
TYPES = {"github-project": "pl.trackers.github:GitHubProject", "github-issues": "pl.trackers.github:GitHubIssues",
         "mcp": "pl.trackers.mcp:Mcp"}


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


def get(kind):
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
        for name in ("create", "update", "delete", "move", "adopt"):
            if hasattr(tracker, name):
                setattr(tracker, name, _marked(getattr(tracker, name)))
    _cache[kind] = (cfg, tracker)
    return tracker


def _marked(write):
    """A board write that stops every process of the profile reusing a board read from before it (board.cards)."""
    def run(*a, **kw):
        from pl import board
        board.dirty()
        try:
            return write(*a, **kw)
        finally:
            board.dirty()
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
