"""Alerts tab: the open alerts, newest first as alerts.listing() gives them, with a detail pane. k acknowledges one."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Static

from pl import alerts
from pl.util import age

EMPTY = "Alerts: none open (pl alerts --all for history)"


def detail(a):
    """Plain text for the detail pane, from the alert's own fields."""
    if a is None:
        return Text(EMPTY, style="dim")
    return Text("\n".join([a.get("title") or "", "", f"fix      {a.get('fix') or ''}", f"alert    {a['key']}",
                           f"severity {a['severity']}", f"open     {age(a.get('first_seen'))} · seen {a.get('count', 1)} times",
                           "acked: no more reminders until it clears" if a.get("acked") else "k acknowledges: no more reminders"]))


def cells(a):
    return (Text(f"{a['severity']} {age(a.get('first_seen'))}", style="red" if a["severity"] == "high" else "#e0a040"),
            Text(f"{a.get('title') or ''} · {a.get('fix') or ''}"),
            Text(f"×{a.get('count', 1)}" + (" acked" if a.get("acked") else ""), style="dim"))


class AlertsView(Horizontal):
    DEFAULT_CSS = """
    #alerts-table { width: 2fr; height: 1fr; border: round $error; }
    #alerts-detail { width: 1fr; height: 1fr; }
    """
    BINDINGS = [Binding("k", "ack", "acknowledge alert")]

    def __init__(self):
        super().__init__()
        self._rows, self._built = {}, []

    def compose(self):
        t = DataTable(id="alerts-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_columns("severity", "alert", "seen")
        yield t
        yield Static(detail(None), id="alerts-detail", classes="panel")

    def show(self, data):
        al = data.get("alerts") or []
        t = self.query_one(DataTable)
        t.border_title = f"Alerts · {len(al)}"
        keep_key = self._current()[0]
        self._rows = {a["key"]: a for a in al}
        built = [(a["key"], cells(a)) for a in self._rows.values()]
        if [k for k, _ in built] == [k for k, _ in self._built]:   # same rows: change only the cells that differ
            for (key, new), (_, was) in zip(built, self._built):
                for col, n, w in zip(t.columns, new, was):
                    if n != w:
                        t.update_cell(key, col, n)
        else:
            t.clear()
            for key, row in built:
                t.add_row(*row, key=key)
            if keep_key in self._rows:   # the same alert stays selected, so k never acks another one
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
            a["acked"] = True
            self.app.notify("acknowledged: no more reminders until it clears")
        if self.app.data is not None:
            self.show(self.app.data)

    def on_data_table_row_highlighted(self, _):
        self._detail_for_cursor()
