"""A read-only, live pop-up view of one agent's tmux window. No key is sent to the pane."""
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static
from textual.worker import get_current_worker

from pl import agent_screen
from pl.tui.chrome import close
from pl.util import age, mask, parse_iso

REFRESH_SECONDS = 2
SCROLLBACK_LINES = 2000
AGENT_SECONDS = 1
NO_WINDOW = "no agent window"
AGENT_COLOURS = {"stopped": "red", "waiting for you": "yellow", "working": "green"}


def agent_head(w, word, why):
    """The one-line header of an agent's window: stage, account, model, status word and elapsed time; missing values are left out."""
    status = f"{word}: {why}" if why else word
    who = w.get("profile") or w.get("harness")
    model = w.get("model")
    account = f"{who} ({model})" if who and model else who or (f"({model})" if model else "")
    started = parse_iso(w.get("started_at"))
    parts = [f"{w['stage']} agent" if w.get("stage") else "", account]
    head = Text(" · ".join(mask(str(p)) for p in parts if p))
    head.append((" · " if head.plain else "") + mask(status), AGENT_COLOURS.get(word, "dim"))
    if started:
        head.append(f" · {age(started)}")
    return head


class AgentPane(Vertical):
    """A read-only column with one worker's live tmux text. It never sends a key to the pane."""
    DEFAULT_CSS = """
    AgentPane { display: none; width: 1fr; height: 1fr; border-left: vkey $primary; }
    #agent-scroll { height: 1fr; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.worker, self._latest, self._drawn = {}, None, None

    def compose(self):
        yield Static(Text(""), id="agent-head", markup=False)
        with VerticalScroll(id="agent-scroll"):
            yield Static(Text(NO_WINDOW, style="dim"), id="agent-body", markup=False)

    def start(self, worker):
        self.worker = worker
        self.display = True
        self.query_one("#agent-head", Static).update(agent_head(worker, "working", ""))
        self._poll()
        self.set_interval(AGENT_SECONDS, self._poll)

    def _poll(self):
        if self.app.screen is not self.screen:
            return
        self.run_worker(self._read, thread=True, group="card-agent", exclusive=True)

    def _read(self):
        result = agent_screen.read(self.worker.get("pane"), lines=SCROLLBACK_LINES)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._show, *result)

    def _show(self, text, word, why):
        self.query_one("#agent-head", Static).update(agent_head(self.worker, word, why))
        if text is None:
            return
        self._latest = text
        scroll = self.query_one("#agent-scroll", VerticalScroll)
        follow = self._drawn is None or scroll.scroll_y >= scroll.max_scroll_y - 1
        if follow and text != self._drawn:
            self._drawn = text
            self.query_one("#agent-body", Static).update(Text.from_ansi(text))
            self.call_after_refresh(scroll.scroll_end, animate=False)


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
        self.query_one("#live-scroll", VerticalScroll).scroll_page_up(animate=False)   # mid-animation, the follow check misreads the position

    def action_page_down(self):
        self.query_one("#live-scroll", VerticalScroll).scroll_page_down(animate=False)
