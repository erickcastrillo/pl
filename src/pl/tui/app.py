"""PlApp: the pl console. One background refresh feeds every view; views never call the board, gh or tmux."""
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from pl import board, dispatch, manager, whatsnew
from pl import config as C
from pl.trackers.github import RateLimited
from pl.tui.assistant import AssistantView
from pl.tui.chrome import TABS, header_text, tabs
from pl.tui.dashboard import WINDOWS, DashboardView, StandupScreen
from pl.tui.ideas import IdeasView
from pl.tui.loops import ActivityView, LoopsView
from pl.tui.needs import NeedsView, needs_groups, waiting_total
from pl.tui.pipeline import PipelineView
from pl.tui.prs import PrsView
from pl.tui.review import ConfirmScreen
from pl.tui.settings import SettingsView
from pl.tui.subagents import SubagentsView

LATER = {}
VIEWS = {"dashboard": DashboardView, "needs": NeedsView, "ideas": IdeasView, "pipeline": PipelineView, "prs": PrsView,
         "loops": LoopsView, "activity": ActivityView, "settings": SettingsView,
         "subagents": SubagentsView, "assistant": AssistantView}


REFRESH, GITHUB_REFRESH = 15, 60   # seconds between board refreshes; GitHub's GraphQL budget needs the slower one


def default_interval():
    """Seconds between board refreshes when none is given: 60 for a GitHub tracker, else 15."""
    return GITHUB_REFRESH if str((C.TRACKER or {}).get("type") or "").startswith("github") else REFRESH


def default_provider():
    """The real data for one refresh: board + tmux snapshot, event metrics per window, GitHub PR activity."""
    from pl import events, memory, watch
    board.share("read")   # reuse the dispatcher's board read while it is young
    snap = watch.watch_snapshot()
    return {"snapshot": snap,
            "metrics_by_window": {key: events.metrics(seconds) for key, _, seconds in WINDOWS},
            "daily": {"specs": events.daily("Spec ready"), "plans": events.daily("Plan for review")},
            "pr_activity": watch.pr_activity(), "memory": memory.status(), "usage": _usage(),
            "machine": manager.read_status(), "now": _now(snap["rows"]), "alerts": _alerts()}


def _now(rows):
    """The "doing now" line and todos per card with a live Claude agent; a transcript problem never fails the refresh."""
    from pl.tui import subagents
    try:
        return subagents.now_lines(rows)
    except Exception:  # noqa: BLE001
        return {}


def _alerts():
    """Open alerts for Needs you; an unreadable file never fails the refresh."""
    from pl import alerts
    try:
        return alerts.listing()
    except Exception:  # noqa: BLE001
        return []


def _usage():
    """Token counts for the Dashboard and Loops tab; a transcript problem never fails the refresh."""
    from pl import usage
    try:
        return usage.summary(usage.scan())
    except Exception:  # noqa: BLE001
        return None


class DataReady(Message):
    def __init__(self, data, error):
        super().__init__()
        self.data, self.error = data, error


class WhatsNewScreen(ModalScreen):
    """The what's-new entries given; esc closes."""
    DEFAULT_CSS = """
    WhatsNewScreen { align: center middle; }
    WhatsNewScreen > Vertical { width: 100%; max-width: 96; height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    #whatsnew-body { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(self, entries):
        super().__init__()
        self.entries = entries

    def compose(self):
        with Vertical():
            yield Static(Text("What's new in pl", style="bold"))
            with VerticalScroll(id="whatsnew-body"):
                yield Static(Text(whatsnew.text(self.entries)))
            yield Static(Text("esc close · ? or the palette (ctrl+p) opens this again · pl whatsnew prints it", style="dim"))

    def action_close(self):
        self.dismiss(None)


class PlApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "pl"
    BINDINGS = [Binding(str((i + 1) % 10), f"tab('{tid}')", "views" if i == 0 else name, show=i == 0, key_display="0-9")
                for i, (tid, name) in enumerate(TABS)] + [
        Binding("w", "cycle_window", "window"), Binding("s", "standup", "standup"),
        Binding("a", "review('approve')", "approve"), Binding("x", "review('send_back')", "send back"),
        Binding("ctrl+x", "review('send_back')", "send back", key_display="^x"),
        Binding("D", "dispatcher", "dispatcher (this profile)"), Binding("r", "refresh", "refresh"),
        Binding("question_mark", "whatsnew", "what's new", key_display="?"), Binding("q", "quit", "quit")]

    def __init__(self, snapshot_provider=None, interval=None, autostart=None, whatsnew=None):
        super().__init__()
        self.autostart = snapshot_provider is None if autostart is None else autostart   # real console: start the dispatcher
        self.show_whatsnew = snapshot_provider is None if whatsnew is None else whatsnew   # real console: new entries pop up
        self.dispatcher_note = None
        self.snapshot_provider = snapshot_provider or default_provider
        self.interval = interval or default_interval()
        self.data, self.error, self.window = None, None, 1   # window: index into WINDOWS, 24 hours first

    def compose(self) -> ComposeResult:
        yield Static(header_text(None), id="header")
        with TabbedContent(id="tabs"):
            for i, (tid, name) in enumerate(tabs()):
                with TabPane(f"{(i + 1) % 10} {name}", id=tid):
                    if tid in VIEWS:
                        yield VIEWS[tid]()
                    else:
                        yield Static(Text(LATER[tid], style="dim"))
        yield Footer()

    def on_mount(self):
        self.set_interval(self.interval, self.refresh_data)
        self.refresh_data()
        if self.autostart and C.DISPATCH.get("autostart", True) is not False:
            self.dispatcher_job("start")
        if self.show_whatsnew and C.CONFIG_DIR is not None and (new := whatsnew.unseen()):
            whatsnew.mark_seen()   # once per upgrade: shown now, not again on the next start
            self.push_screen(WhatsNewScreen(new))

    def action_whatsnew(self):
        if not isinstance(self.screen, WhatsNewScreen):
            self.push_screen(WhatsNewScreen(whatsnew.ENTRIES))

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("What's new", "the features added to pl, where to see them and a command to try",
                            self.action_whatsnew)

    @work(thread=True, group="dispatcher")
    def dispatcher_job(self, what):
        """start: start it when not running. toggle: start it, or ask first and stop it. stop: stop it. Never on the UI thread.
        With the manager on (the default; machine.toml [manager] enabled = false turns it off) and a profile whose
        autostart is not false, start on mount starts pl manager; D asks the manager to stop or restart THIS profile's
        dispatcher only (a stop holds until D again or pl manager restart NAME). pl manager stop stops the manager."""
        try:
            managed = manager.enabled() and C.DISPATCH.get("autostart", True) is not False
            if managed and what == "start":
                note = manager.start()
            elif managed and what == "stop":
                note = manager.restart(C.PROFILE_NAME, "stop")
            elif managed and not dispatch.dispatcher_running():
                note = manager.restart(C.PROFILE_NAME, "restart")
                if not manager.running():
                    note = manager.start()
            elif what == "toggle" and (managed or dispatch.dispatcher_running()):
                msg = (f'Stop the dispatcher of profile "{C.PROFILE_NAME}"? pl manager stops it and does not restart it '
                       f"until you press D again; running agents keep running, and so do the manager and other "
                       f"profiles' dispatchers (pl manager stop stops the manager)."
                       if managed else f'Stop the dispatcher of profile "{C.PROFILE_NAME}"? Ctrl-C goes to tmux window '
                       f"{C.TMUX_SESSION}:dispatch; running agents keep running.")
                self.call_from_thread(self.push_screen, ConfirmScreen(msg),
                                      lambda yes: yes and self.dispatcher_job("stop"))
                return
            else:
                note = dispatch.stop_dispatcher() if what == "stop" else dispatch.start_dispatcher()
        except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
            note = f"dispatcher: failed to {'stop' if what == 'stop' else 'start'} — {type(e).__name__}: {e}"
        if note:
            self.call_from_thread(self.show_dispatcher_note, note)

    def show_dispatcher_note(self, note):
        self.dispatcher_note = note
        self.query_one("#header", Static).update(header_text(self.data, self.error, note))
        self.refresh_data()

    def action_dispatcher(self):
        if C.CONFIG_DIR is None:
            self.notify("no profile loaded: nothing to start", markup=False)
            return
        self.dispatcher_job("toggle")

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self):
        """Runs off the UI thread; the only place the provider (board, gh, tmux) is called."""
        try:
            data, err = self.snapshot_provider(), None
        except RateLimited as e:
            data, err = None, e.note
        except SystemExit as e:
            data, err = None, f"refresh failed: {e}"
        except Exception as e:  # noqa: BLE001 - the console must survive a bad pass
            data, err = None, f"refresh failed: {type(e).__name__}: {e}"
        self.post_message(DataReady(data, err))

    async def on_data_ready(self, m: DataReady):
        if m.data is not None:
            self.data = m.data
        self.error = m.error
        try:
            await self.render_views()
        except Exception as e:  # noqa: BLE001 - a bad row must not kill the console
            self.error = f"render failed: {type(e).__name__}: {e}"
            self.query_one("#header", Static).update(header_text(self.data, self.error, self.dispatcher_note))

    async def render_views(self):
        self.query_one("#header", Static).update(header_text(self.data, self.error, self.dispatcher_note))
        if self.data is None:
            return
        rows = self.data["snapshot"]["rows"]
        self.query_one(DashboardView).show(self.data, self.window)
        self.query_one(NeedsView).show(self.data)
        self.query_one(PrsView).show(self.data)
        self.query_one(LoopsView).show(self.data)
        self.query_one(ActivityView).show(self.data)
        self.query_one(SubagentsView).show(self.data)
        await self.query_one(PipelineView).show(self.data)
        tabs = self.query_one(TabbedContent)
        n_prs = sum(1 for r in rows if r.get("pr"))
        n_cards = sum(1 for r in rows if r.get("card"))
        for tid, label in (("needs", f"2 Needs you {waiting_total(needs_groups(rows))}"),
                           ("pipeline", f"4 Pipeline {n_cards}"), ("prs", f"5 Pull requests {n_prs}")):
            tabs.get_tab(tid).label = label

    @property
    def active_tab(self):
        return self.query_one(TabbedContent).active

    def action_tab(self, tid):
        self.query_one(TabbedContent).active = tid
        pane = self.query_one(f"#{tid}", TabPane)
        target = next((w for w in pane.query("*") if w.focusable), None)
        if target is not None:
            target.focus()
        self.refresh_bindings()

    def on_tabbed_content_tab_activated(self, _):
        self.refresh_bindings()
        if self.active_tab == "subagents":
            self.query_one(SubagentsView).tick()
        if self.active_tab == "prs":
            self.query_one(PrsView).selected()
        if self.active_tab == "assistant":
            self.query_one(AssistantView).opened()

    def check_action(self, action, parameters):
        if action == "tab":
            return any(t[0] == parameters[0] for t in tabs())
        if action == "cycle_window":
            return self.active_tab == "dashboard"
        if action == "standup":
            return self.active_tab == "dashboard" and len(self.screen_stack) == 1
        if action == "review":
            return self.active_tab == "needs" and len(self.screen_stack) == 1
        return True

    async def action_cycle_window(self):
        self.window = (self.window + 1) % len(WINDOWS)
        if self.data is not None:
            self.query_one(DashboardView).show(self.data, self.window)

    def action_standup(self):
        if self.data is None:
            self.notify("no data yet: wait for the first refresh", markup=False)
            return
        self.push_screen(StandupScreen(self.data["snapshot"]))

    def action_review(self, what):
        self.query_one(NeedsView).review(what)

    def action_refresh(self):
        self.query_one(PrsView).clear_cache()
        board.fresh_next()
        self.refresh_data()
