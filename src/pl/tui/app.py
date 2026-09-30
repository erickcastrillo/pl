"""PlApp: the pl console. One background refresh feeds every view; views never call the board, gh or tmux."""
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, Static, TabbedContent, TabPane

from pl import board, dispatch, manager
from pl import config as C
from pl.trackers.github import RateLimited
from pl.tui.chrome import TABS, header_text
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
         "subagents": SubagentsView}


REFRESH, GITHUB_REFRESH = 15, 60   # seconds between board refreshes; GitHub's GraphQL budget needs the slower one


def default_interval():
    """Seconds between board refreshes when none is given: 60 for a GitHub tracker, else 15."""
    return GITHUB_REFRESH if str((C.TRACKER or {}).get("type") or "").startswith("github") else REFRESH


def default_provider():
    """The real data for one refresh: board + tmux snapshot, event metrics per window, GitHub PR activity."""
    from pl import events, memory, watch
    board.share("read")   # reuse the dispatcher's board read while it is young
    return {"snapshot": watch.watch_snapshot(),
            "metrics_by_window": {key: events.metrics(seconds) for key, _, seconds in WINDOWS},
            "daily": {"specs": events.daily("Spec ready"), "plans": events.daily("Plan for review")},
            "pr_activity": watch.pr_activity(), "memory": memory.status(), "usage": _usage(),
            "machine": manager.read_status()}


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


class PlApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "pl"
    BINDINGS = [Binding(str(i + 1), f"tab('{tid}')", "views" if i == 0 else name, show=i == 0, key_display="1-9")
                for i, (tid, name) in enumerate(TABS)] + [
        Binding("w", "cycle_window", "window"), Binding("s", "standup", "standup"),
        Binding("a", "review('approve')", "approve"), Binding("x", "review('send_back')", "send back"),
        Binding("ctrl+x", "review('send_back')", "send back", key_display="^x"),
        Binding("D", "dispatcher", "dispatcher start/stop"), Binding("r", "refresh", "refresh"), Binding("q", "quit", "quit")]

    def __init__(self, snapshot_provider=None, interval=None, autostart=None):
        super().__init__()
        self.autostart = snapshot_provider is None if autostart is None else autostart   # real console: start the dispatcher
        self.dispatcher_note = None
        self.snapshot_provider = snapshot_provider or default_provider
        self.interval = interval or default_interval()
        self.data, self.error, self.window = None, None, 1   # window: index into WINDOWS, 24 hours first

    def compose(self) -> ComposeResult:
        yield Static(header_text(None), id="header")
        with TabbedContent(id="tabs"):
            for i, (tid, name) in enumerate(TABS):
                with TabPane(f"{i + 1} {name}", id=tid):
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

    @work(thread=True, group="dispatcher")
    def dispatcher_job(self, what):
        """start: start it when not running. toggle: start it, or ask first and stop it. stop: stop it. Never on the UI thread.
        On a managed machine (machine.toml exists) the same keys start and stop pl manager instead."""
        try:
            managed = (manager.machine_dir() / "machine.toml").exists()
            if what == "toggle" and (manager.running() if managed else dispatch.dispatcher_running()):
                msg = ("Stop pl manager? It stops restarting dispatchers; running dispatchers and agents keep running."
                       if managed else f'Stop the dispatcher of profile "{C.PROFILE_NAME}"? Ctrl-C goes to tmux window '
                       f"{C.TMUX_SESSION}:dispatch; running agents keep running.")
                self.call_from_thread(self.push_screen, ConfirmScreen(msg),
                                      lambda yes: yes and self.dispatcher_job("stop"))
                return
            if managed:
                note = manager.stop() if what == "stop" else manager.start()
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

    def check_action(self, action, parameters):
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
