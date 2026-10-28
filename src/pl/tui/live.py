"""A read-only, live pop-up view of one agent's tmux window. No key is sent to the pane."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static
from textual.worker import get_current_worker

from pl import agent_screen
from pl.tui.chrome import close

REFRESH_SECONDS = 2
SCROLLBACK_LINES = 2000
NO_WINDOW = "no agent window"


class LiveScreen(ModalScreen):
    DEFAULT_CSS = """
    LiveScreen { align: center middle; }
    LiveScreen > Vertical { width: 90%; height: 90%; border: round $accent; background: $surface; padding: 0 1; }
    #live-scroll { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "close", "back"), Binding("pageup", "page_up", "page up"),
                Binding("pagedown", "page_down", "page down")]

    def __init__(self, pane, title):
        super().__init__()
        self.pane, self.heading = pane, title
        self._text = None

    def compose(self):
        with Vertical():
            yield Static(Text(self.heading), id="live-head", markup=False)
            with VerticalScroll(id="live-scroll"):
                yield Static(Text(NO_WINDOW, style="dim"), id="live-body", markup=False)

    def on_mount(self):
        self._poll()
        self.set_interval(REFRESH_SECONDS, self._poll)

    def _poll(self):
        self.run_worker(self._read, thread=True, group="live", exclusive=True)

    def _read(self):
        result = agent_screen.read(self.pane, lines=SCROLLBACK_LINES)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._show, *result)

    def _show(self, text, word, why):
        status = f"{word}: {why}" if why else word
        self.query_one("#live-head", Static).update(Text(f"{self.heading}  ·  {status}  ·  esc closes"))
        if text is None or text == self._text:
            return
        scroll = self.query_one("#live-scroll", VerticalScroll)
        follow = self._text is None or scroll.scroll_y >= scroll.max_scroll_y - 1
        self._text = text
        self.query_one("#live-body", Static).update(Text.from_ansi(text))
        if follow:
            self.call_after_refresh(scroll.scroll_end, animate=False)

    def action_close(self):
        close(self)

    def action_page_up(self):
        self.query_one("#live-scroll", VerticalScroll).scroll_page_up()

    def action_page_down(self):
        self.query_one("#live-scroll", VerticalScroll).scroll_page_down()
