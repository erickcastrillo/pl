"""Kanban tab: one column per board column (Done hidden), a card box per card; enter opens the whole card."""
import shutil

from rich.text import Text
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Label, Markdown, Static

from pl import config as C
from pl import move_agent, trackers
from pl.agents import jump_to_window
from pl.board import card, sections
from pl.commands import retry
from pl.tui.loops import COPIERS, _copy_run
from pl.tui.review import ConfirmScreen, safe_link
from pl.tui.subagents import TODO_MARK
from pl.util import short_id

STATE = {"review": ("your review", "#e0a040"), "manual": ("yours to do", "#5f9fff"), "working": ("working", "green"),
         "needs": ("needs you", "red"), "waiting": ("in review", "dim"), "queued": ("queued", "green")}


def card_state(r):
    if r["kind"] in ("review", "working", "needs"):
        return r["kind"]
    if r.get("col") == "Manual":
        return "manual"
    return "waiting" if r.get("col") == "PR open" else "queued"


class CardBox(Static, can_focus=True):
    def __init__(self, r, now=None):
        c = r["card"]
        label, colour = (r["approved"], "green") if r.get("approved") else STATE[card_state(r)]
        t = Text((c.get("title") or "") + "\n")
        t.append(short_id(c["id"]), style="dim")
        t.append(f"  {r.get('profile') or '-'}\n", style="dim")
        t.append(label, style=colour)
        if now and now.get("line"):
            t.append("\n" + now["line"], style="red" if now["line"].startswith("error: ") else "dim")
        super().__init__(t, classes="card", markup=False)   # no widget id: card ids can hold / and #
        self.row = r


def open_card_url(app, cid):
    """Open the card's web link in the browser, off the UI thread (building a tracker may touch the network)."""
    def run():
        url = trackers.get("tracker").url(cid)
        if safe_link(url):   # a board-supplied file: or javascript: link is never opened
            app.call_from_thread(app.open_url, url)
        else:
            app.call_from_thread(app.notify, "no web link for this card", markup=False)
    app.run_worker(run, thread=True, group="keys")


def card_head(c, col, url, approved=None):
    """The plain header lines of the card screen: title, then column, tags, assignee, updated, link, metadata."""
    meta = ", ".join(f"{k}={str(v)[:40]}" for k, v in (c.get("metadata") or {}).items())
    t = Text(f"{c.get('title') or ''}\n", style="bold")
    for label, value in (("column", col), *([("status", approved)] if approved else []), ("tags", ", ".join(map(str, c.get("tags") or []))),
                         ("assignee", c.get("assigned_to_display") or c.get("assigned_to")),
                         ("updated", c.get("updated_at")), ("link", url), ("metadata", meta)):
        t.append(f"{label:<9}", style="dim")
        t.append(f"{value or '-'}\n")
    return t


def card_markdown(description):
    """Every section in board order, the text before the first marker first."""
    return "\n\n".join(f"# {k}\n\n{v}" if k else v for k, v in sections(description).items()) or "(no description)"


def todo_markdown(now):
    """The agent's todo list with each item's state, as markdown to put above the card text; "" when it has none."""
    todos = (now or {}).get("todos") or []
    return "# Agent todos\n\n" + "\n".join(f"- {TODO_MARK.get(s, '[ ]')} {t}" for s, t in todos) + "\n\n" if todos else ""


class CardScreen(Screen):
    """The whole card, read-only. The card is read again in a worker; its text is data, never markup."""
    DEFAULT_CSS = """
    #card-head { height: auto; padding: 0 1; border: round $primary; }
    #card-doc { height: 1fr; border: round $primary; }
    """
    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", "close"),
                Binding("y", "copy", "copy card text"), Binding("O", "open", "open in browser")]

    def __init__(self, card_id, col, approved=None, now=None):
        super().__init__()
        self.card_id, self.col, self.card, self.approved, self.now = card_id, col, None, approved, now

    def compose(self):
        yield Static(Text(f"loading… {short_id(self.card_id)}", style="dim"), id="card-head", markup=False)
        with VerticalScroll(id="card-doc"):
            yield Markdown("", open_links=False, id="card-md")
        yield Footer()

    def on_mount(self):
        cid = self.card_id

        def run():
            try:
                c = card(cid)
                try:
                    url = trackers.get("tracker").url(cid)
                except Exception:  # noqa: BLE001 - no link is not an error
                    url = None
            except (SystemExit, Exception) as e:
                self.app.call_from_thread(self._failed, str(e))
                return
            self.app.call_from_thread(self._show, c, url)
        self.run_worker(run, thread=True, group="card")

    def _failed(self, msg):
        self.query_one("#card-head", Static).update(Text(f"could not read the card: {msg}", style="red"))

    async def _show(self, c, url):
        self.card = c
        self.query_one("#card-head", Static).update(card_head(c, self.col, url if safe_link(url) else None, self.approved))
        await self.query_one("#card-md", Markdown).update(todo_markdown(self.now) + card_markdown(c.get("description")))
        self.query_one("#card-doc").focus()

    def action_close(self):
        self.app.pop_screen()

    def action_copy(self):
        if self.card is None:
            return
        text = self.card.get("description") or ""
        self.app.copy_to_clipboard(text)
        for argv in COPIERS:
            if shutil.which(argv[0]):
                _copy_run(argv, text)
                break
        self.app.notify(f"copied {len(text):,} characters", markup=False)

    def action_open(self):
        open_card_url(self.app, self.card_id)

    def on_markdown_link_clicked(self, m):
        if safe_link(m.href):    # card text is untrusted: a file: or javascript: link is never opened
            self.app.open_url(m.href)
        else:
            self.app.notify("only web links open from here", markup=False)


class CardActions:
    """The card keys Kanban and the Pipeline list share. The view gives _row(): the selected row, or None."""
    CARD_BINDINGS = [Binding("w", "jump", "agent window"), Binding("c", "open_card", "card in browser"),
                     Binding("t", "retry", "try again"), Binding("m", "move_agent", "move to account")]

    def action_jump(self):
        r = self._row()
        if r is None:
            return
        win = (r.get("worker") or {}).get("window")
        self.app.run_worker(lambda: self.app.call_from_thread(self.app.notify, jump_to_window(win), markup=False), thread=True, group="keys")

    def action_retry(self):
        """t on a card whose agent died too often: confirm, then pl retry it off the UI thread."""
        r = self._row()
        if r is None or not r.get("failed"):
            self.app.notify("t retries a card whose agent died too often (marked needs you)", markup=False)
            return
        c = r["card"]

        def run():
            try:
                msg = "\n".join(retry(c["id"]))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"not reset: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def answered(yes):
            if yes:
                self.app.run_worker(run, thread=True, group="keys")
        self.app.push_screen(ConfirmScreen(f"Start {short_id(c['id'])}  {str(c.get('title') or '')[:50]} fresh? "
                                           "This clears its failed agent and attempt count."), answered)

    def action_move_agent(self):
        """m on a card with a live Claude agent: confirm, then move it to another healthy account off the UI thread."""
        r = self._row()
        w = (r or {}).get("worker") or {}
        if r is None or not w.get("window"):
            self.app.notify("m moves a card's live agent to another account", markup=False)
            return
        c = r["card"]

        def run():
            try:
                msg = move_agent.move_card(c["id"], None)   # it picks another healthy Claude account
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed move must not kill the console
                msg = f"not moved: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def answered(yes):
            if yes:
                self.app.run_worker(run, thread=True, group="keys")
        self.app.push_screen(ConfirmScreen(f"Move the agent of {short_id(c['id'])}  {str(c.get('title') or '')[:50]} from "
                                           f"{w.get('profile')} to another Claude account with credits? pl stops it with Ctrl-C in its window and "
                                           "resumes the same session there."), answered)

    def action_open_card(self):
        r = self._row()
        if r is not None:
            open_card_url(self.app, r["card"]["id"])

    def action_view_card(self):
        r = self._row()
        if r is not None:
            self.app.push_screen(CardScreen(r["card"]["id"], r.get("col"), r.get("approved"),
                                               ((self.app.data or {}).get("now") or {}).get(r["card"]["id"])))


class PipelineView(CardActions, Vertical):
    BINDINGS = [Binding("enter", "view_card", "open card"), *CardActions.CARD_BINDINGS]

    def compose(self):
        yield Label("Feature Pipeline", id="pipeline-title")
        yield HorizontalScroll(id="pipeline-scroll")

    async def show(self, data):
        rows = [r for r in data["snapshot"]["rows"] if r.get("card")]
        now = data.get("now") or {}
        cols = [c for c in C.COLUMNS if c != "Done"]
        focused = self.app.focused.row["card"]["id"] if isinstance(self.app.focused, CardBox) else None
        n_manual = sum(1 for r in rows if (r["card"].get("metadata") or {}).get("pipeline_mode") != "auto")
        self.query_one("#pipeline-title", Label).update(
            Text(f"Feature Pipeline · {len(rows) - n_manual} in the funnel · {n_manual} manual · Done is hidden"))
        scroll = self.query_one("#pipeline-scroll", HorizontalScroll)
        if repr((rows, now)) == getattr(self, "_shown", None):   # unchanged cards: the columns are not remounted
            return
        self._shown = repr((rows, now))
        await scroll.remove_children()
        widgets = []
        for col in cols:
            here = [r for r in rows if r.get("col") == col]
            body = [CardBox(r, now.get(r['card']['id'])) for r in here] or [Static(Text("(empty)", style="dim"), classes="empty")]
            widgets.append(Vertical(Label(Text(f"{col} ({len(here)})", style="bold")), VerticalScroll(*body), classes="pcol"))
        await scroll.mount_all(widgets)
        if focused is not None:
            again = [b for b in self.query(CardBox) if b.row["card"]["id"] == focused]
            if again:
                again[0].focus()

    def _row(self):
        f = self.app.focused
        return f.row if isinstance(f, CardBox) else None
