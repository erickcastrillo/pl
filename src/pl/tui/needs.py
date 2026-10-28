"""What waits on a person: the Needs-you groups, the attention test and the review helpers the Pipeline tab uses."""
from rich.text import Text

from pl import config as C
from pl import outbound, pipeline
from pl.util import cmd_id
from pl.tui.subagents import TODO_MARK
from pl.tui.review import ReviewScreen, confirm_and_approve

GROUPS = ("specs", "plans", "decide", "rework", "manual")   # the Needs-you groups; decide and rework hold PRs


def _plural(word):
    word = word.capitalize()
    if word[-1] == "y" and word[-2:-1].lower() not in "aeiou":
        return word[:-1] + "ies"
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


def _send_stages():
    """The owner-gated outward stages that write a section, in stage order."""
    if not pipeline.is_config():
        return []
    return [s for s in C.PIPELINE["stages"].values() if s.get("gate") == "owner" and s.get("outward") is True and s.get("section")]


def send_groups():
    """(group key, title) per section an outward stage writes; the first stage to write a section names its group."""
    found = {}
    for s in _send_stages():
        found.setdefault(f"send:{s['section']}", str(s.get("review_group") or f"{_plural(s['section'])} to send"))
    return list(found.items())


def group_keys():
    return [k for k, _ in send_groups()] if pipeline.is_config() else list(GROUPS)


def needs_groups(rows):
    """Rows per Needs-you group, plus "merge" (ready to merge, shown collapsed). Specs only when the spec gate is on.
    A configured pipeline adds a "send:<SECTION>" group per outward stage, holding the cards in its done column."""
    g = {k: [] for k, _ in send_groups()}
    send_at = {s["done"]: f"send:{s['section']}" for s in reversed(_send_stages())}
    g.update({k: [] for k in GROUPS})
    g["merge"] = []
    for r in rows:
        if r.get("card") and r.get("col") in send_at:
            g[send_at[r["col"]]].append(r)
            continue
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
    return sum(len(g[k]) for k in group_keys())


def needs_you(data):
    """The one rule for "needs you": rows at a gate, Manual cards, PRs to decide or merge, and cards whose agent needs
    you, died or is blocked, each once. Rework PRs are not counted: the run agent fixes them."""
    out = []
    for r in ((data or {}).get("snapshot") or {}).get("rows") or []:   # none before the first board read
        if r.get("pr"):
            if r["pr"]["state"] in ("decide", "merge"):
                out.append(r)
        elif needs_me(r):
            out.append(r)
    return out


def needs_you_count(data):
    return len(needs_you(data))


CARD_GROUPS = ("specs", "plans", "manual")   # the groups that hold cards; the rest hold PRs


def needs_me(r):
    """True for a card row that waits on a person: a spec or plan to review, a Manual card, an agent that needs
    you or died (kind "needs" covers both), or a run held back after its waiting agent was released (blocked)."""
    if not r.get("card"):
        return False
    g = needs_groups([r])
    return any(g[k] for k in (*CARD_GROUPS, *(k for k, _ in send_groups()))) or r.get("kind") == "needs" or bool(r.get("failed")) or bool(r.get("blocked"))


def detail(r, now=None):
    """Plain text for a card's detail pane, from the row's own data (no board call)."""
    if r is None:
        return Text("select a row", style="dim")
    c = r["card"]
    m = c.get("metadata") or {}
    lines = [c.get("title") or "", "", f"{pipeline.ui()['item']:<8} {c['id']}", f"column   {r.get('col')}",
             f"account  {m.get('profile') or '-'}", f"tags     {', '.join(map(str, c.get('tags') or [])) or '-'}",
             f"updated  {str(c.get('updated_at') or '')[:16].replace('T', ' ')}"]
    if m.get("dropped_at"):   # card text: shown as plain text, never markup
        lines.append(f"dropped  {str(m['dropped_at'])[:16].replace('T', ' ')} from {m.get('dropped_from') or '-'}: "
                     f"{m.get('drop_reason') or 'no reason given'}")
    if r.get("failed"):   # the agent died too often: why (screen text, plain) and what to do
        w, err = r.get("worker") or {}, m.get("agent_error") or {}
        lines.append(f"died     {w.get('stage')} agent died {int(w.get('attempts') or 0)}\u00d7"
                     + (f"; last error: {err.get('line')}" if err.get("line") and err.get("stage") == w.get("stage") else ""))
        lines.append(f"         press t or run pl retry {cmd_id(c['id'])}")
    if now and now.get("line"):
        lines.append(f"now      {now['line']}")
    lines += [f"         {TODO_MARK.get(s, '[ ]')} {t}" for s, t in (now or {}).get("todos") or []]
    if key := _send_key(r):
        draft = outbound.parse_reply(c, key.removeprefix("send:"))
        lines += ["", *([f"to       {draft['to']}"] if draft["to"] else []), f"subject  {draft['subject']}", "", draft["body"]]
    return Text("\n".join(lines))


KIND = {"specs": "spec", "plans": "plan"}


def _send_key(r):
    g = needs_groups([r])
    return next((k for k, _ in send_groups() if g[k]), None)


def review_kind(r):
    """spec or plan when the row waits for a review, send for a draft to send, else None."""
    g = needs_groups([r])
    return next((KIND[k] for k in KIND if g[k]), "send" if _send_key(r) else None)


def start_review(app, what, r, kind, ids):
    """approve (confirm dialog), send_back, answer or read (the review screen) on a spec or plan row; else a notice."""
    if r is None or kind is None or (what == "answer" and kind != "spec"):
        app.notify("select a spec to answer its open questions" if what == "answer" else "select a spec or a plan to review")
        return
    c = r["card"]
    if kind == "send":
        if what != "approve":
            app.notify(f'use pl reject {cmd_id(c["id"])} "notes" to send it back')
            return
        outside = [e.removeprefix("outside: ") for e in outbound.flagged_recipients(c, _send_key(r).removeprefix("send:"))]
        confirm_and_approve(app, c["id"], str(c.get("title") or ""), kind, outside=outside)
    elif what == "approve":
        confirm_and_approve(app, c["id"], str(c.get("title") or ""), kind)
    else:
        app.push_screen(ReviewScreen(kind, c["id"], ids=ids, notes_first=what == "send_back", answers_first=what == "answer"))
