"""The Ideas tab: ideas list, the harness interview, and the brief that fills in as you answer.
Harness turns, card creation and file writes run in thread workers; every text is rendered as plain rich Text."""
import json
import os
import shlex
import subprocess
import tempfile

from rich.text import Text
from textual.binding import Binding
from textual.markup import escape
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from pl import config as C
from pl import harnesses, ideas

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it
SKIP_TEXT = "(skipped: I do not know yet; ask something else)"
STATUS = {"interviewing": "interviewing", "approving": "creating the card…", "approved": "approved · in Inbox",
          "discarded": "discarded"}


class IdeaInput(TextArea):
    """enter sends, ctrl+j adds a new line."""

    class Send(Message):
        pass

    async def _on_key(self, event):
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Send())
            return
        if event.key == "ctrl+j":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class TextScreen(ModalScreen):
    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > Vertical { width: 70; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt, value=""):
        super().__init__()
        self.prompt, self.value = prompt, value

    def compose(self):
        with Vertical():
            yield Static(Text(self.prompt))
            yield Input(value=self.value, id="idea-title-input")
            yield Static(Text("enter save · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.dismiss(m.value.strip())

    def action_cancel(self):
        self.dismiss(None)


class AccountScreen(ModalScreen):
    DEFAULT_CSS = """
    AccountScreen { align: center middle; }
    AccountScreen > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, choices):
        super().__init__()
        self.choices = choices   # [(account, harness name)]

    def compose(self):
        with Vertical():
            yield Static(Text("Interview with which harness account?"))
            yield OptionList(*[Option(Text(f"{a}  ({h})"), id=a) for a, h in self.choices], id="idea-accounts")
            yield Static(Text("enter pick · esc cancel", style="dim"))

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, m):
        self.dismiss(m.option.id)

    def action_cancel(self):
        self.dismiss(None)


class IdeasView(Widget):
    DEFAULT_CSS = """
    IdeasView { height: 1fr; }
    IdeasView > Horizontal { height: 1fr; }
    #idea-list { width: 34; height: 1fr; border: round $panel-lighten-2; }
    #idea-center { width: 1fr; height: 1fr; border: round $warning; padding: 0 1; }
    #idea-scroll { height: 1fr; }
    #idea-input { height: 5; border: round $warning; }
    #idea-keys, #idea-status { height: 1; color: $text-muted; }
    #idea-brief { width: 54; height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    """
    BINDINGS = [Binding("n", "new", "new idea"), Binding("d", "discard", "discard"), Binding("r", "rename", "rename"),
                Binding("h", "harness", "harness"), Binding("e", "edit_brief", "edit brief"),
                Binding("A", "approve", "approve brief"), Binding("ctrl+s", "skip", "skip question")]

    def __init__(self):
        super().__init__()
        self.items, self.current, self.busy, self.account = [], None, False, None
        self.approving = False

    def compose(self):
        with Horizontal():
            yield OptionList(id="idea-list")
            with Vertical(id="idea-center"):
                with VerticalScroll(id="idea-scroll"):
                    yield Static("", id="idea-transcript")
                yield Static("", id="idea-status")
                yield IdeaInput(id="idea-input", soft_wrap=True, show_line_numbers=False)
                yield Static(Text("enter send · ctrl+j new line · ctrl+s skip question · e edit brief · "
                                  "A approve · h harness · n new · d discard · r rename", style="dim"), id="idea-keys")
            yield Static("", id="idea-brief")

    def on_mount(self):
        self.query_one("#idea-list").border_title = "Ideas"
        self.query_one("#idea-brief").border_title = "Brief · fills in as you answer"
        self._redraw()
        self.reload()

    # ---------- workers ----------

    def _bg(self, fn, group):
        """Run fn in a thread; any error becomes a notification (a worker that raises kills the app)."""
        def run():
            try:
                fn()
            except (SystemExit, Exception) as e:  # noqa: BLE001
                self.app.call_from_thread(self._failed, str(e))
        self.run_worker(run, thread=True, group=group)

    def _failed(self, msg):
        self.busy = False
        self._set_input(True)
        self.app.notify(msg, severity="error", markup=False)
        self._redraw()

    def reload(self, select=None):
        def fn():
            items = ideas.list_ideas()
            self.app.call_from_thread(self._fill, items, select)
        self._bg(fn, "idea-list")

    def _fill(self, items, select=None):
        self.items = items
        ol = self.query_one("#idea-list", OptionList)
        ol.clear_options()
        for i in items:
            q = sum(1 for e in i.get("transcript", []) if e.get("role") == "harness")
            line = STATUS.get(i.get("status"), str(i.get("status"))) + (f" · {q} question{'s' * (q != 1)}" if q else "")
            ol.add_option(Option(Text(f"{str(i.get('title'))[:60]}\n{line}"), id=i["id"]))
        ol.border_title = f"Ideas · {len(items)}"
        want = select or (self.current or {}).get("id")
        for n, i in enumerate(items):
            if i["id"] == want:
                ol.highlighted = n
                self.current = i
        self._redraw()

    def on_option_list_option_highlighted(self, m):
        if m.option_list.id == "idea-list" and m.option is not None:
            hit = next((i for i in self.items if i["id"] == m.option.id), None)
            if hit is not None and hit is not self.current:
                self.current = hit
                self._redraw()

    # ---------- rendering ----------

    def _redraw(self):
        idea = self.current
        t = Text()
        if idea is None:
            t.append("Describe an idea and press enter: the harness asks one question at a time.", style="dim")
        else:
            for e in idea.get("transcript", []):
                me = e.get("role") == "you"
                t.append(("you" if me else str(idea.get("harness") or "harness")) + "\n",
                         style="bold #f0b35a" if me else "bold #7fb2f0")
                t.append(str(e.get("text")) + "\n\n")
        self.query_one("#idea-transcript", Static).update(t)
        acct = (idea or {}).get("account") or self.account or harnesses.default_account()
        try:
            hname = (idea or {}).get("harness") or harnesses.account_harness(acct).name
        except SystemExit:
            hname = "?"
        self.query_one("#idea-center").border_title = f"Interview · {escape(str(hname))} ({escape(str(acct))})"
        self.query_one("#idea-status", Static).update(Text(self._status(idea), style="dim"))
        self.query_one("#idea-brief", Static).update(self._brief(idea))
        self.query_one("#idea-scroll", VerticalScroll).scroll_end(animate=False)
        self.refresh_bindings()

    def _status(self, idea):
        if self.busy:
            return "● the harness is thinking…"
        if idea is None:
            return ""
        if idea.get("status") != "interviewing":
            return STATUS.get(idea.get("status"), "")
        if ideas.is_clear(idea):
            return "the brief is clear: press A to approve it and start the spec"
        return "● waiting for your answer" if idea.get("question") else ""

    def _brief(self, idea):
        brief = (idea or {}).get("brief") or ideas.empty_brief()
        t = Text()
        for k in ideas.FIELDS:
            ok = ideas.field_clear(brief, k)
            label = ideas.LABELS[k].upper() + (f" · {len(brief[k])}" if k == "open_questions" and brief[k] else "")
            t.append(label, style="bold dim")
            t.append("  ✓\n" if ok else "  …\n", style="green" if ok else "#f0b35a")
            v = brief.get(k)
            for line in ([v] if isinstance(v, str) else [f"· {x}" for x in v or []]):
                if line:
                    t.append(str(line) + "\n", style="" if ok else "#f0b35a")
            t.append("\n")
        t.append(f"brief {ideas.clear_count(brief)} of {len(ideas.FIELDS)} clear\n", style="dim")
        if idea and idea.get("card_id"):
            t.append(f"card {str(idea['card_id'])[:8]} in Inbox\n", style="green")
        elif idea and ideas.is_clear(idea):
            t.append("A  approve brief, start spec\n", style="bold")
        else:
            t.append("A  approve brief (answer the open question first)\n", style="dim")
        t.append("e  edit the brief yourself", style="dim")
        return t

    def _set_input(self, on):
        box = self.query_one("#idea-input", IdeaInput)
        box.disabled = not on
        if on:
            box.focus()

    # ---------- the interview ----------

    def on_idea_input_send(self, _):
        text = self.query_one("#idea-input", IdeaInput).text.strip()
        if text:
            self._send(text)

    def _send(self, text):
        if self.busy or self.approving:
            return
        cur = self.current
        if cur is None or cur.get("status") != "interviewing":
            try:
                base, answer = ideas.new_idea(text, account=self.account), None
            except SystemExit as e:
                self.app.notify(str(e), severity="error", markup=False)
                return
        else:
            base, answer = cur, text
        self.busy = True
        self._set_input(False)
        self._redraw()

        def fn():
            new, err = ideas.turn(base, answer)
            if err is None:
                try:
                    ideas.save(new)
                except SystemExit:           # saved elsewhere meanwhile (the Assistant): show the newer draft
                    self.app.call_from_thread(self.reload, new["id"])
                    raise
            self.app.call_from_thread(self._turn_done, new, err)
        self._bg(fn, "idea-turn")

    def _turn_done(self, new, err):
        self.busy = False
        if err:
            self.app.notify(err, severity="error", markup=False)
        else:
            self.query_one("#idea-input", IdeaInput).text = ""
            self.current = new
            self.items = [new] + [i for i in self.items if i["id"] != new["id"]]
            self.reload(select=new["id"])
        self._set_input(True)
        self._redraw()

    def action_skip(self):
        if self.current and self.current.get("question") and self.current.get("status") == "interviewing":
            self._send(SKIP_TEXT)

    # ---------- list actions ----------

    def action_new(self):
        self.current = None
        self.query_one("#idea-list", OptionList).highlighted = None
        self._redraw()
        self._set_input(True)

    def _saved_update(self, **fields):
        cur = self.current
        if cur is None:
            self.app.notify("no idea selected", markup=False)
            return
        new = {**cur, **fields}
        gone = fields.get("status") == "discarded"

        def fn():
            ideas.save({**ideas.load(new["id"]), **fields})   # reload first: the Assistant may have saved it since
            self.app.call_from_thread(self.reload, None if gone else new["id"])
        self.current = None if gone else new
        self._bg(fn, "idea-save")
        self._redraw()

    def action_discard(self):
        if self.current and self.current.get("status") in ("approving", "approved"):
            self.app.notify("an approved idea is not discarded; its card is on the board", markup=False)
            return
        self._saved_update(status="discarded")

    def action_rename(self):
        if self.current is None:
            self.app.notify("no idea selected", markup=False)
            return

        def done(title):
            if title:
                self._saved_update(title=ideas.clean_title(title))
        self.app.push_screen(TextScreen("New title for this idea", str(self.current.get("title") or "")), done)

    def action_harness(self):
        try:
            choices = [(a, harnesses.account_harness(a).name) for a in C.PROFILES]
        except SystemExit as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        if not choices:
            self.app.notify("no harness accounts are configured", markup=False)
            return

        def done(account):
            if not account:
                return
            self.account = account
            if self.current is not None and self.current.get("status") == "interviewing":
                self._saved_update(account=account, harness=dict(choices)[account])
            else:
                self._redraw()
        self.app.push_screen(AccountScreen(choices), done)

    def action_edit_brief(self):
        cur = self.current
        if cur is None:
            self.app.notify("no idea selected", markup=False)
            return
        try:
            fd, tmp = tempfile.mkstemp(prefix=".brief-", suffix=".json", dir=ideas._dir())   # created 0600
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cur.get("brief") or ideas.empty_brief(), f, ensure_ascii=False, indent=2)
            try:
                with self.app.suspend():
                    _run([*shlex.split(os.environ.get("EDITOR") or "vi"), tmp])
                with open(tmp, encoding="utf-8") as f:
                    raw = json.load(f)
            finally:
                os.unlink(tmp)
        except ValueError as e:
            self.app.notify(f"the brief was not valid JSON; nothing changed ({e})", severity="error", markup=False)
            return
        except (SystemExit, Exception) as e:  # noqa: BLE001
            self.app.notify(str(e), severity="error", markup=False)
            return
        if not isinstance(raw, dict):
            self.app.notify("the brief must be a JSON object; nothing changed", severity="error", markup=False)
            return
        self._saved_update(brief=ideas.clean_brief(raw, cur.get("brief")))

    # ---------- approval ----------

    def check_action(self, action, parameters):
        if action == "approve":
            cur = self.current
            return bool(cur and cur.get("status") in ("interviewing", "approving") and ideas.is_clear(cur)
                        and not self.busy and not self.approving) or None
        return True

    def action_approve(self):
        cur = self.current
        if not (cur and cur.get("status") in ("interviewing", "approving") and ideas.is_clear(cur)) or self.approving:
            self.app.notify("answer the open question first", markup=False)
            return

        def fn():
            try:
                done = ideas.approve(cur)
            except (SystemExit, Exception) as e:  # noqa: BLE001
                try:
                    disk = ideas.load(cur["id"])
                except SystemExit:
                    disk = cur
                self.app.call_from_thread(self._approve_failed, disk, str(e))
                return
            self.app.call_from_thread(self._approved, done)
        self.approving = True
        self.current = {**cur, "status": "approving"}
        self._redraw()
        self._bg(fn, "idea-approve")

    def _approve_failed(self, disk, msg):
        self.approving = False
        if self.current is not None and self.current.get("id") == disk.get("id"):
            self.current = disk
        self.app.notify(msg, severity="error", markup=False)
        self._redraw()
        self.reload(select=disk.get("id"))

    def _approved(self, done):
        self.approving = False
        self.current = done
        if done.get("status") != "approved" or not done.get("card_id"):
            self.app.notify("another console is creating this card", markup=False)
            return self.reload(select=done["id"])
        self.app.notify(f"card {str(done.get('card_id'))[:8]} created in Inbox; the spec stage picks it up", markup=False)
        self.reload(select=done["id"])
