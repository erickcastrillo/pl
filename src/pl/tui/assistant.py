"""The Assistant tab: the live screen of the profile's assistant harness session, and a box that types into it.
tmux runs only in thread workers; the screen is read once a second while this tab is active, never otherwise."""
from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input, Static

from pl import agents, assistant, ideas
from pl import config as C
from pl.tui.review import ConfirmScreen

KEYS = ("enter send (a lone digit answers a numbered menu) · ctrl+t chat/idea mode · "
        "ctrl+o open its window (arrows, Esc) · ctrl+r start over · ctrl+f file a ready idea")
HINT = {"chat": "chat mode: ask pl to do things (it asks before acting)",
        "idea": "idea mode: drafting an idea brief with you; it files the card only after your yes"}


class AssistantView(Widget):
    DEFAULT_CSS = """
    AssistantView { height: 1fr; }
    #assistant-screen { height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    #assistant-status, #assistant-keys { height: 1; color: $text-muted; }
    #assistant-warning { height: auto; color: $warning; }
    #assistant-input { border: round $warning; }
    """
    BINDINGS = [Binding("ctrl+t", "mode", "chat/idea"), Binding("ctrl+o", "open_window", "open window"),
                Binding("ctrl+r", "restart", "start over"), Binding("ctrl+f", "file_idea", "file idea")]

    def __init__(self):
        super().__init__(id="assistant-view")
        self.started, self.mode, self.status = False, "chat", "opening the tab starts the assistant"
        self.ready = None     # the idea the assistant marked ready to file, shown until it is filed

    def compose(self):
        yield Static(Text("the assistant starts when you open this tab", style="dim"), id="assistant-screen")
        yield Static("", id="assistant-warning")
        yield Static("", id="assistant-status")
        yield Input(placeholder="ask the assistant; enter sends", id="assistant-input")
        yield Static(Text(KEYS, style="dim"), id="assistant-keys")

    def on_mount(self):
        if C.CONFIG_DIR is None:   # pl commands need a profile: the assistant would have nothing to act on
            self.query_one("#assistant-input", Input).disabled = True
            self.status = "no profile loaded: the assistant needs one (pl setup)"
        self._show_status()
        self.set_interval(1, self.tick)

    # ---------- workers: every one catches its errors (a worker that raises kills the app) ----------

    def _bg(self, fn, group, done=None, failed=None, exclusive=False):
        def run():
            try:
                out = fn()
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify, f"assistant: {e}", severity="error", markup=False)
                if failed:
                    self.app.call_from_thread(failed)
                return
            if done:
                self.app.call_from_thread(done, out)
        self.run_worker(run, thread=True, group=group, exclusive=exclusive)

    def _active(self):
        return self.app.active_tab == "assistant"

    def opened(self):
        """The tab was opened: the first time in this console, start (or reattach to) the assistant."""
        if not self.started and C.CONFIG_DIR is not None:
            self.started = True
            self._start()
        self.tick()

    def _start(self):
        def fn():
            status = assistant.ensure()
            return status, assistant.load().get("mode") or "chat", assistant.warning()
        self._bg(fn, "assistant-start", self._started, self._start_failed)

    def _start_failed(self):
        self.started = False     # the next time the tab opens, it tries again
        self.status = "the assistant did not start (see the notice); reopen the tab to try again"
        self._show_status()

    def _started(self, out):
        self.status, self.mode, warn = out
        self.query_one("#assistant-warning", Static).update(Text(warn or ""))   # one line: allow rules that skip asking
        self._show_status()
        self.tick()

    def tick(self):
        if not (self.started and self._active()):
            return
        height = max(5, self.query_one("#assistant-screen").size.height - 2)

        def fn():   # a pane that died while the console stays open (/exit, tmux lost it) is started again
            status = None if assistant.pane() else assistant.ensure()
            return status, assistant.screen(height), assistant.ready_idea()
        self._bg(fn, "assistant-screen", self._show_screen, self._start_failed, exclusive=True)

    def _show_screen(self, out):
        status, lines, self.ready = out
        if status:
            self.status = status
        body = Text("\n".join(lines)) if lines else Text("the assistant's screen is empty", style="dim")
        self.query_one("#assistant-screen", Static).update(body)
        self._show_status()

    def _show_status(self):
        box = self.query_one("#assistant-screen")
        box.border_title = f"Assistant · {self.mode} mode"
        if self.ready:
            line = Text(f"Idea ready: {str(self.ready.get('title'))[:80]} · ctrl+f to file", style="bold")
        else:
            line = Text(f"{self.status} · {HINT.get(self.mode, '')}", style="dim")
        self.query_one("#assistant-status", Static).update(line)

    # ---------- keys ----------

    def on_input_submitted(self, m):
        text = m.value
        m.input.value = ""
        if not text.strip():     # a bare Enter could accept a menu's default
            return
        self._bg(lambda: assistant.send(text), "assistant-send", lambda _: self.tick())

    def action_mode(self):
        new = "idea" if self.mode == "chat" else "chat"

        def done(_):
            self.mode = new
            self._show_status()
            self.tick()
        self._bg(lambda: assistant.set_mode(new), "assistant-mode", done)

    def action_open_window(self):
        win = assistant.load().get("window")
        self._bg(lambda: agents.jump_to_window(win), "assistant-window", lambda msg: self.app.notify(msg, markup=False))

    def action_restart(self):
        def fn():
            assistant.reset()
            return assistant.ensure(), "chat", assistant.warning()

        def answered(yes):
            if yes:
                self._bg(fn, "assistant-start", self._started)
        self.app.push_screen(ConfirmScreen("Start a new assistant conversation? The current one ends."), answered)

    def action_file_idea(self):
        """File the idea the assistant marked ready: a pl-side confirm, outside the harness, then one Inbox card."""
        idea = self.ready
        if not idea:
            self.app.notify("no idea is ready to file: the assistant marks one after your yes", markup=False)
            return

        def fn():   # file only the draft the dialog showed: still marked ready, same version
            cur = ideas.load(idea["id"])
            if not (cur.get("status") == "interviewing" and cur.get("ready_to_file") and ideas.is_clear(cur)
                    and cur.get("version") == idea.get("version")):
                raise SystemExit("the idea changed since this dialog showed it: nothing filed; check it and try again")
            done = ideas.approve(cur)
            if done.get("status") != "approved" or not done.get("card_id"):
                raise SystemExit("another console is filing this idea right now")
            return f"card {str(done['card_id'])[:8]} created in Inbox; the spec stage picks it up"

        def filed(msg):
            self.ready = None
            self._show_status()
            self.app.notify(msg, markup=False)

        def answered(yes):
            if yes:
                self._bg(fn, "assistant-file", filed)
        problem = str((idea.get("brief") or {}).get("problem") or "")[:160]
        self.app.push_screen(ConfirmScreen(f"File the idea \"{str(idea.get('title'))[:80]}\" as an Inbox card? "
                                           f"Problem: {problem or '-'}. The spec stage picks it up."), answered)
