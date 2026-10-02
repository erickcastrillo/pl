"""Needs you: everything waiting on a person, grouped, with a detail pane."""
from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static

from pl import config as C
from pl.tui.chrome import header_text
from pl.tui.subagents import TODO_MARK
from pl.tui.review import ReviewScreen, checks_text, confirm_and_approve, load_review, size_text

PR_WHAT = {"decide": "needs your decision (auto-review stopped on purpose)", "merge": "ready to merge",
           "rework": "merge check found problems", "gate": "waiting for the merge check"}
# key, title, colour, hint
GROUPS = [("specs", "SPECS TO REVIEW", "#e0a040", "a approve · x send back · o answer questions · enter read"),
          ("plans", "PLANS TO REVIEW", "#e0a040", "a approve · x send back · enter read"),
          ("decide", "PRS STOPPED FOR YOUR CALL", "#e0a040", "auto-review stopped on purpose"),
          ("rework", "NEEDS REWORK", "red", "merge-gate found problems"),
          ("manual", "MANUAL · YOURS TO DO", "#5f9fff", "no agent runs these")]


def needs_groups(rows):
    """Rows per Needs-you group, plus "merge" (ready to merge, shown collapsed). Specs only when the spec gate is on."""
    g = {k: [] for k, *_ in GROUPS}
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
    return sum(len(g[k]) for k, *_ in GROUPS)


def row_id(r):
    return f"pr:{r['pr']['repo']}#{r['pr']['number']}" if r.get("pr") else r["card"]["id"]


def detail(r, now=None):
    """Plain text for the detail pane, from the row's own data (no board call)."""
    if r is None:
        return Text("select a row", style="dim")
    if r.get("pr"):
        pr = r["pr"]
        return Text("\n".join([pr["title"] or "", "", f"{pr['repo']}#{pr['number']}   {PR_WHAT.get(pr['state'], pr['state'])}",
                               pr.get("url") or ""]))
    c = r["card"]
    m = c.get("metadata") or {}
    lines = [c.get("title") or "", "", f"card     {c['id']}", f"column   {r.get('col')}",
             f"account  {m.get('profile') or '-'}", f"tags     {', '.join(map(str, c.get('tags') or [])) or '-'}",
             f"updated  {str(c.get('updated_at') or '')[:16].replace('T', ' ')}"]
    if now and now.get("line"):
        lines.append(f"now      {now['line']}")
    lines += [f"         {TODO_MARK.get(s, '[ ]')} {t}" for s, t in (now or {}).get("todos") or []]
    return Text("\n".join(lines))


KIND = {"specs": "spec", "plans": "plan"}


def review_kind(r):
    """spec or plan when the row waits for a review on Needs you, else None."""
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


class NeedsView(Horizontal):
    DEFAULT_CSS = """
    #needs-side { width: 1fr; height: 1fr; }
    #needs-body { height: 1fr; }
    """
    BINDINGS = [Binding("tab", "app.focus_next", "list / text"), Binding("o", "answer", "answer questions")]

    def __init__(self):
        super().__init__()
        self._rows, self._kinds, self._built = {}, {}, []
        self._bodies, self._pending = {}, {}   # key → (updated_at, text); read again only when the card changed
        self._now = {}   # card id -> {"line", "todos"} from the refresh
        self._shown, self._note = None, (None, "")   # what the pane shows; the "updated HH:MM" note

    def compose(self):
        t = DataTable(id="needs-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_columns("id", "title", "where", "now")
        yield t
        with Vertical(id="needs-side", classes="panel"):
            d = Static(Text(""), id="needs-detail")
            d.styles.height = "auto"     # the metadata on top, the spec or plan text scrolls below it
            yield d
            with VerticalScroll(id="needs-body"):
                yield Markdown("", open_links=False, id="needs-md")

    def show(self, data):
        rows = data["snapshot"]["rows"]
        self._now = data.get("now") or {}
        g = needs_groups(rows)
        t = self.query_one(DataTable)
        t.border_title = f"Needs you · {waiting_total(g)}"
        keep = t.cursor_row
        keep_key = self._current()[0]
        self._rows, self._kinds = {}, {}
        built = []
        for key, title, colour, hint in GROUPS:
            if not g[key]:
                continue
            built.append((f"group:{key}", (Text("■", style=colour), Text(f"{title} {len(g[key])}", style="bold"),
                                           Text(hint, style="dim"), Text(""))))
            for r in g[key]:
                rid = row_id(r)
                if rid in self._rows:   # a duplicate row would raise DuplicateKey
                    continue
                self._rows[rid], self._kinds[rid] = r, key
                if r.get("pr"):
                    cells = (f"{r['pr']['repo']}#{r['pr']['number']}", r["pr"]["title"] or "", r["pr"]["repo"], "")
                else:
                    cells = (str(r["card"]["id"])[:8], r["card"].get("title") or "", str((r["card"].get("tags") or [None])[0] or r.get("profile") or ""),
                             (self._now.get(r["card"]["id"]) or {}).get("line") or "")
                built.append((rid, (Text(cells[0], style="dim"), Text(cells[1]), Text(cells[2], style="dim"),
                                    Text(cells[3], style="red" if cells[3].startswith("error: ") else "dim"))))
        if g["merge"]:
            built.append(("group:merge", (Text("▸", style="green"), Text(f"{len(g['merge'])} PRs ready to merge"),
                                          Text("merge-gate passed · press 5", style="dim"), Text(""))))
        if not built:   # a heading row, so a / x / o find no spec or plan and only notify
            built.append(("group:empty", (Text("■", style="dim"), Text("Nothing needs you.", style="dim"), Text(""), Text(""))))
        old = self._built
        if [k for k, _ in built] == [k for k, _ in old]:   # same rows, same order: change only the cells that differ
            for (rid, cells), (_, before) in zip(built, old):
                for col, new, was in zip(t.columns, cells, before):
                    if new != was:
                        t.update_cell(rid, col, new)
        else:
            t.clear()
            for rid, cells in built:
                t.add_row(*cells, key=rid)
            if t.row_count:   # the same card stays selected when rows reorder, so a / x never act on another card
                idx = t.get_row_index(keep_key) if keep_key in self._rows else min(max(keep, 1), t.row_count - 1)   # row 0 is a heading
                t.move_cursor(row=idx)
        self._built = built
        self._bodies = {k: v for k, v in self._bodies.items() if k in self._rows}
        self._detail_for_cursor()

    def _current(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._rows.get(key)

    def _stamp(self, key):
        return str(self._rows[key]["card"].get("updated_at") or "")

    def _detail_for_cursor(self):
        key, r = self._current()
        kind = KIND.get(self._kinds.get(key))
        self.query_one("#needs-body").display = kind is not None
        if kind is None:
            self._paint(key, detail(r, self._now.get(((r or {}).get('card') or {}).get('id'))))
            return
        stamp = self._stamp(key)
        if self._bodies.get(key, (None,))[0] != stamp and self._pending.get(key) != stamp:
            self._pending[key] = stamp   # the old text stays on screen until the new one arrives
            self._load(kind, key, stamp)
        self._paint(key)

    def _load(self, kind, key, stamp):
        def run():   # one board read, off the UI thread
            try:
                c, text, checks = load_review(kind, key)
                hit = ("ok", text, checks, size_text(c.get("description")))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - RateLimited carries its own note
                hit = ("error", getattr(e, "note", None) or f"could not read the card: {e}")
            self.app.call_from_thread(self._loaded, stamp, key, hit)
        self.run_worker(run, thread=True, group="needs-body")

    def _loaded(self, stamp, key, hit):
        if self._pending.get(key) == stamp:
            del self._pending[key]
        if key not in self._rows or self._stamp(key) != stamp:   # the card changed again meanwhile
            return
        self._bodies[key] = (stamp, hit)
        if self._current()[0] == key:
            self._paint(key)

    def _paint(self, key, text=None):
        """Touch the pane only when what it would show differs from what it shows."""
        md = None
        if text is None:
            hit = self._bodies.get(key, (None, ("loading",)))[1]
            text = detail(self._rows[key], self._now.get(key))
            if hit[0] == "ok":
                text.append("\n")
                text.append_text(hit[3])
            text.append("\n\n")
            if hit[0] == "ok":
                text.append_text(checks_text(hit[2]))
            else:
                text.append("loading…" if hit[0] == "loading" else hit[1], style="dim" if hit[0] == "loading" else "red")
            md = hit[1] if hit[0] == "ok" else ""
        before, shown = self._shown, (key, md, text.plain)
        if shown == before:
            return
        self._shown = shown
        same = before is not None and before[0] == key
        new_md = md is not None and not (same and before[1] == md)
        if new_md and same and before[1]:   # real text replaced by newer text: say so, keep the reader's place
            self._note = (key, datetime.now().strftime("%H:%M"))
        if self._note[0] == key:
            text = Text("\n").join([Text(f"updated {self._note[1]}", style="dim"), text])
        self.query_one("#needs-detail", Static).update(text)
        if not new_md:
            return
        self.query_one("#needs-md", Markdown).update(md)   # the scroll offset is kept, clamped to the new height

    def review(self, what):
        """what: approve (confirm dialog), send_back or read (the review screen)."""
        key, r = self._current()
        group = self._kinds.get(key)
        start_review(self.app, what, r, KIND.get(group), [rid for rid, g in self._kinds.items() if g == group])

    def select_group(self, group):
        """Put the cursor on the first row of a group (specs, plans, ...); stay put when the group is empty."""
        t = self.query_one(DataTable)
        rid = next((k for k, g in self._kinds.items() if g == group), None)
        if rid is not None:
            t.move_cursor(row=t.get_row_index(rid))

    def action_answer(self):
        self.review("answer")

    def on_data_table_row_highlighted(self, _):
        try:
            self._detail_for_cursor()
        except Exception as e:  # noqa: BLE001 - a bad card must not kill the console
            self.app.error = f"render failed: {type(e).__name__}: {e}"
            self.app.query_one("#header", Static).update(header_text(self.app.data, self.app.error))

    def on_data_table_row_selected(self, _):
        self.review("read")
