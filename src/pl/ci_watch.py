"""An alert when the newest pl pull request has had no check run or status for over 30 minutes."""
import json
import time

from pl import alerts, board, rework
from pl import config as C
from pl.trackers import github
from pl.util import parse_iso

QUIET = 30 * 60
EVERY = 300
PREFIX = "ci_not_running:"
FIX = "check the repository's Actions minutes and workflow settings (Settings → Actions)"
_CACHE = {}
_clock = time.time


def _in_pr_open(card):
    try:
        return board.col_name(card["list_id"]) == C.STAGE_DONE_AT["run"]
    except SystemExit:   # no usable tracker: the board's columns are unknown
        return False


def _open_prs(all_cards):
    """[(card, repo, number, url)] of the PR open cards, the most recently updated card first."""
    out = []
    for c in sorted(all_cards, key=lambda c: parse_iso(c.get("updated_at") or ""), reverse=True):
        if (prs := rework._prs(c)) and _in_pr_open(c):
            out.append((c, *prs[-1]))
    return out


def _look(repo, number, url):
    """The head's push time and whether it has any check; None when the call could not be made."""
    hit = _CACHE.get(url)
    if hit and hit["checks"] and _clock() - hit["at"] < EVERY:
        return hit
    try:
        pr = json.loads(github._gh(["pr", "view", str(number), "--repo", repo, "--json", "headRefOid,commits,statusCheckRollup"]))
        entry = {"at": _clock(), "head": pr["headRefOid"], "pushed": parse_iso(pr["commits"][-1]["committedDate"]),
                 "checks": bool(pr["statusCheckRollup"])}
    except (github.RateLimited, SystemExit, ValueError, KeyError, IndexError, TypeError):
        return None
    _CACHE[url] = entry
    return entry


def check(all_cards, notify):
    prs = _open_prs(all_cards)
    if not prs or not C.AUTO_MERGE.get("require_ci", True):
        alerts.sweep(PREFIX, set(), notify)
        return
    _, repo, number, url = prs[0]
    seen = _look(repo, number, url)
    if seen is None:
        alerts.sweep(PREFIX, {a["key"] for a in alerts.listing() if a["key"].startswith(PREFIX)}, notify)
        return
    if seen["checks"] or _clock() - seen["pushed"] <= QUIET:
        alerts.sweep(PREFIX, set(), notify)
        return
    key, title = PREFIX + repo, f"CI is not running on {repo}"
    if why := alerts.open(key, "high", title, FIX, cards=[c["id"] for c, *_ in prs]):
        notify(alerts.headline(why, title), FIX)
    alerts.sweep(PREFIX, {key}, notify)
