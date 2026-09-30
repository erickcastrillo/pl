"""pl standup: a short plain-text summary of a time window (default the last 24 hours) to paste in chat.

Counts come from the event log, the board snapshot and two GitHub PR searches. Titles only: never card bodies."""
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone

from pl import config as C
from pl import events
from pl.trackers import github
from pl.util import parse_iso

IN_PROGRESS = ["Spec ready", "Plan for review", "Approved", "In progress", "PR open"]
BULLETS, TITLE = 5, 70
SPAN_RE = re.compile(r"(\d+)([mhd])")
UNIT = {"m": "minutes", "h": "hours", "d": "days"}
# Pipeline counts: label, event test
PIPELINE = [("ideas added", lambda e: e["kind"] == "idea_approved"),
            ("specs written", lambda e: e["kind"] == "moved" and e.get("to") == "Spec ready"),
            ("specs approved", lambda e: e["kind"] == "spec_approved"),
            ("plans written", lambda e: e["kind"] == "moved" and e.get("to") == "Plan for review"),
            ("plans approved", lambda e: e["kind"] == "approved"),
            ("sent back", lambda e: e["kind"] in ("rejected", "spec_rejected")),
            ("cards done", lambda e: e["kind"] == "moved" and e.get("to") == "Done")]


def parse_since(text, now=None):
    """24h / 90m / 7d back from now, or a local YYYY-MM-DD[THH:MM]. None means 24h."""
    now = now or datetime.now(timezone.utc)
    text = (text or "24h").strip()
    if m := SPAN_RE.fullmatch(text):
        return now - timedelta(**{UNIT[m[2]]: int(m[1])})
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", text):
        try:
            return datetime.fromisoformat(text).astimezone()
        except ValueError:
            pass
    raise SystemExit(f"pl standup: --since takes 24h, 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM, not {text!r}")


def _scope():
    """The search qualifiers for the profile's PRs: repo:... per [code_host] repos, else user:<owner>; None when unset."""
    ch = C.CODE_HOST or {}
    owner, repos = ch.get("owner"), ch.get("repos") or []
    if repos:
        return " ".join(f"repo:{r if '/' in r else f'{owner}/{r}'}" for r in repos if '/' in r or owner)
    return f"user:{owner}" if owner else None


def _pr(p, me):
    return {"repo": (p.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1] or "?", "number": p.get("number"),
            "title": p.get("title") or "",
            "yours": me in [(p.get("user") or {}).get("login")] + [a.get("login") for a in p.get("assignees") or []]}


def pr_summary(start):
    """Every PR in the profile's repos since `start`: per kind (merged, opened, closed unmerged) GitHub's total and
    up to 100 PRs, and how many merged ones are yours (authored by or assigned to you). Three searches plus one
    `gh api user`. A short reason string when GitHub cannot answer."""
    from pl import watch
    scope = _scope()
    if not scope:
        return "set [code_host] owner to count the team's PRs"
    since = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        me = (watch._gh(["api", "user"]) or {}).get("login")
        out = {}
        for kind, q in (("merged", f"merged:>={since}"), ("opened", f"created:>={since}"),
                        ("closed", f"is:unmerged closed:>={since}")):
            got = watch._gh(["api", "-X", "GET", "search/issues", "-f", f"q=is:pr {scope} {q}", "-f", "per_page=100",
                             "-f", "sort=updated"])
            items = [_pr(p, me) for p in got.get("items") or []]
            out[kind] = {"total": int(got.get("total_count", len(items))), "items": items}
    except subprocess.TimeoutExpired:
        return "gh timed out"
    except FileNotFoundError:
        return "gh is not installed"
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return github.TOKEN_RE.sub(r"\1***", " ".join(str(e).split()))[:120] or "gh failed"
    out["merged"]["yours"] = sum(p["yours"] for p in out["merged"]["items"])   # counted from the first 100
    return out


def _cut(s):
    s = " ".join(str(s or "").split())
    return s if len(s) <= TITLE else s[:TITLE - 1].rstrip() + "…"


def _bullets(items):
    return [f"- {_cut(x)}" for x in items[:BULLETS]]


def text(snapshot, start, prs, now=None, cards=None, col=None, markdown=False):
    """The standup text. prs: pr_summary()'s dict or reason. cards: every board card (titles, and Done when the log
    has no moves to Done); col(card) gives a card's column name."""
    now = now or datetime.now(timezone.utc)
    rows = snapshot.get("rows") or []
    titles = {c["id"]: c.get("title") for c in cards or () if c.get("id")}
    titles.update({r["card"]["id"]: r["card"].get("title") for r in rows if r.get("card")})
    evs = sorted((e for e in events._read() if start < e["_t"] <= now), key=lambda e: e["_t"], reverse=True)
    head = (lambda s: f"**{s}:**") if markdown else (lambda s: f"{s}:")
    local = start.astimezone().strftime("%Y-%m-%d %H:%M")
    out = [("### " if markdown else "") + f"Standup for {C.PROFILE_NAME or 'pl'}, since {local}", ""]

    if isinstance(prs, str):
        out.append(f"{head('PRs')} unavailable ({prs})")
    else:
        m = prs["merged"]
        out.append(f"{head('PRs')} {m['total']} merged ({m.get('yours', 0)} yours), {prs['opened']['total']} opened, "
                   f"{prs['closed']['total']} closed without merging")
        every = [p for k in ("merged", "opened", "closed") for p in prs[k]["items"]]
        out += _bullets([f"{p['repo']}#{p['number']} {p['title']}" for p in sorted(every, key=lambda p: not p.get("yours"))])

    hits = {label: [e.get("card") for e in evs if test(e)] for label, test in PIPELINE}
    counts = {label: len(h) for label, h in hits.items()}
    touched = [cid for label, *_ in reversed(PIPELINE) for cid in hits[label]]   # bullets: cards done first
    if not counts["cards done"] and cards and col:   # no moves to Done logged: the board's Done column updated in the window
        done = [c for c in cards if col(c) == "Done" and start.timestamp() < parse_iso(c.get("updated_at") or "") <= now.timestamp()]
        counts["cards done"] = len(done)
        touched = [c["id"] for c in done] + touched
    out.append(head("Pipeline") + " " + ", ".join(f"{n} {label}" for label, n in counts.items()))
    seen = []
    for cid in touched:
        if cid and cid not in seen:
            seen.append(cid)
    out += _bullets([titles.get(cid) or str(cid)[:8] for cid in seen])

    per = Counter(r.get("col") for r in rows if r.get("card"))
    working = [r["card"].get("title") for r in rows if r.get("card") and r.get("kind") == "working"]
    waiting = (snapshot.get("needs") or {}).get("attention", 0)
    out.append(head("In progress now") + " " + ", ".join(f"{c} {per.get(c, 0)}" for c in IN_PROGRESS)
               + f"; {len(working)} agents running, {waiting} waiting on you")
    out += _bullets(working)

    from pl.tui.needs import GROUPS, needs_groups
    g = needs_groups(rows)
    need = [r for k, *_ in GROUPS for r in g[k]]
    out.append(f"{head('Needs you')} {len(need)}")
    out += _bullets([(r.get("pr") or r.get("card") or {}).get("title") for r in need][:3])

    errors = [e for e in evs if e["kind"] == "error"]
    if errors:
        top = Counter(_cut(e.get("message") or "error")[:60].rstrip() for e in errors).most_common(3)
        out.append(f"{head('Problems')} {len(errors)} errors")
        out += [f"- {n}x {m}" for m, n in top]
    return "\n".join(out)


def _cards():
    from pl import board
    board.share("read")   # reuse the dispatcher's board read while it is young
    return board.cards()


def cmd_standup(a):
    from pl import board, watch
    start = parse_since(a.since)
    snap = watch.watch_snapshot()
    print(text(snap, start, pr_summary(start), cards=_cards(), col=lambda c: board.col_name(c.get("list_id")),
               markdown=a.markdown))
