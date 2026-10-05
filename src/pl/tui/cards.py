"""Pipeline tab: every card as a list grouped by board column (Done hidden; z shows it), its full text beside it, and
actions. Cards that need a person carry a red ! and n shows only those."""
import shutil

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Markdown, OptionList, Static

from pl import config as C
from pl import move_agent, trackers
from pl.agents import jump_to_window
from pl.board import card, sections
from pl.commands import adopt, done, drop, hold, move_to, restart, retry, undrop, unhold
from pl.tui.chrome import show_first_heading, skip_headings
from pl.tui.loops import COPIERS, _copy_run
from pl.tui.needs import detail, needs_groups, needs_me, review_kind, start_review
from pl.tui.review import ConfirmScreen, open_editor, review_file, safe_link, write_section
from pl.tui.subagents import TODO_MARK
from pl.util import short_id

TITLE_WIDTH = 34   # two less than before: the state column starts with the ! marker


STATE = {"review": ("your review", "#e0a040"), "manual": ("yours to do", "#5f9fff"), "working": ("working", "green"),
         "needs": ("needs you", "red"), "waiting": ("in review", "dim"), "queued": ("queued", "green")}


def card_state(r):
    if r["kind"] in ("review", "working", "needs"):
        return r["kind"]
    if r.get("col") == "Manual":
        return "manual"
    return "waiting" if r.get("col") == "PR open" else "queued"


def open_card_url(app, cid):
    """Open the card's web link in the browser, off the UI thread (building a tracker may touch the network)."""
    def run():
        url = trackers.get("tracker").url(cid)
        if safe_link(url):   # a board-supplied file: or javascript: link is never opened
            app.call_from_thread(app.open_url, url)
        else:
            app.call_from_thread(app.notify, "no web link for this card", markup=False)
    app.run_worker(run, thread=True, group="keys")


def card_head(c, col, url, approved=None):
    """The plain header lines of the card screen: title, then column, tags, assignee, updated, link, metadata."""
    meta = ", ".join(f"{k}={str(v)[:40]}" for k, v in (c.get("metadata") or {}).items())
    t = Text(f"{c.get('title') or ''}\n", style="bold")
    for label, value in (("column", col), *([("status", approved)] if approved else []), ("tags", ", ".join(map(str, c.get("tags") or []))),
                         ("assignee", c.get("assigned_to_display") or c.get("assigned_to")),
                         ("updated", c.get("updated_at")), ("link", url), ("metadata", meta)):
        t.append(f"{label:<9}", style="dim")
        t.append(f"{value or '-'}\n")
    return t


def card_markdown(description):
    """Every section in board order, the text before the first marker first."""
    return "\n\n".join(f"# {k}\n\n{v}" if k else v for k, v in sections(description).items()) or "(no description)"


def todo_markdown(now):
    """The agent's todo list with each item's state, as markdown to put above the card text; "" when it has none."""
    todos = (now or {}).get("todos") or []
    return "# Agent todos\n\n" + "\n".join(f"- {TODO_MARK.get(s, '[ ]')} {t}" for s, t in todos) + "\n\n" if todos else ""


class CardScreen(Screen):
    """The whole card, read-only. The card is read again in a worker; its text is data, never markup."""
    DEFAULT_CSS = """
    #card-head { height: auto; padding: 0 1; border: round $primary; }
    #card-doc { height: 1fr; border: round $primary; }
    """
    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", "close"),
                Binding("y", "copy", "copy card text"), Binding("O", "open", "open in browser")]

    def __init__(self, card_id, col, approved=None, now=None):
        super().__init__()
        self.card_id, self.col, self.card, self.approved, self.now = card_id, col, None, approved, now

    def compose(self):
        yield Static(Text(f"loading… {short_id(self.card_id)}", style="dim"), id="card-head", markup=False)
        with VerticalScroll(id="card-doc"):
            yield Markdown("", open_links=False, id="card-md")
        yield Footer()

    def on_mount(self):
        cid = self.card_id

        def run():
            try:
                c = card(cid)
                try:
                    url = trackers.get("tracker").url(cid)
                except Exception:  # noqa: BLE001 - no link is not an error
                    url = None
            except (SystemExit, Exception) as e:
                self.app.call_from_thread(self._failed, str(e))
                return
            self.app.call_from_thread(self._show, c, url)
        self.run_worker(run, thread=True, group="card")

    def _failed(self, msg):
        self.query_one("#card-head", Static).update(Text(f"could not read the card: {msg}", style="red"))

    async def _show(self, c, url):
        self.card = c
        self.query_one("#card-head", Static).update(card_head(c, self.col, url if safe_link(url) else None, self.approved))
        await self.query_one("#card-md", Markdown).update(todo_markdown(self.now) + card_markdown(c.get("description")))
        self.query_one("#card-doc").focus()

    def action_close(self):
        self.app.pop_screen()

    def action_copy(self):
        if self.card is None:
            return
        text = self.card.get("description") or ""
        self.app.copy_to_clipboard(text)
        for argv in COPIERS:
            if shutil.which(argv[0]):
                _copy_run(argv, text)
                break
        self.app.notify(f"copied {len(text):,} characters", markup=False)

    def action_open(self):
        open_card_url(self.app, self.card_id)

    def on_markdown_link_clicked(self, m):
        if safe_link(m.href):    # card text is untrusted: a file: or javascript: link is never opened
            self.app.open_url(m.href)
        else:
            self.app.notify("only web links open from here", markup=False)


class CardActions:
    """The card keys of the Pipeline list. The view gives _row(): the selected row, or None."""
    CARD_BINDINGS = [Binding("w", "jump", "agent window"), Binding("c", "open_card", "card in browser"),
                     Binding("t", "retry", "try again"), Binding("m", "move_agent", "move to account")]

    def action_jump(self):
        r = self._row()
        if r is None:
            return
        win = (r.get("worker") or {}).get("window")
        self.app.run_worker(lambda: self.app.call_from_thread(self.app.notify, jump_to_window(win), markup=False), thread=True, group="keys")

    def action_retry(self):
        """t on a card whose agent died too often: confirm, then pl retry it off the UI thread."""
        r = self._row()
        if r is None or not r.get("failed"):
            self.app.notify("t retries a card whose agent died too often (marked needs you)", markup=False)
            return
        c = r["card"]

        def run():
            try:
                msg = "\n".join(retry(c["id"]))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"not reset: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def answered(yes):
            if yes:
                self.app.run_worker(run, thread=True, group="keys")
        self.app.push_screen(ConfirmScreen(f"Start {short_id(c['id'])}  {str(c.get('title') or '')[:50]} fresh? "
                                           "This clears its failed agent and attempt count."), answered)

    def action_move_agent(self):
        """m on a card with a live Claude agent: confirm, then move it to another healthy account off the UI thread."""
        r = self._row()
        w = (r or {}).get("worker") or {}
        if r is None or not w.get("window"):
            self.app.notify("m moves a card's live agent to another account", markup=False)
            return
        c = r["card"]

        def run():
            try:
                msg = move_agent.move_card(c["id"], None)   # it picks another healthy Claude account
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed move must not kill the console
                msg = f"not moved: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def answered(yes):
            if yes:
                self.app.run_worker(run, thread=True, group="keys")
        self.app.push_screen(ConfirmScreen(f"Move the agent of {short_id(c['id'])}  {str(c.get('title') or '')[:50]} from "
                                           f"{w.get('profile')} to another Claude account with credits? pl stops it with Ctrl-C in its window and "
                                           "resumes the same session there."), answered)

    def action_open_card(self):
        r = self._row()
        if r is not None:
            open_card_url(self.app, r["card"]["id"])

    def action_view_card(self):
        r = self._row()
        if r is not None:
            self.app.push_screen(CardScreen(r["card"]["id"], r.get("col"), r.get("approved"),
                                               ((self.app.data or {}).get("now") or {}).get(r["card"]["id"])))


def card_rows(data):
    """(column, its card rows) in board order: Done and empty columns left out."""
    rows = [r for r in data["snapshot"]["rows"] if r.get("card")]
    return [(col, here) for col in C.COLUMNS if col != "Done" and (here := [r for r in rows if r.get("col") == col])]


def card_count(data):
    return sum(len(here) for _, here in card_rows(data))


def attention_count(data):
    """Cards on the Pipeline list (Done hidden) that wait on a person."""
    return sum(needs_me(r) for _, here in card_rows(data) for r in here)


class ColumnPicker(ModalScreen):
    """The board's columns but the card's own; enter picks one, esc cancels."""
    DEFAULT_CSS = """
    ColumnPicker { align: center middle; }
    ColumnPicker > Vertical { width: 60; height: auto; max-height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    ColumnPicker OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title, current):
        super().__init__()
        self.title_text, self.columns = title, [c for c in C.COLUMNS if c != current]

    def compose(self):
        with Vertical():
            yield Static(Text(self.title_text))
            yield OptionList(*self.columns)
            yield Static(Text("enter pick · esc cancel", style="dim"))

    def on_option_list_option_selected(self, m):
        self.dismiss(self.columns[m.option_index])

    def action_cancel(self):
        self.dismiss(None)


class ReasonScreen(ModalScreen):
    """One line of text: enter gives it (maybe empty), esc gives None."""
    DEFAULT_CSS = """
    ReasonScreen { align: center middle; }
    ReasonScreen > Vertical { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical():
            yield Static(Text(self.message))
            yield Input(placeholder="reason (optional)")
            yield Static(Text("enter next · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.dismiss(m.value)

    def action_cancel(self):
        self.dismiss(None)


def drop_back(c):
    """The column undrop puts a dropped card back in: where it was dropped from, else Inbox."""
    back = (c.get("metadata") or {}).get("dropped_from")
    return back if back in C.COLUMNS and back != "Done" else "Inbox"


class CardsView(CardActions, Horizontal):
    DEFAULT_CSS = """
    CardsView { height: 1fr; }
    #cards-table { width: 84; height: 1fr; border: round $primary; }
    #cards-side { width: 1fr; height: 1fr; }
    #cards-detail { height: auto; }
    #cards-body { height: 1fr; }
    """
    BINDINGS = [Binding("tab", "app.focus_next", "list / text"), *CardActions.CARD_BINDINGS,
                Binding("v", "move_column", "move to column"), Binding("n", "only_mine", "only mine / all"),
                Binding("o", "answer", "answer questions"), Binding("d", "drop", "drop"), Binding("h", "hand_off", "to Manual"),
                Binding("f", "done", "done"), Binding("e", "edit_input", "edit idea"), Binding("z", "show_done", "show Done"),
                Binding("u", "undrop", "undo drop"), Binding("R", "restart", "restart agent"),
                Binding("i", "adopt", "to pipeline"), Binding("p", "hold", "hold / release")]

    def __init__(self):
        super().__init__()
        self._rows, self._built, self._now, self._data = {}, [], {}, None
        self.only_mine = False   # n: list only the cards that wait on a person
        self.show_done = False   # z: a Done group from the last refresh's Done cards (no extra board read)
        self._bodies, self._pending = {}, {}   # card id → (updated_at, markdown or error); read again only when it changed
        self._shown = None
        self._last = None   # the cursor row before the last move: which way it travels

    def compose(self):
        t = DataTable(id="cards-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_column("id")
        t.add_column("title", width=TITLE_WIDTH)   # a long title ends in … so state and now stay on screen
        t.add_columns("state", "now")
        yield t
        with Vertical(id="cards-side", classes="panel"):
            yield Static(Text(""), id="cards-detail")
            with VerticalScroll(id="cards-body"):
                yield Markdown("", open_links=False, id="cards-md")

    def show(self, data):
        self._now, self._data = data.get("now") or {}, data
        groups = card_rows(data)
        total, mine = sum(len(h) for _, h in groups), attention_count(data)
        if self.show_done and (finished := [r for r in data["snapshot"].get("done") or [] if r.get("card")]):
            groups = [*groups, ("Done", finished)]
        t = self.query_one(DataTable)
        if self.only_mine:
            groups = [(col, here) for col, h in groups if (here := [r for r in h if needs_me(r)])]
            t.border_title = f"Pipeline · needs you only · {mine} of {total} cards · n shows all"
        else:
            t.border_title = f"Pipeline · {total} cards · {mine} need you (n only those) · " + (
                "z hides Done" if self.show_done else "Done is hidden (z shows it)")
        keep, keep_key = t.cursor_row, self._current()[0]
        self._rows, built = {}, []
        for col, here in groups:
            built.append((f"group:{col}", (Text("■", style="dim"), Text(f"{col.upper()} {len(here)}", style="bold"), Text(""), Text(""))))
            for r in here:
                cid = r["card"]["id"]
                if cid in self._rows:   # a duplicate row would raise DuplicateKey
                    continue
                self._rows[cid] = r
                m = r["card"].get("metadata") or {}
                if r.get("col") == "Done":   # dropped (with its reason) or done
                    label, colour = ("dropped", "yellow") if m.get("dropped_at") else ("done", "green")
                else:
                    label, colour = (r["approved"], "red" if r.get("blocked") or r.get("failed") else "green") if r.get("approved") else STATE[card_state(r)]
                state = Text.assemble(("! ", "bold red") if needs_me(r) else "  ", (label, colour))   # ! = waits on a person
                line = str(m.get("drop_reason") or "") if r.get("col") == "Done" else (self._now.get(cid) or {}).get("line") or ""
                built.append((cid, (Text(short_id(cid), style="dim"), Text(r["card"].get("title") or "", no_wrap=True, overflow="ellipsis"), state,
                                    Text(line, style="red" if line.startswith("error: ") else "dim"))))
        if not built:
            empty = "Nothing needs you." if self.only_mine else "No cards on the board."
            built.append(("group:empty", (Text("■", style="dim"), Text(empty, style="dim"), Text(""), Text(""))))
        rebuilt = [k for k, _ in built] != [k for k, _ in self._built]
        if not rebuilt:   # same rows, same order: change only the cells that differ
            for (rid, cells), (_, before) in zip(built, self._built):
                for col, new, was in zip(t.columns, cells, before):
                    if new != was:
                        t.update_cell(rid, col, new)
        else:
            t.clear()
            for rid, cells in built:
                t.add_row(*cells, key=rid)
            idx = t.get_row_index(keep_key) if keep_key in self._rows else min(max(keep, 1), t.row_count - 1)   # row 0 is a heading
            t.move_cursor(row=idx)
        self._last = skip_headings(t, self._rows.__contains__)
        if rebuilt:
            show_first_heading(t, lambda k: k in self._rows)
        self._built = built
        self._bodies = {k: v for k, v in self._bodies.items() if k in self._rows}
        self._detail_for_cursor()

    def _current(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._rows.get(key)

    def _row(self):
        return self._current()[1]

    def _stamp(self, key):
        return str(self._rows[key]["card"].get("updated_at") or "")

    def _detail_for_cursor(self):
        key, r = self._current()
        self.query_one("#cards-body").display = r is not None
        if r is None:
            self._paint(key, detail(None), "")
            return
        stamp = self._stamp(key)
        if self._bodies.get(key, (None,))[0] != stamp and self._pending.get(key) != stamp and self.app.active_tab == "cards":
            self._pending[key] = stamp   # the old text stays on screen until the new one arrives; read only when shown
            self._load(key, stamp)
        self._paint_card(key)

    def opened(self):
        """The tab was just shown: read the selected card's text now; on the first card, show its heading too."""
        show_first_heading(self.query_one(DataTable), lambda k: k in self._rows)
        self._detail_for_cursor()

    def _load(self, key, stamp):
        def run():   # one board read, off the UI thread
            try:
                hit = ("ok", card_markdown(card(key).get("description")))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - RateLimited carries its own note
                hit = ("error", getattr(e, "note", None) or f"could not read the card: {e}")
            self.app.call_from_thread(self._loaded, stamp, key, hit)
        self.run_worker(run, thread=True, group="cards-body")

    def _loaded(self, stamp, key, hit):
        if self._pending.get(key) == stamp:
            del self._pending[key]
        if key not in self._rows or self._stamp(key) != stamp:   # the card changed again meanwhile
            return
        self._bodies[key] = (stamp, hit)
        if self._current()[0] == key:
            self._paint_card(key)

    def _paint_card(self, key):
        r = self._rows[key]
        text = detail(r, self._now.get(key))
        hit = self._bodies.get(key, (None, ("loading", "")))[1]
        if hit[0] != "ok":
            text.append("\n\n")
            text.append("loading…" if hit[0] == "loading" else hit[1], style="dim" if hit[0] == "loading" else "red")
        self._paint(key, text, hit[1] if hit[0] == "ok" else "")

    def _paint(self, key, text, md):
        """Touch the pane only when what it would show differs from what it shows."""
        shown = (key, md, text.plain)
        if shown == self._shown:
            return
        new_md = self._shown is None or self._shown[1] != md
        self._shown = shown
        self.query_one("#cards-detail", Static).update(text)
        if new_md:
            self.query_one("#cards-md", Markdown).update(md)

    def action_only_mine(self):
        """n: only the cards that wait on a person, or all of them again."""
        self.show_attention(not self.only_mine)

    def show_attention(self, on=True, group=None):
        """Turn the attention filter on or off and redraw from the last refresh; with a Needs-you card group (specs,
        plans, manual), select its first card."""
        self.only_mine = on
        if self._data is None:
            return
        self.show(self._data)
        t = self.query_one(DataTable)
        cid = next((k for k, r in self._rows.items() if group and needs_groups([r])[group]), None)
        if cid is not None:
            t.move_cursor(row=t.get_row_index(cid))

    def action_answer(self):
        self.review("answer")

    def review(self, what):
        """a / x / o on a spec or plan waiting for review; any other card only gets a notice."""
        r = self._row()
        if r and r.get("col") == "Spec ready" and (r["card"].get("metadata") or {}).get("pipeline_mode") != "auto":
            self.app.notify("this card is manual: press i to hand it to the pipeline (pl adopt)", markup=False)
            return
        kind = review_kind(r) if r else None
        start_review(self.app, what, r, kind, [k for k, x in self._rows.items() if review_kind(x) == kind])

    def action_move_column(self):
        """v: pick a column, confirm, then move the card off the UI thread."""
        r = self._row()
        if r is None:
            self.app.notify("select a card to move it to another column", markup=False)
            return
        c = r["card"]
        head = f"{short_id(c['id'])}  {str(c.get('title') or '')[:50]}"

        def run(col):
            try:
                msg = f"moved {head} to {col}" if move_to(c["id"], col) else f"not moved: {head}"
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"not moved: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def picked(col):
            if col:
                self.app.push_screen(ConfirmScreen(f"Move {head} from {r.get('col')} to {col}?"),
                                     lambda yes: yes and self.app.run_worker(lambda: run(col), thread=True, group="keys"))
        self.app.push_screen(ColumnPicker(f"Move {head} to which column?", r.get("col")), picked)

    def _ask_then(self, question, job, failed):
        """Confirm, then job() off the UI thread; its lines (or the refusal after failed) become a notice."""
        def run():
            try:
                msg = "\n".join(job())
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"{failed}{e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)
        self.app.push_screen(ConfirmScreen(question), lambda yes: yes and self.app.run_worker(run, thread=True, group="keys"))

    def _open_row(self, key, what):
        """The selected card when it is not in Done, else None after a notice."""
        r = self._row()
        if r is None:
            self.app.notify(f"select a card to {what} it", markup=False)
        elif r.get("col") == "Done":
            self.app.notify(f"{key} works on a card that is not in Done; u puts a dropped card back, v moves any card", markup=False)
        else:
            return r
        return None

    @staticmethod
    def _head(c):
        return f"{short_id(c['id'])}  {str(c.get('title') or '')[:50]}"

    @staticmethod
    def _agent_note(r, stops=False):
        if not (r.get("worker") or {}).get("window"):
            return ""
        return (" Its running agent is stopped first (Ctrl-C in its window)." if stops
                else " Its running agent is not stopped: stop it in its window (w).")

    def action_drop(self):
        """d: an optional reason, a confirm, then pl drop off the UI thread."""
        r = self._open_row("d", "drop")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])

        def reason(text):
            if text is None:
                return
            why = text.strip() or None
            self._ask_then(f"Drop {head}? It moves to Done marked dropped, not done; u or pl undrop puts it back."
                           + (f" Reason: {why}" if why else "") + self._agent_note(r, stops=True),
                           lambda: drop(c["id"], why), "not dropped: ")
        self.app.push_screen(ReasonScreen(f"Drop {head}: why is it no longer needed? (optional)"), reason)

    def action_hand_off(self):
        """h: confirm, then move the card to Manual: a person's to do, no agent starts on it."""
        r = self._open_row("h", "hand off")
        if r is None:
            return
        if r.get("col") == "Manual":
            self.app.notify(f"{short_id(r['card']['id'])} is already in Manual", markup=False)
            return
        c, head = r["card"], self._head(r["card"])
        self._ask_then(f"Move {head} from {r.get('col')} to Manual? It becomes yours to do; the dispatcher starts no agent on it."
                       + self._agent_note(r),
                       lambda: [f"moved {head} to Manual" if move_to(c["id"], "Manual") else f"not moved: {head}"], "not moved: ")

    def action_done(self):
        """f: confirm, then pl done off the UI thread."""
        r = self._open_row("f", "finish")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])
        self._ask_then(f"Mark {head} done? It moves from {r.get('col')} to Done as finished (not dropped)." + self._agent_note(r),
                       lambda: done(c["id"]), "not done: ")

    def action_undrop(self):
        """u on a dropped card in the Done group: confirm, then pl undrop off the UI thread."""
        r = self._row()
        if r is None or r.get("col") != "Done" or not (r["card"].get("metadata") or {}).get("dropped_at"):
            self.app.notify("u puts a dropped card back (z shows Done)", markup=False)
            return
        c = r["card"]
        self._ask_then(f"Put {self._head(c)} back to {drop_back(c)}? The drop is cleared.", lambda: undrop(c["id"]), "not put back: ")

    def action_restart(self):
        """R: confirm, then pl restart off the UI thread: its agent is stopped and the stage starts fresh."""
        r = self._open_row("R", "restart")
        if r is None:
            return
        c, stage = r["card"], (r.get("worker") or {}).get("stage")
        if not stage:
            self.app.notify("R restarts a card's agent; this card has none", markup=False)
            return
        self._ask_then(f"Restart the {stage} agent of {self._head(c)}? pl stops it (Ctrl-C in its window) and the "
                       f"dispatcher starts the {stage} stage fresh (attempt 1).", lambda: restart(c["id"]), "not restarted: ")

    def action_adopt(self):
        """i on a manual card: confirm, then pl adopt off the UI thread."""
        r = self._open_row("i", "hand to the pipeline")
        if r is None:
            return
        c = r["card"]
        if (c.get("metadata") or {}).get("pipeline_mode") == "auto":
            self.app.notify(f"{short_id(c['id'])} is already in the pipeline", markup=False)
            return
        self._ask_then(f"Hand {self._head(c)} to the pipeline (pl adopt)? The dispatcher starts the next stage's agent on it.",
                       lambda: adopt(c["id"]), "not adopted: ")

    def action_hold(self):
        """p: hold a card (an optional reason, then a confirm) or release a held one (a confirm); pl hold / unhold."""
        r = self._open_row("p", "hold")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])
        if "parked" in (c.get("tags") or []):
            self._ask_then(f"Release the hold on {head}? The dispatcher takes it again.", lambda: unhold(c["id"]), "not released: ")
            return

        def reason(text):
            if text is None:
                return
            why = text.strip() or None
            self._ask_then(f"Hold {head}? The dispatcher starts no agent on it until you press p again (pl unhold)."
                           + (f" Reason: {why}" if why else "") + self._agent_note(r),
                           lambda: hold(c["id"], why), "not held: ")
        self.app.push_screen(ReasonScreen(f"Hold {head}: why does it wait? (optional)"), reason)

    def action_show_done(self):
        """z: show or hide the Done group, from the last refresh."""
        self.show_done = not self.show_done
        if self._data is not None:
            self.show(self._data)

    def action_edit_input(self):
        """e: the card's INPUT (your idea) in $EDITOR, the same way the spec review's E opens the spec; written back
        only when it changed and the card's INPUT is still the text that was opened."""
        r = self._row()
        if r is None:
            self.app.notify("select a card to edit its idea", markup=False)
            return
        cid = r["card"]["id"]

        def read():   # one board read, off the UI thread
            try:
                c = card(cid)
                loaded = sections(c.get("description")).get("INPUT")
                if loaded is None:
                    raise SystemExit(f"{short_id(cid)} has no INPUT section")
                path = review_file("input", c)
                before = path.read_text()
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify, f"not opened: {e}", severity="error", markup=False)
                return
            self.app.call_from_thread(self._edit_input, cid, path, loaded, before)
        self.app.run_worker(read, thread=True, group="keys")

    def _edit_input(self, cid, path, loaded, before):
        open_editor(self.app, path)
        new = path.read_text() if path.is_file() else before
        if new == before:
            self.app.notify("INPUT not changed", markup=False)
            return

        def run():
            try:
                write_section(cid, "INPUT", loaded, new)
                path.unlink(missing_ok=True)   # saved: the next e starts from the card again
                msg = f"saved INPUT of {short_id(cid)}"
            except (SystemExit, Exception) as e:  # noqa: BLE001
                msg = f"{e}. Your edit is kept in {path}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)
        self.app.run_worker(run, thread=True, group="keys")

    def on_data_table_row_highlighted(self, _):
        self._last = skip_headings(self.query_one(DataTable), self._rows.__contains__, self._last)
        try:
            self._detail_for_cursor()
        except Exception as e:  # noqa: BLE001 - a bad card must not kill the console
            self.app.notify(f"render failed: {type(e).__name__}: {e}", markup=False)

    def on_data_table_row_selected(self, _):
        self.action_view_card()
