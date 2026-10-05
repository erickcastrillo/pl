"""What waits on a person: the Needs-you groups, the attention test and the review helpers the Pipeline tab uses."""
from rich.text import Text

from pl import config as C
from pl.tui.subagents import TODO_MARK
from pl.tui.review import ReviewScreen, confirm_and_approve

GROUPS = ("specs", "plans", "decide", "rework", "manual")   # the Needs-you groups; decide and rework hold PRs


def needs_groups(rows):
    """Rows per Needs-you group, plus "merge" (ready to merge, shown collapsed). Specs only when the spec gate is on."""
    g = {k: [] for k in GROUPS}
    g["merge"] = []
    for r in rows:
        if r.get("pr"):
            if r["pr"]["state"] in g:
                g[r["pr"]["state"]].append(r)
        elif r.get("card"):
            auto = (r["card"].get("metadata") or {}).get("pipeline_mode") == "auto"
            if r.get("col") == "Spec ready" and auto and (C.GATES or {}).get("spec"):
                if not (r["card"].get("metadata") or {}).get("spec_approved_at"):   # approved: the planner has it
                    g["specs"].append(r)
            elif r.get("kind") == "review":
                g["plans"].append(r)
            elif r.get("col") == "Manual":
                g["manual"].append(r)
    return g


def waiting_total(g):
    return sum(len(g[k]) for k in GROUPS)


CARD_GROUPS = ("specs", "plans", "manual")   # the groups that hold cards; the rest hold PRs


def needs_me(r):
    """True for a card row that waits on a person: a spec or plan to review, a Manual card, an agent that needs
    you or died (kind "needs" covers both), or a run held back after its waiting agent was released (blocked)."""
    if not r.get("card"):
        return False
    g = needs_groups([r])
    return any(g[k] for k in CARD_GROUPS) or r.get("kind") == "needs" or bool(r.get("failed")) or bool(r.get("blocked"))


def detail(r, now=None):
    """Plain text for a card's detail pane, from the row's own data (no board call)."""
    if r is None:
        return Text("select a row", style="dim")
    c = r["card"]
    m = c.get("metadata") or {}
    lines = [c.get("title") or "", "", f"card     {c['id']}", f"column   {r.get('col')}",
             f"account  {m.get('profile') or '-'}", f"tags     {', '.join(map(str, c.get('tags') or [])) or '-'}",
             f"updated  {str(c.get('updated_at') or '')[:16].replace('T', ' ')}"]
    if m.get("dropped_at"):   # card text: shown as plain text, never markup
        lines.append(f"dropped  {str(m['dropped_at'])[:16].replace('T', ' ')} from {m.get('dropped_from') or '-'}: "
                     f"{m.get('drop_reason') or 'no reason given'}")
    if now and now.get("line"):
        lines.append(f"now      {now['line']}")
    lines += [f"         {TODO_MARK.get(s, '[ ]')} {t}" for s, t in (now or {}).get("todos") or []]
    return Text("\n".join(lines))


KIND = {"specs": "spec", "plans": "plan"}


def review_kind(r):
    """spec or plan when the row waits for a review, else None."""
    g = needs_groups([r])
    return next((KIND[k] for k in KIND if g[k]), None)


def start_review(app, what, r, kind, ids):
    """approve (confirm dialog), send_back, answer or read (the review screen) on a spec or plan row; else a notice."""
    if r is None or kind is None or (what == "answer" and kind != "spec"):
        app.notify("select a spec to answer its open questions" if what == "answer" else "select a spec or a plan to review")
        return
    c = r["card"]
    if what == "approve":
        confirm_and_approve(app, c["id"], str(c.get("title") or ""), kind)
    else:
        app.push_screen(ReviewScreen(kind, c["id"], ids=ids, notes_first=what == "send_back", answers_first=what == "answer"))
