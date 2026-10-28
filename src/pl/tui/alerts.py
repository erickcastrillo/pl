"""Alerts tab: the open, unacknowledged alerts newest first, with a detail pane. a acknowledges one."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Static

from pl import alerts
from pl.tui.chrome import empty_state
from pl.util import age, short_id


def title(a):
    """The alert's title. An older title holds a GitHub card id cut to 8 characters ("Card your-org: ..."):
    it shows the short id from the alert's key instead."""
    t, parts = a.get("title") or "", str(a.get("key") or "").split(":")
    cid = parts[1] if len(parts) > 1 else ""
    return t.replace(cid[:8], short_id(cid), 1) if "#" in cid and short_id(cid) not in t else t


def _cards(a):
    return ", ".join(short_id(c) for c in a.get("cards") or []) or "-"


def detail(a):
    """Plain text for the detail pane, from the alert's own fields."""
    if a is None:
        return Text(f"No open alerts. pl watches for: {alerts.watching()}", style="dim")
    return Text("\n".join([title(a), "", f"severity {a['severity']}",
                           f"open     {age(a.get('first_seen'))} · seen {a.get('count', 1)} times",
                           f"cards    {_cards(a)}", f"details  {a.get('details') or '-'}", f"fix      {a.get('fix') or '-'}",
                           f"alert    {a['key']}", "a acknowledges: hidden until it changes"]))


def cells(a):
    return (Text(a["severity"], style="red" if a["severity"] == "high" else "#e0a040"), Text(title(a)),
            Text(age(a.get("first_seen")), style="dim"), Text(_cards(a), style="dim"))


class AlertsView(Horizontal):
    DEFAULT_CSS = """
    #alerts-table { width: 2fr; height: 1fr; border: round $error; }
    #alerts-detail { width: 1fr; height: 1fr; }
    """
    BINDINGS = [Binding("a", "ack", "acknowledge alert"), Binding("k", "ack", "acknowledge alert", show=False)]

    def __init__(self):
        super().__init__()
        self._rows, self._built = {}, []

    def compose(self):
        t = DataTable(id="alerts-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_columns("severity", "alert", "age", "cards")
        yield t
        yield Static(detail(None), id="alerts-detail", classes="panel")

    def show(self, data):
        al = data.get("alerts") or []
        t = self.query_one(DataTable)
        t.border_title = f"Alerts · {len(al)}"
        keep_key = self._current()[0]
        self._rows = {a["key"]: a for a in al}
        built = [(a["key"], cells(a)) for a in self._rows.values()]
        if not built:
            built = [(f"empty:{i}", (Text(line, style="dim"), Text(""), Text(""), Text(""))) for i, line in enumerate(empty_state("alerts"))]
        if [k for k, _ in built] == [k for k, _ in self._built]:   # same rows: change only the cells that differ
            for (key, new), (_, was) in zip(built, self._built):
                for col, n, w in zip(t.columns, new, was):
                    if n != w:
                        t.update_cell(key, col, n)
        else:
            t.clear()
            for key, row in built:
                t.add_row(*row, key=key)
            if keep_key in self._rows:   # the same alert stays selected, so a never acks another one
                t.move_cursor(row=t.get_row_index(keep_key))
        self._built = built
        self._detail_for_cursor()

    def _current(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._rows.get(key)

    def _detail_for_cursor(self):
        self.query_one("#alerts-detail", Static).update(detail(self._current()[1]))

    def action_ack(self):
        key, a = self._current()
        if a is None:
            self.app.notify("select an alert to acknowledge")
            return
        if alerts.ack(key):
            if self.app.data is not None:
                self.app.data["alerts"] = [x for x in self.app.data.get("alerts") or [] if x["key"] != key]
            self.app.notify("acknowledged: hidden until its cards or severity change")
        if self.app.data is not None:
            self.app.run_worker(self.app.render_views())   # tab label and header count follow the row

    def on_data_table_row_highlighted(self, _):
        self._detail_for_cursor()

    def on_data_table_row_selected(self, _):
        self._detail_for_cursor()
