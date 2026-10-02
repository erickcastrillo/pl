"""Pipeline tab: every card as a list grouped by board column (Done hidden), its full text beside it, and actions."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Markdown, OptionList, Static

from pl import config as C
from pl.board import card
from pl.commands import move_to
from pl.tui.chrome import show_first_heading, skip_headings
from pl.tui.needs import detail, review_kind, start_review
from pl.tui.pipeline import STATE, CardActions, card_markdown, card_state
from pl.tui.review import ConfirmScreen
from pl.util import short_id

TITLE_WIDTH = 36


def card_rows(data):
    """(column, its card rows) in board order: Done and empty columns left out."""
    rows = [r for r in data["snapshot"]["rows"] if r.get("card")]
    return [(col, here) for col in C.COLUMNS if col != "Done" and (here := [r for r in rows if r.get("col") == col])]


def card_count(data):
    return sum(len(here) for _, here in card_rows(data))


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


class CardsView(CardActions, Horizontal):
    DEFAULT_CSS = """
    CardsView { height: 1fr; }
    #cards-table { width: 84; height: 1fr; border: round $primary; }
    #cards-side { width: 1fr; height: 1fr; }
    #cards-detail { height: auto; }
    #cards-body { height: 1fr; }
    """
    BINDINGS = [Binding("tab", "app.focus_next", "list / text"), *CardActions.CARD_BINDINGS,
                Binding("v", "move_column", "move to column")]

    def __init__(self):
        super().__init__()
        self._rows, self._built, self._now = {}, [], {}
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
        self._now = data.get("now") or {}
        groups = card_rows(data)
        t = self.query_one(DataTable)
        t.border_title = f"Pipeline · {sum(len(h) for _, h in groups)} cards · Done is hidden"
        keep, keep_key = t.cursor_row, self._current()[0]
        self._rows, built = {}, []
        for col, here in groups:
            built.append((f"group:{col}", (Text("■", style="dim"), Text(f"{col.upper()} {len(here)}", style="bold"), Text(""), Text(""))))
            for r in here:
                cid = r["card"]["id"]
                if cid in self._rows:   # a duplicate row would raise DuplicateKey
                    continue
                self._rows[cid] = r
                label, colour = (r["approved"], "green") if r.get("approved") else STATE[card_state(r)]
                line = (self._now.get(cid) or {}).get("line") or ""
                built.append((cid, (Text(short_id(cid), style="dim"), Text(r["card"].get("title") or "", no_wrap=True, overflow="ellipsis"), Text(label, style=colour),
                                    Text(line, style="red" if line.startswith("error: ") else "dim"))))
        if not built:
            built.append(("group:empty", (Text("■", style="dim"), Text("No cards on the board.", style="dim"), Text(""), Text(""))))
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

    def review(self, what):
        """a / x on a spec or plan waiting for review; any other card only gets a notice."""
        r = self._row()
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

    def on_data_table_row_highlighted(self, _):
        self._last = skip_headings(self.query_one(DataTable), self._rows.__contains__, self._last)
        try:
            self._detail_for_cursor()
        except Exception as e:  # noqa: BLE001 - a bad card must not kill the console
            self.app.notify(f"render failed: {type(e).__name__}: {e}", markup=False)

    def on_data_table_row_selected(self, _):
        self.action_view_card()
