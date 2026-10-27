"""A read-only full screen with one loop run's whole log, masked as it is read."""
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from pl import loops, util

MISSING = f"The log of this run is no longer kept (only the last {loops.RUNS_KEPT} runs are kept)."


def run_title(name, run):
    try:
        when = datetime.fromisoformat(run["started_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    except (KeyError, TypeError, ValueError):
        when = "?"
    title = f"{name} loop · {when} · {'ok' if run.get('ok') else 'failed'}"
    if not run.get("ok") and run.get("reason"):
        title += f" · {run['reason']}"
    return title


def log_text(name, run):
    """The run's masked log, or None when it is not kept. A `log` that is not a plain file name never leaves the loop's folder."""
    log = run.get("log")
    if not isinstance(log, str) or Path(log).name != log:
        return None
    try:
        text = (loops.run_log_dir(name) / log).read_text(errors="replace")
    except OSError:
        return None
    return util.mask(text)


class RunLogScreen(Screen):
    BINDINGS = [Binding("escape", "close", "back"), Binding("home", "scroll_top", "top"),
                Binding("end,G", "scroll_bottom", "end"), Binding("pageup", "page_up", "page up"),
                Binding("pagedown", "page_down", "page down")]

    def __init__(self, name, run):
        super().__init__()
        self.heading = run_title(name, run)
        self.body = log_text(name, run)

    def compose(self):
        yield Static(Text(self.heading, style="bold"), id="runlog-head", markup=False)
        with VerticalScroll(id="runlog-scroll"):
            yield Static(Text(MISSING, style="dim") if self.body is None else Text(self.body), id="runlog-body", markup=False)
        yield Footer()

    def _scroll(self):
        return self.query_one("#runlog-scroll", VerticalScroll)

    def action_close(self):
        self.app.pop_screen()

    def action_scroll_top(self):
        self._scroll().scroll_home(animate=False)

    def action_scroll_bottom(self):
        self._scroll().scroll_end(animate=False)

    def action_page_up(self):
        self._scroll().scroll_page_up()

    def action_page_down(self):
        self._scroll().scroll_page_down()
