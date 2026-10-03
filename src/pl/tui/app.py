"""PlApp: the pl console. One background refresh feeds every view; views never call the board, gh or tmux."""
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, Tabs, TabbedContent, TabPane

from pl import board, dispatch, manager, update, whatsnew
from pl import config as C
from pl.trackers.github import RateLimited
from pl.tui.alerts import AlertsView
from pl.tui.assistant import AssistantView
from pl.tui.cards import CardsView, card_count
from pl.tui.chrome import KEY_NAMES, TABS, TAB_KEYS, header_text, tabs
from pl.tui.dashboard import WINDOWS, DashboardView, StandupScreen, copy_text
from pl.tui.ideas import IdeasView
from pl.tui.loops import ActivityView, LoopsView
from pl.tui.needs import NeedsView, needs_groups, waiting_total
from pl.tui.pipeline import PipelineView
from pl.tui.prs import PrsView
from pl.tui.review import ConfirmScreen
from pl.tui.settings import SettingsView
from pl.tui.skills import SkillsView
from pl.tui.subagents import SubagentsView

LATER = {}
VIEWS = {"dashboard": DashboardView, "needs": NeedsView, "ideas": IdeasView, "pipeline": PipelineView, "prs": PrsView,
         "loops": LoopsView, "activity": ActivityView, "settings": SettingsView,
         "subagents": SubagentsView, "assistant": AssistantView, "alerts": AlertsView, "cards": CardsView}


REFRESH, GITHUB_REFRESH = 15, 60   # seconds between board refreshes; GitHub's GraphQL budget needs the slower one


def default_interval():
    """Seconds between board refreshes when none is given: 60 for a GitHub tracker, else 15."""
    return GITHUB_REFRESH if str((C.TRACKER or {}).get("type") or "").startswith("github") else REFRESH


def default_provider():
    """The real data for one refresh: board + tmux snapshot, event metrics per window, GitHub PR activity."""
    from pl import events, ghquota, memory, watch
    ghquota.set_role("console")   # a periodic reader: it yields first when GitHub's budget runs low
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
    """Open alerts for the Alerts tab; an unreadable file never fails the refresh."""
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


class UpdateScreen(ModalScreen):
    """How to update pl: the commands from INSTALL.md. c copies them; pl never runs them."""
    DEFAULT_CSS = """
    UpdateScreen { align: center middle; }
    UpdateScreen > Vertical { width: 100%; max-width: 120; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("c", "copy", "copy"), Binding("escape", "close", "close")]

    def __init__(self, note):
        super().__init__()
        self.note = note

    def compose(self):
        with Vertical():
            yield Static(Text(self.note or f"pl v{update.__version__}", style="bold"))
            yield Static(Text(update.HOW + update.COMMANDS))
            yield Static(Text("c copy the commands · esc close · pl update prints them", style="dim"))

    def action_copy(self):
        copy_text(self.app, update.COMMANDS)
        self.app.notify("copied the update commands", markup=False)

    def action_close(self):
        self.dismiss(None)


class DispatcherChoice(ModalScreen):
    """D on a running managed dispatcher: r restart, s stop, esc nothing."""
    DEFAULT_CSS = """
    DispatcherChoice { align: center middle; }
    DispatcherChoice > Vertical { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("r", "pick('restart')", "restart"), Binding("s", "pick('stop')", "stop"),
                Binding("escape", "pick(None)", "cancel")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical():
            yield Static(Text(self.message))
            yield Static(Text("r restart · s stop · esc cancel", style="dim"))

    def action_pick(self, what):
        self.dismiss(what)


class PlApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "pl"
    BINDINGS = [Binding(KEY_NAMES.get(TAB_KEYS[tid], TAB_KEYS[tid]), f"tab('{tid}')", "views" if i == 0 else name,
                        show=i == 0, key_display="0-9 ! @")
                for i, (tid, name) in enumerate(TABS)] + [
        Binding(f"{mod}+{TAB_KEYS[tid]}", f"tab('{tid}')", name, show=False)   # work even while the Assistant box has focus
        for mod in ("ctrl", "alt") for tid, name in TABS if TAB_KEYS[tid].isdigit()] + [
        Binding("w", "cycle_window", "window"), Binding("s", "standup", "standup"),
        Binding("a", "review('approve')", "approve"), Binding("x", "review('send_back')", "send back"),
        Binding("ctrl+x", "review('send_back')", "send back", key_display="^x"),
        Binding("D", "dispatcher", "dispatcher (this profile)"), Binding("r", "refresh", "refresh"),
        Binding("question_mark", "whatsnew", "what's new", key_display="?"), Binding("U", "update", "update", show=False),
        Binding("q", "quit", "quit")]

    def __init__(self, snapshot_provider=None, interval=None, autostart=None, whatsnew=None, updates=None):
        super().__init__()
        self.autostart = snapshot_provider is None if autostart is None else autostart   # real console: start the dispatcher
        self.show_whatsnew = snapshot_provider is None if whatsnew is None else whatsnew   # real console: new entries pop up
        self.check_updates = snapshot_provider is None if updates is None else updates   # real console: daily tag check
        self.dispatcher_note = self.update_note = None
        self.snapshot_provider = snapshot_provider or default_provider
        self.interval = interval or default_interval()
        self.data, self.error, self.window = None, None, 1   # window: index into WINDOWS, 24 hours first

    def compose(self) -> ComposeResult:
        yield Static(header_text(None), id="header")
        with TabbedContent(id="tabs", initial="dashboard"):
            for tid, name in tabs():
                with TabPane(f"{TAB_KEYS[tid]} {name}", id=tid):
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
        if self.autostart and C.CONFIG_DIR is not None:   # real console: copy in missing or newer built-in skills
            self.install_builtins()
        if self.show_whatsnew and C.CONFIG_DIR is not None and (new := whatsnew.unseen()):
            whatsnew.mark_seen()   # once per upgrade: shown now, not again on the next start
            self.push_screen(WhatsNewScreen(new))
        if self.check_updates and update.enabled():
            self.update_job()

    @work(thread=True, group="update")
    def update_job(self):
        """Off the UI thread: the daily tag check (3 s at most). Any error is ignored."""
        try:
            if note := update.notice(update.check()):
                self.call_from_thread(self.show_update, note)
        except Exception:  # noqa: BLE001 - a worker that raises kills the app
            pass

    def show_update(self, note):
        self.update_note = note
        self.notify(note, timeout=30, markup=False)

    def action_update(self):
        if not isinstance(self.screen, UpdateScreen):
            self.push_screen(UpdateScreen(self.update_note))

    def install_builtins(self):
        from pl import skills
        try:
            for line in skills.install_builtins():
                if "kept" in line:   # only an update it did not apply needs you; the rest is routine
                    self.notify(line + " (pl skills reset to take it)", timeout=10)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a bad library folder or config must not stop the console
            self.notify(f"built-in skills not installed: {e}", timeout=10, markup=False)

    def action_whatsnew(self):
        if not isinstance(self.screen, WhatsNewScreen):
            self.push_screen(WhatsNewScreen(whatsnew.ENTRIES))

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("What's new", "the features added to pl, where to see them and a command to try",
                            self.action_whatsnew)
        yield SystemCommand("Skills: view, edit, create", "every account's skills, agents and commands (Settings tab)",
                            self.action_skills)

    def action_skills(self):
        """Settings tab, the Skills section, its list focused."""
        self.action_tab("settings")
        self.query_one(SkillsView).focus_table()

    @work(thread=True, group="dispatcher")
    def dispatcher_job(self, what):
        """start: start it when not running. toggle: start it, or ask first and stop it. stop: stop it. Never on the UI thread.
        With the manager on (the default; machine.toml [manager] enabled = false turns it off) and a profile whose
        autostart is not false, start on mount starts pl manager (and restarts one on an older pl build); D on THIS
        profile's dispatcher, read fresh from status.json: running offers restart or stop, stopped starts it."""
        try:
            managed = manager.enabled() and C.DISPATCH.get("autostart", True) is not False
            if managed and what == "start":
                note = manager.start()
                if old := manager.restart_if_old():
                    self.call_from_thread(self.notify, old, timeout=15, markup=False)
            elif managed and what in ("stop", "restart"):
                note = manager.restart(C.PROFILE_NAME, what, source="console-D")
            elif managed and not self.managed_up():
                note = manager.restart(C.PROFILE_NAME, "restart")
                if not manager.running():
                    note = manager.start()
            elif managed:
                msg = (f'Restart the dispatcher of profile "{C.PROFILE_NAME}"? Restart stops and starts it again; stop '
                       f"stops it until you press D again. Running agents keep running, and so do the manager and other "
                       f"profiles' dispatchers (pl manager stop stops the manager).")
                self.call_from_thread(self.push_screen, DispatcherChoice(msg), self.dispatcher_chosen)
                return
            elif what == "toggle" and dispatch.dispatcher_running():
                msg = (f'Stop the dispatcher of profile "{C.PROFILE_NAME}"? Ctrl-C goes to tmux window '
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

    def dispatcher_chosen(self, what):
        """The DispatcherChoice answer. r restarts at once; s asks once more, as a stop lasts until you restart it."""
        if what == "stop":
            self.push_screen(ConfirmScreen(f'Stop the dispatcher of {C.PROFILE_NAME}? It stays stopped until you '
                                           "restart it. y/n"), lambda yes: yes and self.dispatcher_job("stop"))
        elif what:
            self.dispatcher_job(what)

    @staticmethod
    def managed_up():
        """The manager's status, read now (never the refresh snapshot), runs this profile's dispatcher and no stop
        is pending: a D right after s starts it again, never stops it twice."""
        row = next((p for p in (manager.read_status() or {}).get("profiles") or [] if p.get("name") == C.PROFILE_NAME), {})
        return bool(row.get("running") and not row.get("stopped") and not manager.stop_pending(C.PROFILE_NAME))

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
        self.query_one(AlertsView).show(self.data)
        self.query_one(CardsView).show(self.data)
        await self.query_one(PipelineView).show(self.data)
        tabs = self.query_one(TabbedContent)
        n_prs = sum(1 for r in rows if r.get("pr"))
        n_cards = sum(1 for r in rows if r.get("card"))
        for tid, label in (("needs", f"2 Needs you {waiting_total(needs_groups(rows))}"),
                           ("pipeline", f"4 Kanban {n_cards}"), ("prs", f"5 Pull requests {n_prs}"),
                           ("alerts", f"! Alerts {len(self.data.get('alerts') or [])}"),
                           ("cards", f"@ Pipeline {card_count(self.data)}")):
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
            self.call_after_refresh(self.query_one(AssistantView).focus_box)
        if self.active_tab == "needs":
            self.query_one(NeedsView).opened()
        if self.active_tab == "cards":
            self.query_one(CardsView).opened()

    def on_descendant_focus(self, event):
        """A click on a tab leaves the focus on the tab bar, where typed letters run console commands (D, s, q, r ...)
        instead of reaching the Assistant's box: on the Assistant tab the box takes the focus back."""
        if isinstance(event.widget, Tabs) and self.active_tab == "assistant" and len(self.screen_stack) == 1:
            self.call_after_refresh(self.query_one(AssistantView).focus_box)

    def check_action(self, action, parameters):
        if action == "tab":
            return any(t[0] == parameters[0] for t in tabs())
        if action == "cycle_window":
            return self.active_tab == "dashboard"
        if action == "standup":
            return self.active_tab == "dashboard" and len(self.screen_stack) == 1
        if action == "review":
            return self.active_tab in ("needs", "cards") and len(self.screen_stack) == 1
        if action == "dispatcher":
            return len(self.screen_stack) == 1   # never a second D while its choice or confirm dialog is open
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
        self.query_one(CardsView if self.active_tab == "cards" else NeedsView).review(what)

    def action_refresh(self):
        self.query_one(PrsView).clear_cache()
        board.fresh_next()
        self.refresh_data()
