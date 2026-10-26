"""PlApp: the pl console. One background refresh feeds every view; views never call the board, gh or tmux."""
import os

from pathlib import Path

import json
import subprocess
import sys
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static, Tabs, TabbedContent, TabPane, TextArea
from textual.css.query import NoMatches

from pl import board, dispatch, manager, update, whatsnew
from pl import config as C
from pl.trackers.github import RateLimited
from pl.tui.alerts import AlertsView
from pl.tui.assistant import AssistantView
from pl.tui.brain import BrainView
from pl.tui.cards import CardsView, card_count
from pl.tui.connectors import ConnectorsPanel
from pl.tui.extensions import ExtensionsView
from pl.tui.chrome import GROUP_KEYS, GROUPS, HELP, groups, header_text, resolve, tabs
from pl.tui.needs import needs_you_count, review_kind
from pl.tui.dashboard import WINDOWS, DashboardView, StandupScreen, copy_text
from pl.tui.home import HomeView, read as summary_read
from pl.tui.ideas import IdeasView
from pl.tui.loops import ActivityView, LoopsView
from pl.tui.prs import PrsView
from pl.tui.review import ConfirmScreen
from pl.tui.panels import make_models_panel, make_pods_panel
from pl.tui.settings import SUBSCRIPTION_LINE, SettingsView
from pl.tui.skills import SkillsView
from pl.tui.subagents import SubagentsView

LATER = {}


def _titled(title, make):
    def build():
        pane = VerticalScroll(make(), classes="panel")
        pane.border_title = title
        pane.styles.height = "1fr"
        return pane
    return build


VIEWS = {"home": HomeView, "dashboard": DashboardView, "ideas": IdeasView, "prs": PrsView,
         "loops": LoopsView, "activity": ActivityView, "settings": SettingsView,
         "subagents": SubagentsView, "assistant": AssistantView, "alerts": AlertsView, "cards": CardsView,
         "brain": BrainView,
         "skills": _titled("Skills, agents and commands (every account)", SkillsView),
         "connectors": _titled("Connectors", ConnectorsPanel),
         "pods": _titled("Pods", make_pods_panel),
         "models": _titled("Local models", make_models_panel),
         "extensions": _titled("Extensions", lambda: ExtensionsView(SUBSCRIPTION_LINE))}

_run = subprocess.run   # the one subprocess runner here; tests fake it


REFRESH, GITHUB_REFRESH = 15, 60   # seconds between board refreshes; GitHub's GraphQL budget needs the slower one
BUILD_CHECK = 30   # seconds between checks for a newly installed pl build
DRAFTS = ("#assistant-input", "#idea-input", "#brain-input")   # boxes whose text is lost on a restart, focused or not


def default_interval():
    """Seconds between board refreshes when none is given: 60 for a GitHub tracker, else 15."""
    return GITHUB_REFRESH if str((C.TRACKER or {}).get("type") or "").startswith("github") else REFRESH


def default_provider():
    """The real data for one refresh: board + tmux snapshot, event metrics per window, GitHub PR activity."""
    from pl import events, ghquota, memory, outward, watch
    ghquota.set_role("console")   # a periodic reader: it yields first when GitHub's budget runs low
    board.share("read")   # reuse the dispatcher's board read while it is young
    snap = watch.watch_snapshot()
    return {"snapshot": snap,
            "metrics_by_window": {key: events.metrics(seconds) for key, _, seconds in WINDOWS},
            "daily": {"specs": events.daily("Spec ready"), "plans": events.daily("Plan for review")},
            "pr_activity": watch.pr_activity(), "memory": memory.status(), "usage": _usage(),
            "machine": manager.read_status(), "now": _now(snap["rows"]), "alerts": _alerts(),
            "kill": outward.state(),
            "home": [summary_read(d) for d in sorted(Path.home().glob(".pl-*"))]}


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
            yield Static(Text("esc close · the palette (ctrl+p) has What's new · pl whatsnew prints it", style="dim"))

    def action_close(self):
        self.dismiss(None)


class HelpScreen(ModalScreen):
    """What this screen is for, then every key active on it; esc closes."""
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 100%; max-width: 96; height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    #help-body { height: 1fr; }
    """
    BINDINGS = [Binding("escape", "close", "close"), Binding("question_mark", "help", "close", show=False)]

    def __init__(self, title, about, keys):
        super().__init__()
        self.title_text, self.about, self.keys = title, about, keys

    def compose(self):
        with Vertical():
            yield Static(Text(f"Help: {self.title_text}", style="bold"))
            with VerticalScroll(id="help-body"):
                yield Static(Text(self.about + "\n\n" + "\n".join(f"{k}  {what}" for k, what in self.keys)))
            yield Static(Text("esc close · the palette (ctrl+p) has What's new", style="dim"))

    def action_help(self):
        self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class UpdateScreen(ModalScreen):
    """How to update pl: the commands from INSTALL.md. c copies them; pl never runs them."""
    DEFAULT_CSS = """
    UpdateScreen { align: center middle; }
    UpdateScreen > Vertical { width: 100%; max-width: 120; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("y", "copy", "copy"), Binding("escape", "close", "close")]

    def __init__(self, note):
        super().__init__()
        self.note = note

    def compose(self):
        with Vertical():
            yield Static(Text(self.note or f"pl v{update.__version__}", style="bold"))
            yield Static(Text(update.HOW + update.COMMANDS))
            yield Static(Text("y copy the commands · esc close · pl update prints them", style="dim"))

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


NARROW_BELOW = 120   # columns: below this the list fills the width and the details pane gives way


class PlApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "pl"
    BINDINGS = [Binding(GROUP_KEYS[gid], f"tab('{gid}')", "views" if i == 0 else name, show=i == 0, key_display="1-8")
                for i, (gid, name, _) in enumerate(g for g in GROUPS if g[0] != "home")] + [
        Binding(f"{mod}+{GROUP_KEYS[gid]}", f"tab('{gid}')", name, show=False)   # work even while the Assistant box has focus
        for mod in ("ctrl", "alt") for gid, name, _ in GROUPS if gid != "home"] + [
        Binding("H", "tab('home')", "pipelines", show=False),
        Binding("left_square_bracket", "cycle_window(-1)", "earlier window", key_display="["),
        Binding("right_square_bracket", "cycle_window(1)", "later window", key_display="]"), Binding("s", "standup", "standup"),
        Binding("a", "review('approve')", "approve"), Binding("x", "review('send_back')", "send back"),
        Binding("ctrl+x", "review('send_back')", "send back", key_display="^x"),
        Binding("D", "dispatcher", "dispatcher (this profile)"), Binding("r", "refresh", "refresh"),
        Binding("K", "kill_switch", "kill switch"),
        Binding("question_mark", "help", "help", key_display="?"), Binding("U", "update", "update", show=False),
        Binding("ctrl+k", "switcher", "switch", priority=True),
        Binding("exclamation_mark", "oldest", "oldest waiting", key_display="!"),
        Binding("N", "restart_console", "restart on the new pl", show=False),
        Binding("q", "quit", "quit")]

    def __init__(self, snapshot_provider=None, interval=None, autostart=None, whatsnew=None, updates=None,
                 restarts=None, open_card=None, tab=None):
        super().__init__()
        self.autostart = snapshot_provider is None if autostart is None else autostart   # real console: start the dispatcher
        self.show_whatsnew = snapshot_provider is None if whatsnew is None else whatsnew   # real console: new entries pop up
        self.check_updates = snapshot_provider is None if updates is None else updates   # real console: daily tag check
        self.check_build = snapshot_provider is None if restarts is None else restarts   # real console: restart on a new install
        self.dispatcher_note = self.update_note = None
        self.restart_into = self.pending_build = None   # restart_into: the build to exec after run() returns
        self.snapshot_provider = snapshot_provider or default_provider
        self.interval = interval or default_interval()
        self.open_card = open_card
        self.initial_tab = tab
        self.data, self.error, self.last_good_at, self.window = None, None, None, 1   # window: index into WINDOWS, 24 hours first

    def compose(self) -> ComposeResult:
        yield Static(header_text(None), id="header")
        with TabbedContent(id="tabs", initial="g-dashboard"):
            for gid, label, views in groups():
                with TabPane(f"{GROUP_KEYS[gid]} {label}" if gid != "home" else label, id=f"g-{gid}"):
                    with TabbedContent(id=f"{gid}-views", classes="-single" if len(views) == 1 else ""):
                        for vid, sub_label in views:
                            with TabPane(sub_label, id=vid):
                                if vid in VIEWS:
                                    yield VIEWS[vid]()
                                else:
                                    yield Static(Text(LATER[vid], style="dim"))
        yield Footer()

    def on_resize(self, event):
        self.set_class(event.size.width < NARROW_BELOW, "-narrow")

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
        if self.check_build:
            update.build_id()   # this console's build, read now: a later install is a new build
            self.set_interval(BUILD_CHECK, self.build_job)
        if self.open_card:
            from pl.tui.cards import CardScreen
            self.push_screen(CardScreen(self.open_card, None))
        if self.query("#home-views"):
            self.query_one("#tabs", TabbedContent).hide_tab("g-home")
        if self.initial_tab:
            self.action_tab(self.initial_tab)

    @work(thread=True, exclusive=True, group="build")
    def build_job(self):
        """Off the UI thread: a newly installed pl build that imports (one that does not is told once and kept out)."""
        try:
            b = self.pending_build or update.restart_build(
                warn=lambda m: self.call_from_thread(self.notify, m, timeout=30, markup=False))
        except Exception:  # noqa: BLE001 - a worker that raises kills the app
            return
        if b:
            self.call_from_thread(self.new_build, b)

    def new_build(self, b):
        """Restart now when nothing would be lost; else say so once and try again at the next check."""
        if self.idle_safe():
            self.restart_now(b)
        elif self.pending_build != b:
            self.pending_build = b
            self.notify("pl was updated: press N to restart the console on the new pl now (it restarts by itself once no dialog "
                        "or unsent text is open)", timeout=30, markup=False)

    def idle_safe(self):
        """No dialog open, and no unsent text: in the focused box or in the Assistant's or Ideas' box."""
        if len(self.screen_stack) > 1:
            return False
        boxes = [self.focused, *(w for sel in DRAFTS for w in self.query(sel))]
        return not any((getattr(w, "value", None) or getattr(w, "text", None) or "").strip()
                       for w in boxes if isinstance(w, (Input, TextArea)))

    def restart_now(self, b):
        """Leave the app (the terminal is restored); pl watch then execs the same command line on the new build."""
        self.restart_into = b
        self.exit()

    def switch_to(self, profile, card=None, tab=None):
        self.restart_into = ("profile", profile, card, tab)
        self.exit()

    def action_restart_console(self):
        if self.pending_build:
            self.restart_now(self.pending_build)
        else:
            self.notify("no new pl build installed: nothing to restart", markup=False)

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

    def action_help(self):
        if isinstance(self.screen, HelpScreen):
            return
        keys, seen = [], set()
        for key, active in self.screen.active_bindings.items():
            b = active.binding
            if active.enabled and b.show and b.description and b.description not in seen:
                seen.add(b.description)
                keys.append((b.key_display or key, b.description))
        title = dict(tabs()).get(self.active_tab, self.active_tab)
        self.push_screen(HelpScreen(title, HELP.get(self.active_tab, ""), keys))

    def action_switcher(self):
        from pl.tui.switcher import SwitcherScreen
        if not isinstance(self.screen, SwitcherScreen):
            self.push_screen(SwitcherScreen(self.data))

    @work(thread=True, group="inbox")
    def action_oldest(self):
        try:
            r = _run([sys.executable, "-m", "pl", "inbox", "--json"],
                     capture_output=True, text=True, timeout=10)
            items = json.loads(r.stdout) if r.returncode == 0 else []
            if isinstance(items, dict):
                items = items.get("items") or items.get("cards") or []
        except Exception:
            items = []
        if not items:
            self.call_from_thread(self.notify, "nothing waits on you", markup=False)
            return
        first = items[0]
        cid = first.get("card") or first.get("id") or first.get("card_id")
        prof = first.get("profile")
        if prof and prof != C.PROFILE_NAME:
            self.call_from_thread(self.switch_to, prof, card=cid)
        else:
            from pl.tui.cards import CardScreen
            self.call_from_thread(self.push_screen, CardScreen(cid, None))

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("What's new", "the features added to pl, where to see them and a command to try",
                            self.action_whatsnew)
        yield SystemCommand("Skills: view, edit, create", "every account's skills, agents and commands (Extend tab)",
                            self.action_skills)
        yield SystemCommand("Kill switch", "turn the machine-wide kill switch on or off (holds all outward actions)",
                            self.action_kill_switch)
        yield SystemCommand("Brain: ask questions across pipelines", "one read-only brain session per machine",
                            self.action_brain)

    def action_brain(self):
        """Brain tab, one machine-wide brain session."""
        self.action_tab("brain")

    def action_skills(self):
        """Extend tab, the Skills sub-tab, its list focused."""
        self.action_tab("skills")
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
        self.query_one("#header", Static).update(header_text(self.data, self.error, note, self.last_good_at))
        self.refresh_data()

    def action_dispatcher(self):
        if C.CONFIG_DIR is None:
            self.notify("no profile loaded: nothing to start", markup=False)
            return
        self.dispatcher_job("toggle")

    def action_kill_switch(self):
        from pl import outward
        cur = outward.state()
        on = cur.get("on")
        msg = ("Turn off the machine-wide kill switch? Held outward actions will be released."
               if on else
               "Turn on the machine-wide kill switch? Outward actions on all profiles will be held.")

        def done(yes):
            if yes:
                user = getattr(C, "USER_EMAIL", None) or os.environ.get("USER") or "console"
                outward.set(not on, by=user)
                try:
                    from pl import events
                    events.emit("kill_switch", state="off" if on else "on", action="off" if on else "on", by=user)
                except Exception:
                    pass
                self.notify(f"kill switch {'off' if on else 'on'}", markup=False)
                self.refresh_data()

        self.push_screen(ConfirmScreen(msg), done)

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
            self.data, self.last_good_at = m.data, time.strftime("%H:%M")
        self.error = m.error
        try:
            await self.render_views()
        except Exception as e:  # noqa: BLE001 - a bad row must not kill the console
            self.error = f"render failed: {type(e).__name__}: {e}"
            self.query_one("#header", Static).update(header_text(self.data, self.error, self.dispatcher_note, self.last_good_at))

    async def render_views(self):
        self.query_one("#header", Static).update(header_text(self.data, self.error, self.dispatcher_note, self.last_good_at))
        if self.data is None:
            return
        rows = self.data["snapshot"]["rows"]
        if self.query(HomeView):
            self.query_one(HomeView).show(self.data)
        self.query_one(DashboardView).show(self.data, self.window)
        self.query_one(PrsView).show(self.data)
        self.query_one(LoopsView).show(self.data)
        self.query_one(ActivityView).show(self.data)
        self.query_one(SubagentsView).show(self.data)
        self.query_one(AlertsView).show(self.data)
        self.query_one(CardsView).show(self.data)
        failed = (self.error or "").splitlines()[0] if self.error else ""
        for table in ("#cards-table", "#prs-table"):
            self.query_one(table).border_subtitle = failed if failed.startswith("refresh failed") else ""
        n_prs = sum(1 for r in rows if r.get("pr"))
        mine = needs_you_count(self.data)
        for group, tid, label in (("work", "cards", f"Board {card_count(self.data)}" + (f" · {mine} need you" if mine else "")),
                                  ("review", "prs", f"Pull requests {n_prs}"),
                                  ("activity", "alerts", f"Alerts {len(self.data.get('alerts') or [])}")):
            self.query_one(f"#{group}-views", TabbedContent).get_tab(tid).label = label

    @property
    def active_tab(self):
        try:
            outer = self.query_one("#tabs", TabbedContent).active
            return self.query_one(f"#{outer.removeprefix('g-')}-views", TabbedContent).active
        except NoMatches:
            return None

    def action_tab(self, name):
        if (place := resolve(name)) is None:
            return
        gid, view = place
        self.query_one("#tabs", TabbedContent).active = f"g-{gid}"
        if view:
            self.query_one(f"#{gid}-views", TabbedContent).active = view
        pane = self.query_one(f"#{self.active_tab}", TabPane)
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
        if self.active_tab == "brain":
            self.query_one(BrainView).opened()
            self.call_after_refresh(self.query_one(BrainView).focus_box)
        if self.active_tab == "cards":
            self.query_one(CardsView).opened()

    def on_descendant_focus(self, event):
        """A click on a tab leaves the focus on the tab bar, where typed letters run console commands (D, s, q, r ...)
        instead of reaching the Assistant's box: on the Assistant tab the box takes the focus back."""
        if isinstance(event.widget, Tabs) and self.active_tab in ("assistant", "brain") and len(self.screen_stack) == 1:
            view = self.query_one(BrainView if self.active_tab == "brain" else AssistantView)
            self.call_after_refresh(view.focus_box)

    def check_action(self, action, parameters):
        if action == "tab":
            return resolve(parameters[0]) is not None
        if action == "cycle_window":
            return self.active_tab == "dashboard"
        if action == "standup":
            return self.active_tab == "dashboard" and len(self.screen_stack) == 1
        if action == "review":
            return self.active_tab == "cards" and len(self.screen_stack) == 1 and self.selected_waits_for_review()
        if action == "dispatcher":
            return len(self.screen_stack) == 1   # never a second D while its choice or confirm dialog is open
        if action == "kill_switch":
            return len(self.screen_stack) == 1
        return True

    def selected_waits_for_review(self):
        """A spec or plan at its gate, or a manual spec card (a / x then say how to hand it to the pipeline)."""
        try:
            row = self.query_one(CardsView)._row()
        except NoMatches:
            return False
        return bool(row) and (review_kind(row) is not None or row.get("col") == "Spec ready")

    async def action_cycle_window(self, step):
        self.window = (self.window + step) % len(WINDOWS)
        if self.data is not None:
            self.query_one(DashboardView).show(self.data, self.window)

    def action_standup(self):
        if self.data is None:
            self.notify("no data yet: wait for the first refresh", markup=False)
            return
        self.push_screen(StandupScreen(self.data["snapshot"]))

    def action_review(self, what):
        self.query_one(CardsView).review(what)

    def action_refresh(self):
        self.query_one(PrsView).clear_cache()
        board.fresh_next()
        self.refresh_data()
