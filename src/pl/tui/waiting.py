"""Waiting: one list of everything needs_you() returns, oldest wait first, with approve, send back and open."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static
from textual.widgets.data_table import RowDoesNotExist

from pl.tui.cards import CardScreen, FitTable
from pl.tui.needs import needs_you, review_kind, start_review
from pl.tui.prs import PrsView
from pl.util import age, parse_iso, short_id


def kind_of(r):
    """PR, spec, plan, manual, or agent (an agent that needs you, died or is blocked)."""
    if r.get("pr"):
        return "PR"
    return review_kind(r) or ("manual" if r.get("col") == "Manual" else "agent")


def since(r):
    """The ISO time the row has waited since: the PR's or the card's last update (no per-state time is stored yet)."""
    return str((r["pr"].get("updatedAt") if r.get("pr") else r["card"].get("updated_at")) or "")


def waiting_rows(data):
    """needs_you(), oldest wait first; rows with no time go last, in needs_you() order."""
    return sorted(needs_you(data), key=lambda r: (not since(r), parse_iso(since(r)) if since(r) else 0))


def row_key(r):
    pr = r.get("pr")
    return (pr.get("url") or f"{pr['repo']}#{pr['number']}") if pr else r["card"]["id"]


def row_id(r):
    pr = r.get("pr")
    return f"{pr['repo']}#{pr['number']}" if pr else short_id(r["card"]["id"])


def row_text(r):
    """Kind, id and age come before the title, so a title cut at the terminal's edge never hides them."""
    title = (r["pr"] if r.get("pr") else r["card"]).get("title") or ""
    waited = age(parse_iso(since(r))) if since(r) else "-"
    return Text(f"{kind_of(r):<6} {row_id(r):<16} {waited:>4}  {' '.join(title.split())}", no_wrap=True, overflow="ellipsis")


class WaitingTable(FitTable):
    BINDINGS = [Binding("enter", "select_cursor", "open")]   # the table's own Enter wins over the view's, so it carries the label


class WaitingView(Vertical):
    DEFAULT_CSS = """
    #waiting-table { height: 1fr; border: round $panel-lighten-2; }
    #waiting-empty { padding: 1 2; color: $text-muted; }
    """
    BINDINGS = [Binding("a", "approve", "approve"), Binding("x", "send_back", "send back")]

    def __init__(self):
        super().__init__()
        self._rows = {}

    def compose(self):
        t = WaitingTable("w", id="waiting-table", cursor_type="row", show_header=False)
        t.add_column("w", key="w")
        yield t
        yield Static(Text("Nothing waits on you", style="dim"), id="waiting-empty")

    def show(self, data):
        rows = waiting_rows(data)
        t = self.query_one(FitTable)
        t.border_title = f"Waiting · {len(rows)}"
        t.display = bool(rows)
        self.query_one("#waiting-empty").display = not rows
        keep = self._current()[0]
        self._rows = {}
        for r in rows:
            self._rows.setdefault(row_key(r), r)
        if [k for k in self._rows] != [row.key.value for row in t.ordered_rows]:
            t.clear()
            for key, r in self._rows.items():
                t.add_row(row_text(r), key=key)
            if keep in self._rows:
                t.move_cursor(row=t.get_row_index(keep))
        else:
            for key, r in self._rows.items():
                t.update_cell(key, "w", row_text(r))

    def _current(self):
        t = self.query_one(FitTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._rows.get(key)

    def _ids(self, kind):
        return [r["card"]["id"] for r in self._rows.values() if kind_of(r) == kind]

    def _prs_at(self, r):
        """The Pull requests view with its cursor on this PR, or None (with a notice) when it is not listed there."""
        prs = self.app.query_one(PrsView)
        t = prs.query_one(FitTable)
        try:
            t.move_cursor(row=t.get_row_index(row_key(r)))
        except RowDoesNotExist:
            self.app.notify("that pull request is not in the Pull requests list yet", markup=False)
            return None
        return prs

    def on_data_table_row_selected(self, _):
        self.action_open()

    def action_approve(self):
        r = self._current()[1]
        if r is None:
            return
        kind = kind_of(r)
        if kind in ("spec", "plan"):
            start_review(self.app, "approve", r, kind, self._ids(kind))
        elif kind == "PR" and r["pr"]["state"] == "merge":
            if (prs := self._prs_at(r)) is not None:
                prs.action_merge()
        else:
            self.app.notify("a approves a spec or plan, or merges a ready PR; press Enter to open this one", markup=False)

    def action_send_back(self):
        r = self._current()[1]
        if r is None:
            return
        kind = kind_of(r)
        if kind in ("spec", "plan"):
            start_review(self.app, "send_back", r, kind, self._ids(kind))
        else:
            self.app.notify("x sends a spec or plan back; press Enter to open this one", markup=False)

    def action_open(self):
        r = self._current()[1]
        if r is None:
            return
        kind = kind_of(r)
        if kind in ("spec", "plan"):
            start_review(self.app, "read", r, kind, self._ids(kind))
        elif kind == "PR":
            if self._prs_at(r) is not None:
                self.app.action_tab("prs")
        else:
            self.app.push_screen(CardScreen(r["card"]["id"], r.get("col"), r.get("approved"),
                                            ((self.app.data or {}).get("now") or {}).get(r["card"]["id"])))
