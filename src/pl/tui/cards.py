"""Pipeline tab: every card as a list grouped by board column (Done hidden; z shows it), its full text beside it, and
actions. Cards that need a person carry a red ! and n shows only those."""
import re
import shutil

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Markdown, OptionList, Static
from textual.worker import get_current_worker

from pl import agent_screen
from pl import config as C
from pl import confirm, move_agent, trackers
from pl.agents import jump_to_window
from pl.board import card, plan_order, plan_slice, sections
from pl.commands import adopt, done, drop, hold, move_to, restart, retry, undrop, unhold
from pl.tui.chrome import empty_state, show_first_heading, skip_headings
from pl.tui.live import NO_WINDOW, LiveScreen
from pl.tui.loops import COPIERS, _copy_run
from pl.tui.needs import detail, needs_groups, needs_me, needs_you_count, review_kind, start_review
from pl.tui.review import ConfirmScreen, ReviewScreen, confirm_and_approve, open_editor, review_file, safe_link, write_section
from pl.tui.subagents import TODO_MARK
from pl.util import ago, short_id

TITLE_WIDTH = 34   # two less than before: the state column starts with the ! marker
NARROW_NOW = 14


class FitTable(DataTable):
    """Under the app's -narrow class the `flex` column takes the width the others leave, and the `capped` columns stop
    growing with their text, so no column is cut at 80 columns. Wide, columns keep their own widths."""

    def __init__(self, flex, wide=None, capped=(), **kw):
        super().__init__(**kw)
        self.flex, self.wide, self.capped = flex, wide, capped

    def on_resize(self, _):
        narrow = self.app.has_class("-narrow")
        cols = {c.key.value: c for c in self.ordered_columns}
        for k in self.capped:
            cols[k].auto_width, cols[k].width = not narrow, NARROW_NOW if narrow else cols[k].width
        flex = cols[self.flex]
        if narrow:
            others = sum(c.get_render_width(self) for c in cols.values() if c is not flex)
            flex.auto_width, flex.width = False, max(10, self.content_size.width - others - 2)
        else:
            flex.auto_width, flex.width = self.wide is None, self.wide or flex.width
        self._require_update_dimensions = True
        self.refresh(layout=True)


STATE = {"review": ("your review", "#e0a040"), "manual": ("yours to do", "#5f9fff"), "working": ("working", "green"),
         "needs": ("needs you", "red"), "waiting": ("in review", "dim"), "queued": ("queued", "green")}


def card_state(r):
    if r["kind"] in ("review", "working", "needs"):
        return r["kind"]
    if r.get("col") == "Manual":
        return "manual"
    return "waiting" if r.get("col") == "PR open" else "queued"


def open_card_url(app, cid):
    """Open the card's web link in the browser, off the UI thread (building a tracker may touch the network)."""
    def run():
        url = trackers.get("tracker").url(cid)
        if safe_link(url):   # a board-supplied file: or javascript: link is never opened
            app.call_from_thread(app.open_url, url)
        else:
            app.call_from_thread(app.notify, "no web link for this card", markup=False)
    app.run_worker(run, thread=True, group="keys")


def header_summary(c, col):
    m, w = c.get("metadata") or {}, (c.get("metadata") or {}).get("worker") or {}
    is_gate = col == "Plan for review" or (
        col == "Spec ready" and bool((C.GATES or {}).get("spec")) and not m.get("spec_approved_at"))
    stage = w.get("stage") if w else None
    if not stage:
        for sname, sinfo in (getattr(C, "PIPELINE", None) or {}).get("stages", {}).items():
            if sinfo.get("column") == col:
                stage = sname
                break
        stages = {"Inbox": "spec", "Approved": "run", "In progress": "run"}
        stage = stage or stages.get(col) or (
            col if is_gate else ("design" if "DESIGN" in (c.get("description") or "") else "plan")
            if col == "Spec ready" else col)
    if is_gate:
        return stage, "you", "approve or send back"
    if col == "Manual":
        return stage, "you", "yours to do"
    if w and w.get("stage") and not w.get("dead") and not m.get("agent_error"):
        return stage, f"{stage} agent", "agent builds PLAN" if stage == "run" else f"agent writes {stage.upper()}"
    queued = col in ("Inbox", "Approved", "Spec ready") or (stage in ("spec", "plan", "design", "run") and col not in ("Done", "PR open", "Manual"))
    return stage, "nobody", "waits for a slot" if queued else "-"


def card_head(c, col, url, approved=None):
    """The plain header lines of the card screen: title, then stage/who/next, column, tags, assignee, links, updated, link, metadata."""
    m = c.get("metadata") or {}
    stage, who, next_step = header_summary(c, col)
    meta = ", ".join(f"{k}={str(v)[:40]}" for k, v in m.items())
    links = ", ".join(f"{l.get('pipeline')}:{short_id(l.get('id'))} ({l.get('relation', 'related')})"
                      for l in m.get("links") or [])
    t = Text(f"{c.get('title') or ''}\n", style="bold")
    t.append(f"{stage} · {who} · {next_step}\n")
    for label, value in (("column", col), *([("status", approved)] if approved else []),
                         ("tags", ", ".join(map(str, c.get("tags") or []))),
                         ("assignee", c.get("assigned_to_display") or c.get("assigned_to")),
                         ("links", links)):
        t.append(f"{label:<9}", style="dim")
        t.append(f"{value or '-'}\n")
    t.append(f"{'updated':<9}", style="dim")
    up = c.get("updated_at")
    t.append(f"{ago(up)}  " if up else "-\n")
    if up:
        t.append(f"{up}\n", style="dim")
    for label, value in (("link", url), ("metadata", meta)):
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
    BINDINGS = [Binding("escape", "close", "close"),
                Binding("y", "copy", "copy card text"), Binding("O", "open", "open in browser"),
                Binding("a", "approve", "approve"), Binding("x", "send_back", "send back"),
                Binding("p", "hold", "hold / release"), Binding("R", "restart", "restart agent"),
                Binding("d", "drop", "drop")]

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

    def check_action(self, action, parameters):
        if action in ("approve", "send_back"):
            m = (self.card or {}).get("metadata") or {}
            is_gate = (self.col == "Plan for review") or (
                self.col == "Spec ready" and bool((C.GATES or {}).get("spec")) and not m.get("spec_approved_at")
            )
            return True if is_gate else None
        if action in ("hold", "drop"):
            return True if self.col != "Done" else None
        if action == "restart":
            stage = (((self.card or {}).get("metadata") or {}).get("worker") or {}).get("stage")
            return True if (self.col != "Done" and bool(stage)) else None
        return True

    def _ask_then(self, question, job, failed):
        def run():
            try:
                msg = "\n".join(job())
            except (SystemExit, Exception) as e:
                msg = f"{failed}{e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)
            self.app.call_from_thread(self.action_close)
        self.app.push_screen(ConfirmScreen(question), lambda yes: yes and self.app.run_worker(run, thread=True, group="keys"))

    def action_approve(self):
        if self.card:
            kind = "spec" if self.col == "Spec ready" else "plan"
            confirm_and_approve(self.app, self.card["id"], str(self.card.get("title") or ""), kind, after=self.action_close,
                                agent_pending=bool(confirm.pending(self.card)))

    def action_send_back(self):
        if self.card:
            kind = "spec" if self.col == "Spec ready" else "plan"
            self.app.push_screen(ReviewScreen(kind, self.card["id"], ids=[self.card["id"]], notes_first=True))

    def action_hold(self):
        if not self.card:
            return
        head = f"{short_id(self.card['id'])}  {str(self.card.get('title') or '')[:50]}"
        if "parked" in (self.card.get("tags") or []):
            self._ask_then(f"Release the hold on {head}? The dispatcher takes it again.", lambda: unhold(self.card["id"]), "not released: ")
            return

        def reason(why):
            if why is not None:
                note = f" Reason: {w}" if (w := why.strip()) else ""
                self._ask_then(f"Hold {head}? The dispatcher starts no agent on it until you press p again (pl unhold).{note}",
                               lambda: hold(self.card["id"], w or None), "not held: ")
        self.app.push_screen(ReasonScreen(f"Hold {head}: why does it wait? (optional)"), reason)

    def action_drop(self):
        if not self.card:
            return
        head = f"{short_id(self.card['id'])}  {str(self.card.get('title') or '')[:50]}"

        def reason(why):
            if why is not None:
                note = f" Reason: {w}" if (w := why.strip()) else ""
                self._ask_then(f"Drop {head}? It moves to Done marked dropped, not done; u or pl undrop puts it back.{note}",
                               lambda: drop(self.card["id"], w or None), "not dropped: ")
        self.app.push_screen(ReasonScreen(f"Drop {head}: why is it no longer needed? (optional)"), reason)

    def action_restart(self):
        if not self.card:
            return
        stage = ((self.card.get("metadata") or {}).get("worker") or {}).get("stage")
        if not stage:
            self.app.notify("R restarts a card's agent; this card has none", markup=False)
            return
        head = f"{short_id(self.card['id'])}  {str(self.card.get('title') or '')[:50]}"
        self._ask_then(f"Restart the {stage} agent of {head}? pl stops it (Ctrl-C in its window) and the "
                       f"dispatcher starts the {stage} stage fresh (attempt 1).", lambda: restart(self.card["id"]), "not restarted: ")



class CardActions:
    """The card keys of the Pipeline list. The view gives _row(): the selected row, or None."""
    CARD_BINDINGS = [Binding("w", "live_view", "agent screen"), Binding("W", "jump", "agent window (tmux)"),
                     Binding("c", "open_card", "card in browser"),
                     Binding("t", "retry", "try again"), Binding("m", "move_agent", "move to account")]

    def action_live_view(self):
        r = self._row()
        if r is None:
            return
        w = r.get("worker") or {}
        if not w.get("pane"):
            self.app.notify("this card has no agent window", markup=False)
            return
        c = r.get("card") or {}
        self.app.push_screen(LiveScreen(w["pane"], f"{short_id(c.get('id'))}  {str(c.get('title') or '')[:50]} · {w.get('stage') or ''} agent"))

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


def card_rows(data):
    """(column, its card rows) in board order: Done and empty columns left out."""
    rows = [r for r in data["snapshot"]["rows"] if r.get("card")]
    return [(col, here) for col in C.COLUMNS if col != "Done" and (here := [r for r in rows if r.get("col") == col])]


def card_count(data):
    return sum(len(here) for _, here in card_rows(data))


class ColumnPicker(ModalScreen):
    """The board's columns but the card's own; enter picks one, esc cancels."""
    DEFAULT_CSS = """
    ColumnPicker { align: center middle; }
    ColumnPicker > Vertical { width: 60; height: auto; max-height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    ColumnPicker OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title, current):
        super().__init__()
        self.title_text, self.columns = title, [c for c in C.COLUMNS if c != current]

    def compose(self):
        with Vertical():
            yield Static(Text(self.title_text))
            yield OptionList(*self.columns)
            yield Static(Text("enter pick · esc cancel", style="dim"))

    def on_option_list_option_selected(self, m):
        self.dismiss(self.columns[m.option_index])

    def action_cancel(self):
        self.dismiss(None)


class ReasonScreen(ModalScreen):
    """One line of text: enter gives it (maybe empty), esc gives None."""
    DEFAULT_CSS = """
    ReasonScreen { align: center middle; }
    ReasonScreen > Vertical { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, message, placeholder="reason (optional)"):
        super().__init__()
        self.message, self.placeholder = message, placeholder

    def compose(self):
        with Vertical():
            yield Static(Text(self.message))
            yield Input(placeholder=self.placeholder)
            yield Static(Text("enter next · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.dismiss(m.value)

    def action_cancel(self):
        self.dismiss(None)


def drop_back(c):
    """The column undrop puts a dropped card back in: where it was dropped from, else Inbox."""
    back = (c.get("metadata") or {}).get("dropped_from")
    return back if back in C.COLUMNS and back != "Done" else "Inbox"


AGENT_LINES, AGENT_SECONDS = 20, 2
AGENT_COLOURS = {"stopped": "red", "waiting for you": "yellow", "working": "green"}


class CardsView(CardActions, Horizontal):
    DEFAULT_CSS = """
    CardsView { height: 1fr; }
    #cards-table { height: 1fr; border: round $primary; }
    #cards-side { width: 1fr; height: 1fr; }
    #cards-detail { height: auto; }
    #cards-agent { height: auto; max-height: 23; border-top: solid $primary; }
    #cards-body { height: 1fr; }
    """
    BINDINGS = [Binding("tab", "app.focus_next", "list / text"), *CardActions.CARD_BINDINGS,
                Binding("v", "move_column", "move to column"), Binding("f", "only_mine", "only mine / all"),
                Binding("o", "answer", "answer questions"), Binding("d", "drop", "drop"), Binding("h", "hand_off", "to Manual"),
                Binding("F", "done", "done"), Binding("e", "edit_input", "edit idea"), Binding("z", "show_done", "show Done"),
                Binding("u", "undrop", "undo drop"), Binding("R", "restart", "restart agent"),
                Binding("i", "adopt", "to pipeline"), Binding("p", "hold", "hold"),
                Binding("L", "plan_filter", "plan filter")]

    def __init__(self):
        super().__init__()
        self._rows, self._built, self._now, self._data = {}, [], {}, None
        self.only_mine = False   # f: list only the cards that wait on a person
        self.show_done = False   # z: a Done group from the last refresh's Done cards (no extra board read)
        self.plan = None   # L: only this plan's cards, ordered by slice
        self._bodies, self._pending = {}, {}   # card id → (updated_at, markdown or error); read again only when it changed
        self._shown = None
        self._last = None   # the cursor row before the last move: which way it travels
        self._agent = None   # (key, pane, text, word, why) last drawn in the Agent section

    def compose(self):
        t = FitTable("title", TITLE_WIDTH, ("now",), id="cards-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_column("id", key="id")
        t.add_column("title", width=TITLE_WIDTH, key="title")   # a long title ends in … so state and now stay on screen
        t.add_column("state", key="state")
        t.add_column("now", key="now")
        yield t
        with Vertical(id="cards-side", classes="panel"):
            yield Static(Text(""), id="cards-detail")
            yield Static(Text(""), id="cards-agent", markup=False)
            with VerticalScroll(id="cards-body"):
                yield Markdown("", open_links=False, id="cards-md")

    def on_mount(self):
        self.query_one("#cards-agent").display = False
        self.set_interval(AGENT_SECONDS, self._agent_tick)

    def show(self, data):
        self._now, self._data = data.get("now") or {}, data
        groups = card_rows(data)
        total, mine = sum(len(h) for _, h in groups), needs_you_count(data)
        if self.show_done and (finished := [r for r in data["snapshot"].get("done") or [] if r.get("card")]):
            groups = [*groups, ("Done", finished)]
        t = self.query_one(DataTable)
        if self.plan is not None:
            finished = data["snapshot"].get("done") or []
            here = sorted((r for r in [*data["snapshot"]["rows"], *finished]
                           if r.get("card") and plan_slice(r["card"])[0] == self.plan and (not self.only_mine or needs_me(r))),
                          key=lambda r: plan_order(r["card"]))
            groups = [(f"plan {self.plan}", here)] if here else []
            needs = " · needs you only" if self.only_mine else ""
            t.border_title = (f"Pipeline · plan {self.plan}{needs} · {len(here)} cards · L clears" if here
                              else f"Pipeline · no cards in plan {self.plan} · L clears")
        elif self.only_mine:
            groups = [(col, here) for col, h in groups if (here := [r for r in h if needs_me(r)])]
            t.border_title = f"Pipeline · needs you only · {sum(len(h) for _, h in groups)} of {total} cards · f shows all"
        else:
            t.border_title = f"Pipeline · {total} cards · {mine} need you (f only those) · " + (
                "z hides Done" if self.show_done else "Done is hidden (z shows it)")
        keep, keep_key = t.cursor_row, self._current()[0]
        self._rows, built = {}, []
        for col, here in groups:
            built.append((f"group:{col}", (Text("■", style="dim"), Text(f"{col.upper()} {len(here)}", style="bold"), Text(""), Text(""))))
            for r in here:
                cid = r["card"]["id"]
                if cid in self._rows:   # a duplicate row would raise DuplicateKey
                    continue
                self._rows[cid] = r
                m = r["card"].get("metadata") or {}
                if r.get("col") == "Done":   # dropped (with its reason) or done
                    label, colour = ("dropped", "yellow") if m.get("dropped_at") else ("done", "green")
                else:
                    label, colour = (r["approved"], "red" if r.get("blocked") or r.get("failed") or r.get("waiting") else "green") if r.get("approved") else STATE[card_state(r)]
                state = Text.assemble(("! ", "bold red") if needs_me(r) else "  ", (label, colour))   # ! = waits on a person
                line = str(m.get("drop_reason") or "") if r.get("col") == "Done" else (self._now.get(cid) or {}).get("line") or ""
                if self.plan is not None:
                    line = f"{r.get('col')} · {line}" if line else str(r.get("col"))
                built.append((cid, (Text(short_id(cid), style="dim"), Text(r["card"].get("title") or "", no_wrap=True, overflow="ellipsis"), state,
                                    Text(line, style="red" if line.startswith("error: ") else "dim"))))
        if not built:   # the title column is narrow, so the message takes one short line per row
            lines = ([f"No cards in plan {self.plan}."] if self.plan is not None else ["Nothing needs you."] if self.only_mine
                     else empty_state("cards"))
            for i, line in enumerate(lines):
                built.append((f"group:empty{i}", (Text("■" if i == 0 else "", style="dim"), Text(line, style="dim"), Text(""), Text(""))))
        rebuilt = [k for k, _ in built] != [k for k, _ in self._built]
        if not rebuilt:   # same rows, same order: change only the cells that differ
            for (rid, cells), (_, before) in zip(built, self._built):
                for col, new, was in zip(t.columns, cells, before):
                    if new != was:
                        t.update_cell(rid, col, new)
        else:
            t.clear()
            for rid, cells in built:
                t.add_row(*cells, key=rid)
            idx = t.get_row_index(keep_key) if keep_key in self._rows else min(max(keep, 1), t.row_count - 1)   # row 0 is a heading
            t.move_cursor(row=idx)
        self._last = skip_headings(t, self._rows.__contains__)
        if rebuilt:
            show_first_heading(t, lambda k: k in self._rows)
        self._built = built
        self._bodies = {k: v for k, v in self._bodies.items() if k in self._rows}
        self._detail_for_cursor()

    def _current(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._rows.get(key)

    def _row(self):
        return self._current()[1]

    def _stamp(self, key):
        return str(self._rows[key]["card"].get("updated_at") or "")

    def _detail_for_cursor(self):
        key, r = self._current()
        self.query_one("#cards-body").display = r is not None
        if r is None:
            self._paint(key, detail(None), "")
            return
        stamp = self._stamp(key)
        if self._bodies.get(key, (None,))[0] != stamp and self._pending.get(key) != stamp and self.app.active_tab == "cards":
            self._pending[key] = stamp   # the old text stays on screen until the new one arrives; read only when shown
            self._load(key, stamp)
        self._paint_card(key)
        if (self._agent or (None,))[0] != key:
            self._agent = None
            self._draw_agent(Text.assemble(("Agent", "bold"), "  ·  ", ("reading…", "dim")))
        self._agent_tick()

    def opened(self):
        """The tab was just shown: read the selected card's text now; on the first card, show its heading too."""
        show_first_heading(self.query_one(DataTable), lambda k: k in self._rows)
        self._detail_for_cursor()

    def _load(self, key, stamp):
        def run():   # one board read, off the UI thread
            try:
                hit = ("ok", card_markdown(card(key).get("description")))
            except (SystemExit, Exception) as e:  # noqa: BLE001 - RateLimited carries its own note
                hit = ("error", getattr(e, "note", None) or f"could not read the card: {e}")
            self.app.call_from_thread(self._loaded, stamp, key, hit)
        self.run_worker(run, thread=True, group="cards-body")

    def _draw_agent(self, text):
        agent = self.query_one("#cards-agent", Static)
        agent.display = True
        agent.update(text)

    def _agent_tick(self):
        if self.app.active_tab != "cards" or len(self.app.screen_stack) != 1:
            return
        key, r = self._current()
        if r is None:
            self.query_one("#cards-agent").display = False
            return
        pane = (r.get("worker") or {}).get("pane")
        if not pane:
            self._agent_show(key, pane, None, NO_WINDOW, "")
            return
        self.run_worker(lambda: self._agent_read(key, pane), thread=True, group="cards-agent", exclusive=True)

    def _agent_read(self, key, pane):
        result = agent_screen.read(pane, lines=AGENT_LINES)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._agent_show, key, pane, *result)

    def _agent_show(self, key, pane, text, word, why):
        if self._current()[0] != key or (shown := (key, pane, text, word, why)) == self._agent:
            return
        self._agent = shown
        status = f"{word}: {why}" if why else word
        out = Text.assemble(("Agent", "bold"), "  ·  ", (status, AGENT_COLOURS.get(word, "dim")))
        if text is not None:
            out.append("\n")
            out.append_text(Text.from_ansi(text, no_wrap=True, overflow="ellipsis"))
        self._draw_agent(out)

    def _loaded(self, stamp, key, hit):
        if self._pending.get(key) == stamp:
            del self._pending[key]
        if key not in self._rows or self._stamp(key) != stamp:   # the card changed again meanwhile
            return
        self._bodies[key] = (stamp, hit)
        if self._current()[0] == key:
            self._paint_card(key)

    def _paint_card(self, key):
        r = self._rows[key]
        text = detail(r, self._now.get(key))
        hit = self._bodies.get(key, (None, ("loading", "")))[1]
        if hit[0] != "ok":
            text.append("\n\n")
            text.append("loading…" if hit[0] == "loading" else hit[1], style="dim" if hit[0] == "loading" else "red")
        self._paint(key, text, hit[1] if hit[0] == "ok" else "")

    def _paint(self, key, text, md):
        """Touch the pane only when what it would show differs from what it shows."""
        shown = (key, md, text.plain)
        if shown == self._shown:
            return
        new_md = self._shown is None or self._shown[1] != md
        self._shown = shown
        self.query_one("#cards-detail", Static).update(text)
        if new_md:
            self.query_one("#cards-md", Markdown).update(md)

    def action_only_mine(self):
        """f: only the cards that wait on a person, or all of them again."""
        self.show_attention(not self.only_mine)

    def show_attention(self, on=True, group=None):
        """Turn the attention filter on or off and redraw from the last refresh; with a Needs-you card group (specs,
        plans, manual), select its first card."""
        self.only_mine = on
        if self._data is None:
            return
        self.show(self._data)
        t = self.query_one(DataTable)
        cid = next((k for k, r in self._rows.items() if group and needs_groups([r])[group]), None)
        if cid is not None:
            t.move_cursor(row=t.get_row_index(cid))

    def action_plan_filter(self):
        """L: ask for a plan number and list only that plan's cards by slice; L again shows everything."""
        if self.plan is not None:
            self.plan = None
        else:
            def picked(v):
                if v is None:
                    return
                if not re.fullmatch(r"[0-9]+", v.strip()) or int(v) == 0:
                    self.app.notify("the plan must be a whole number from 1", markup=False)
                    return
                self.plan = int(v)
                if self._data is not None:
                    self.show(self._data)
            self.app.push_screen(ReasonScreen("Show one plan: its number (esc cancels)", placeholder="plan number, e.g. 21"), picked)
            return
        if self._data is not None:
            self.show(self._data)

    def action_answer(self):
        self.review("answer")

    def review(self, what):
        """a / x / o on a spec or plan waiting for review; any other card only gets a notice."""
        r = self._row()
        if r and r.get("col") == "Spec ready" and (r["card"].get("metadata") or {}).get("pipeline_mode") != "auto":
            self.app.notify("this card is manual: press i to hand it to the pipeline (pl adopt)", markup=False)
            return
        kind = review_kind(r) if r else None
        start_review(self.app, what, r, kind, [k for k, x in self._rows.items() if review_kind(x) == kind])

    def action_move_column(self):
        """v: pick a column, confirm, then move the card off the UI thread."""
        r = self._row()
        if r is None:
            self.app.notify("select a card to move it to another column", markup=False)
            return
        c = r["card"]
        head = f"{short_id(c['id'])}  {str(c.get('title') or '')[:50]}"

        def run(col):
            try:
                msg = f"moved {head} to {col}" if move_to(c["id"], col) else f"not moved: {head}"
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"not moved: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)

        def picked(col):
            if col:
                self.app.push_screen(ConfirmScreen(f"Move {head} from {r.get('col')} to {col}?"),
                                     lambda yes: yes and self.app.run_worker(lambda: run(col), thread=True, group="keys"))
        self.app.push_screen(ColumnPicker(f"Move {head} to which column?", r.get("col")), picked)

    def _ask_then(self, question, job, failed):
        """Confirm, then job() off the UI thread; its lines (or the refusal after failed) become a notice."""
        def run():
            try:
                msg = "\n".join(job())
            except (SystemExit, Exception) as e:  # noqa: BLE001 - a failed write must not kill the console
                msg = f"{failed}{e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)
        self.app.push_screen(ConfirmScreen(question), lambda yes: yes and self.app.run_worker(run, thread=True, group="keys"))

    def _open_row(self, key, what):
        """The selected card when it is not in Done, else None after a notice."""
        r = self._row()
        if r is None:
            self.app.notify(f"select a card to {what} it", markup=False)
        elif r.get("col") == "Done":
            self.app.notify(f"{key} works on a card that is not in Done; u puts a dropped card back, v moves any card", markup=False)
        else:
            return r
        return None

    @staticmethod
    def _head(c):
        return f"{short_id(c['id'])}  {str(c.get('title') or '')[:50]}"

    @staticmethod
    def _agent_note(r, stops=False):
        if not (r.get("worker") or {}).get("window"):
            return ""
        return (" Its running agent is stopped first (Ctrl-C in its window)." if stops
                else " Its running agent is not stopped: stop it in its window (w).")

    def action_drop(self):
        """d: an optional reason, a confirm, then pl drop off the UI thread."""
        r = self._open_row("d", "drop")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])

        def reason(text):
            if text is None:
                return
            why = text.strip() or None
            self._ask_then(f"Drop {head}? It moves to Done marked dropped, not done; u or pl undrop puts it back."
                           + (f" Reason: {why}" if why else "") + self._agent_note(r, stops=True),
                           lambda: drop(c["id"], why), "not dropped: ")
        self.app.push_screen(ReasonScreen(f"Drop {head}: why is it no longer needed? (optional)"), reason)

    def action_hand_off(self):
        """h: confirm, then move the card to Manual: a person's to do, no agent starts on it."""
        r = self._open_row("h", "hand off")
        if r is None:
            return
        if r.get("col") == "Manual":
            self.app.notify(f"{short_id(r['card']['id'])} is already in Manual", markup=False)
            return
        c, head = r["card"], self._head(r["card"])
        self._ask_then(f"Move {head} from {r.get('col')} to Manual? It becomes yours to do; the dispatcher starts no agent on it."
                       + self._agent_note(r),
                       lambda: [f"moved {head} to Manual" if move_to(c["id"], "Manual") else f"not moved: {head}"], "not moved: ")

    def action_done(self):
        """F: confirm, then pl done off the UI thread."""
        r = self._open_row("F", "finish")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])
        self._ask_then(f"Mark {head} done? It moves from {r.get('col')} to Done as finished (not dropped)." + self._agent_note(r),
                       lambda: done(c["id"]), "not done: ")

    def action_undrop(self):
        """u on a dropped card in the Done group: confirm, then pl undrop off the UI thread."""
        r = self._row()
        if r is None or r.get("col") != "Done" or not (r["card"].get("metadata") or {}).get("dropped_at"):
            self.app.notify("u puts a dropped card back (z shows Done)", markup=False)
            return
        c = r["card"]
        self._ask_then(f"Put {self._head(c)} back to {drop_back(c)}? The drop is cleared.", lambda: undrop(c["id"]), "not put back: ")

    def action_restart(self):
        """R: confirm, then pl restart off the UI thread: its agent is stopped and the stage starts fresh."""
        r = self._open_row("R", "restart")
        if r is None:
            return
        c, stage = r["card"], (r.get("worker") or {}).get("stage")
        if not stage:
            self.app.notify("R restarts a card's agent; this card has none", markup=False)
            return
        self._ask_then(f"Restart the {stage} agent of {self._head(c)}? pl stops it (Ctrl-C in its window) and the "
                       f"dispatcher starts the {stage} stage fresh (attempt 1).", lambda: restart(c["id"]), "not restarted: ")

    def action_adopt(self):
        """i on a manual card: confirm, then pl adopt off the UI thread."""
        r = self._open_row("i", "hand to the pipeline")
        if r is None:
            return
        c = r["card"]
        if (c.get("metadata") or {}).get("pipeline_mode") == "auto":
            self.app.notify(f"{short_id(c['id'])} is already in the pipeline", markup=False)
            return
        self._ask_then(f"Hand {self._head(c)} to the pipeline (pl adopt)? The dispatcher starts the next stage's agent on it.",
                       lambda: adopt(c["id"]), "not adopted: ")

    def action_hold(self):
        """p: hold a card (an optional reason, then a confirm) or release a held one (a confirm); pl hold / unhold."""
        r = self._open_row("p", "hold")
        if r is None:
            return
        c, head = r["card"], self._head(r["card"])
        if "parked" in (c.get("tags") or []):
            self._ask_then(f"Release the hold on {head}? The dispatcher takes it again.", lambda: unhold(c["id"]), "not released: ")
            return

        def reason(text):
            if text is None:
                return
            why = text.strip() or None
            self._ask_then(f"Hold {head}? The dispatcher starts no agent on it until you press p again (pl unhold)."
                           + (f" Reason: {why}" if why else "") + self._agent_note(r),
                           lambda: hold(c["id"], why), "not held: ")
        self.app.push_screen(ReasonScreen(f"Hold {head}: why does it wait? (optional)"), reason)

    def action_show_done(self):
        """z: show or hide the Done group, from the last refresh."""
        self.show_done = not self.show_done
        if self._data is not None:
            self.show(self._data)

    def action_edit_input(self):
        """e: the card's INPUT (your idea) in $EDITOR, the same way the spec review's E opens the spec; written back
        only when it changed and the card's INPUT is still the text that was opened."""
        r = self._row()
        if r is None:
            self.app.notify("select a card to edit its idea", markup=False)
            return
        cid = r["card"]["id"]

        def read():   # one board read, off the UI thread
            try:
                c = card(cid)
                loaded = sections(c.get("description")).get("INPUT")
                if loaded is None:
                    raise SystemExit(f"{short_id(cid)} has no INPUT section")
                path = review_file("input", c)
                before = path.read_text()
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify, f"not opened: {e}", severity="error", markup=False)
                return
            self.app.call_from_thread(self._edit_input, cid, path, loaded, before)
        self.app.run_worker(read, thread=True, group="keys")

    def _edit_input(self, cid, path, loaded, before):
        open_editor(self.app, path)
        new = path.read_text() if path.is_file() else before
        if new == before:
            self.app.notify("INPUT not changed", markup=False)
            return

        def run():
            try:
                write_section(cid, "INPUT", loaded, new)
                path.unlink(missing_ok=True)   # saved: the next e starts from the card again
                msg = f"saved INPUT of {short_id(cid)}"
            except (SystemExit, Exception) as e:  # noqa: BLE001
                msg = f"{e}. Your edit is kept in {path}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
            self.app.call_from_thread(self.app.refresh_data)
        self.app.run_worker(run, thread=True, group="keys")

    def on_data_table_row_highlighted(self, _):
        self._last = skip_headings(self.query_one(DataTable), self._rows.__contains__, self._last)
        self.app.refresh_bindings()
        try:
            self._detail_for_cursor()
        except Exception as e:  # noqa: BLE001 - a bad card must not kill the console
            self.app.notify(f"render failed: {type(e).__name__}: {e}", markup=False)

    def on_data_table_row_selected(self, _):
        self.action_view_card()
