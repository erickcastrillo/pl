"""The Assistant tab: the profile's assistant harness session as a chat, built from its transcript, and a box that
types into it. The harness's own menus (permission prompts, questions) show as a card; a number key answers one.
tmux and file reads run only in thread workers, once a second while this tab is active, never otherwise."""
import time
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from pl import agents, assistant, ideas
from pl import config as C
from pl.trackers import mcp
from pl.tui import subagents
from pl.tui.review import ConfirmScreen
from pl.util import mask, short_id

BOOT = "You are the pl assistant for profile"     # pl's first prompt (assistant.first_prompt): not shown
HIDDEN = ("<command-", "<local-command-", "<system-reminder>")
SCREEN_LINES = 15
MOVED = 3          # seconds: with no session registry, a screen that changed this recently counts as thinking
SLOW = 5           # seconds: a screen read running this long shows "still reading…" and a new read may start
CHANGED = "the question changed; look again"
KEYS = ("enter send · a number answers the card · ctrl+t chat/idea mode · "
        "ctrl+o open its window (arrows, Esc) · ctrl+r start over · ctrl+f file a ready idea · "
        "esc then 1-9 · switch tab")
HINT = {"chat": "chat mode: ask pl to do things (it asks before acting)",
        "idea": "idea mode: drafting an idea brief with you; it files the card only after your yes"}


def _tool_line(b):
    """One grey line for a tool call: "↳ Bash: pl list", "↳ Read assistant.md"; its input masked, its result left out."""
    name, inp = str(b.get("name") or "tool"), mcp._mask(b.get("input"), sorted(mcp._secrets, key=len, reverse=True))
    if isinstance(inp, dict) and name == "Bash" and isinstance(inp.get("command"), str):
        what = f"Bash: {inp['command']}"
    elif isinstance(inp, dict) and isinstance(inp.get("file_path"), str):
        what = f"{name} {Path(inp['file_path']).name}"
    else:
        what = f"{name} {subagents._short_input(inp)}"
    return "↳ " + subagents._cut(mask(what), 100)


def chat_items(entries):
    """[(timestamp, kind, text)] oldest first; kind is you, assistant, tool or error. Every text is masked."""
    out = []
    for e in entries:
        role = (e.get("message") or {}).get("role")
        if e.get("isMeta") or role not in ("user", "assistant"):
            continue
        ts, err = str(e.get("timestamp") or "")[:19], e.get("isApiErrorMessage") or e.get("error")
        for b in subagents._blocks(e):
            kind = b.get("type")
            if kind == "text":
                text = mask(b.get("text") or "").strip()
                if text and not (role == "user" and (BOOT in text or text.startswith(HIDDEN))):
                    out.append((ts, "error" if err else "you" if role == "user" else "assistant", text))
            elif kind == "tool_use":
                out.append((ts, "tool", _tool_line(b)))
            elif kind == "tool_result" and b.get("is_error"):
                out.append((ts, "error", "↳ " + (subagents._first_line(b.get("content"))[:200] or "the tool failed")))
    return out


def render_chat(items, updated_at=None):
    """The conversation as Ideas-style turns; the update notice sits where the restart happened."""
    t, last, note = Text(), None, str(updated_at or "")[:19] or None
    for ts, kind, text in items:
        if note and ts > note:
            t.append(assistant.UPDATED + "\n\n", style="italic green")
            note = None
        if kind in ("you", "assistant"):
            if last in ("tool", "error"):
                t.append("\n")
            t.append(kind + "\n", style="bold #f0b35a" if kind == "you" else "bold #7fb2f0")
            t.append(text + "\n\n")
        else:
            t.append(text + "\n", style="dim" if kind == "tool" else "red")
        last = kind
    if note:
        t.append(assistant.UPDATED + "\n", style="italic green")
    return t


def _transcript(st):
    """(path, stat) of the assistant's Claude transcript under its account's projects folder, else None."""
    acct = st.get("account")
    if st.get("harness") != "claude" or acct not in C.PROFILES:
        return None
    return subagents._transcript(Path(C.PROFILES[acct]).expanduser(), st.get("session_id"))


def menu_card(m):
    t = Text("needs your answer\n", style="bold")
    for x in m["title"]:
        t.append(x + "\n")
    for d, label in m["options"]:
        t.append(f" {d} ", style="bold reverse")
        t.append(f"{label}   ")
    t.append("\npress a number to answer · pl never answers for you", style="dim")
    return t


class AssistantInput(Input):
    """While a menu card shows, a number key in the empty box answers it at once (that one digit, no Enter).
    options: the card's [(digit, label)]; the answer carries them so a changed question is never answered."""
    options = ()

    class Answer(Message):
        def __init__(self, digit, options):
            super().__init__()
            self.digit, self.options = digit, options

    async def _on_key(self, event):
        if not self.value and event.character and event.character in (d for d, _ in self.options):
            event.stop()
            event.prevent_default()
            self.post_message(self.Answer(event.character, list(self.options)))
            return
        await super()._on_key(event)


class AssistantView(Widget):
    DEFAULT_CSS = """
    AssistantView { height: 1fr; }
    #assistant-center { height: 1fr; border: round $warning; padding: 0 1; }
    #assistant-scroll { height: 1fr; }
    #assistant-menu { height: auto; border: round $warning; background: $warning 15%; padding: 0 1; }
    #assistant-status, #assistant-keys { height: 1; color: $text-muted; }
    #assistant-warning { height: auto; color: $warning; }
    #assistant-input { border: round $warning; }
    """
    BINDINGS = [Binding("ctrl+t", "mode", "chat/idea"), Binding("ctrl+o", "open_window", "open window"),
                Binding("ctrl+r", "restart", "start over"), Binding("ctrl+f", "file_idea", "file idea"),
                Binding("escape", "leave_box", "leave box", show=False)]

    def __init__(self):
        super().__init__(id="assistant-view")
        self.started, self.mode, self.status = False, "chat", "opening the tab starts the assistant"
        self.ready = None     # the idea the assistant marked ready to file, shown until it is filed
        self.activity, self.redraws = None, 0
        self._read_started, self._seq, self._shown = None, 0, 0   # the newest read: when it began, its number; shown
        self._key, self._lines, self._moved = None, None, 0.0   # what the chat was drawn from; the screen, when it moved

    def compose(self):
        yield Static("", id="assistant-warning")
        with Vertical(id="assistant-center"):
            with VerticalScroll(id="assistant-scroll") as scroll:
                scroll.can_focus = False     # opening the tab focuses the box, the first focusable widget
                yield Static(Text("the assistant starts when you open this tab", style="dim"), id="assistant-screen")
            yield Static("", id="assistant-menu")
            yield Static("", id="assistant-status")
            yield AssistantInput(placeholder="ask the assistant; enter sends", id="assistant-input")
            yield Static(Text(KEYS, style="dim"), id="assistant-keys")

    def on_mount(self):
        if C.CONFIG_DIR is None:   # pl commands need a profile: the assistant would have nothing to act on
            self.query_one("#assistant-input", Input).disabled = True
            self.status = "no profile loaded: the assistant needs one (pl setup)"
        self.query_one("#assistant-menu").display = False
        self._show_status()
        self.set_interval(1, self._poll)

    def _poll(self):   # a slow read is never cancelled: the next poll waits, then after SLOW s starts a new read too
        if not any(w.group == "assistant-screen" and w.is_running for w in self.workers):
            self.tick()
        elif self._read_started is not None and time.monotonic() - self._read_started > SLOW:
            self.activity = "still reading…"
            self._show_status()
            self.tick()

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
        last = self._key
        self._seq += 1
        seq, self._read_started = self._seq, time.monotonic()

        def fn():   # a pane that died while the console stays open (/exit, tmux lost it) is started again
            status = None if assistant.pane() else assistant.ensure()
            assistant.restart_if_updated()        # reads the screen again itself, under the lock
            lines = assistant.screen(SCREEN_LINES)
            st = assistant.load()
            found, chat = _transcript(st), None
            if found:         # read again only when the file's size or mtime changed
                p, s = found
                key = (str(p), s.st_size, s.st_mtime_ns, st.get("updated_at"))
                if key != last:
                    entries = subagents.read_tail(p, ident=(s.st_dev, s.st_ino))
                    chat = render_chat(chat_items(entries), st.get("updated_at"))
            else:             # no transcript yet (or not claude): the screen itself
                key = ("screen", tuple(lines))
                if key != last:
                    chat = Text("\n".join(lines)) if lines else Text("the assistant's screen is empty", style="dim")
            return seq, status, lines, key, chat, assistant.menu(lines), assistant.busy(st), assistant.ready_idea()
        self._bg(fn, "assistant-screen", self._show_screen, self._start_failed)

    def _show_screen(self, out):
        seq, status, lines, key, chat, menu, busy, ready = out
        if seq < self._shown:          # a slow read that a newer one already overtook
            return
        self._shown, self.ready = seq, ready
        if seq == self._seq:
            self._read_started = None
        if status:
            self.status = status
        if chat is not None:
            self._key = key
            self.query_one("#assistant-screen", Static).update(chat)
            self.query_one("#assistant-scroll", VerticalScroll).scroll_end(animate=False)
            self.redraws += 1
        card = self.query_one("#assistant-menu", Static)
        card.display = bool(menu)
        if menu:
            card.update(menu_card(menu))
        self.query_one("#assistant-input", AssistantInput).options = tuple(menu["options"]) if menu else ()
        if self._lines is not None and lines != self._lines:
            self._moved = time.monotonic()
        self._lines = lines
        moved = busy is None and time.monotonic() - self._moved < MOVED   # no registry: the screen moved just now
        self.activity = "● waiting for you" if menu else "● thinking…" if busy or moved else "ready"
        self._show_status()

    def _show_status(self):
        box = self.query_one("#assistant-center")
        box.border_title = f"Assistant · {self.mode} mode"
        box.border_subtitle = self.status
        if self.ready:
            line = Text(f"Idea ready: {str(self.ready.get('title'))[:80]} · ctrl+f to file", style="bold")
        else:
            line = Text(f"{self.activity or self.status} · {HINT.get(self.mode, '')}", style="dim")
        self.query_one("#assistant-status", Static).update(line)

    # ---------- keys ----------

    def on_input_submitted(self, m):
        text = m.value
        m.input.value = ""
        if not text.strip():     # a bare Enter could accept a menu's default
            return
        self._bg(lambda: assistant.send(text), "assistant-send", lambda _: self.tick())

    def on_assistant_input_answer(self, m):
        def done(typed):
            if not typed:
                self.app.notify(CHANGED, markup=False)
            self.tick()
        self._bg(lambda: assistant.answer(m.digit, m.options), "assistant-send", done)

    def action_leave_box(self):
        self.app.set_focus(None)   # plain digits then switch tabs again

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
            return f"card {short_id(done['card_id'])} created in Inbox; the spec stage picks it up"

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
