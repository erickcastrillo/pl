"""Spec review and plan review inside pl: the text, checks computed from it, notes, approve / send back.

Board reads and writes run in thread workers; approve and send back call the same functions as the CLI."""
import argparse
import io
import os
import re
import shlex
import subprocess
from contextlib import redirect_stdout
from urllib.parse import urlparse

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, Markdown, Static, TextArea

from pl import commands
from pl import config as C
from pl.board import card, card_size, check_size, col_name, fresh_next, max_card_chars, render, sections, update
from pl.ideas import _plain
from pl.util import parse_iso, slug_of

_run = subprocess.run          # the one subprocess runner here ($EDITOR); tests fake it

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
AC_RE = re.compile(r"\bAC-(\d+)\b")
EMPTY_RE = re.compile(r"[-*\s]*\(?(none|n/a|nothing)\)?\.?", re.I)
HEAD_RE = re.compile(r"^(#+)\s")
OQ_RE = re.compile(r"^#+\s.*open question", re.I)
ITEM_RE = re.compile(r"^(\s*)(\d+[.)]|[-*+])\s+(.*\S)")
ANSWER_RE = re.compile(r"^\s*\*\*Answer:\*\*\s?(.*)$")


def spec_checks(text, tags):
    """(label, value, ok) rows for the right rail, computed from the SPEC text and the card tags."""
    text = text or ""
    ids = set(AC_RE.findall(text))
    ac_lines = [line for line in text.splitlines() if AC_RE.search(line)]
    neg = any(re.search(r"\bnegative\b", line, re.I) for line in ac_lines)
    reg = any(re.search(r"\bregression\b", line, re.I) for line in ac_lines)
    if not any(OQ_RE.match(line) for line in text.splitlines()):
        oq_row = ("open questions", "section missing", False)
    else:
        qs = open_questions(text)
        still, done = sum(1 for q in qs if not q["answer"]), sum(1 for q in qs if q["answer"])
        oq_row = ("open questions", (f"{still} open" if still else "none open") + (f", {done} answered" if done else ""),
                  not still)
    repos = ", ".join(map(str, tags or []))
    return [("acceptance criteria", str(len(ids)), bool(ids)),
            ("negative AC", "yes" if neg else "missing", neg),
            ("regression AC", "yes" if reg else "missing", reg),
            ("repos", repos or "none tagged", bool(repos)),
            oq_row]


def plan_checks(text):
    """(label, value, ok) rows: the budget line, WP count and tiers from the index table, the not-doing list."""
    text = text or ""
    b = re.search(r"^[\s>*`-]*budget:\s*(.+?)[\s`]*$", text, re.I | re.M)
    rows = [line for line in text.splitlines() if re.match(r"^\s*\|\s*WP\d+", line)]
    wps = {re.match(r"^\s*\|\s*(WP\d+)", line).group(1) for line in rows}
    lite = sum(1 for line in rows if re.search(r"\|\s*lite\s*\|", line, re.I))
    full = sum(1 for line in rows if re.search(r"\|\s*full\s*\|", line, re.I))
    nd = bool(re.search(r"deliberately not doing", text, re.I))
    return [("budget", b.group(1) if b else "missing", bool(b)),
            ("work packages", f"{len(wps)} ({lite} lite, {full} full)" if wps else "no index table", bool(wps)),
            ("deliberately not doing", "present" if nd else "missing", nd)]


def open_questions(text):
    """The items under the first "open question" heading (any level, up to the next heading at that level or above):
    dicts with marker, text, indent (of the item's text), end (line after its last line), answer, answer_line."""
    qs, level, top, cur = [], None, None, None
    for i, line in enumerate((text or "").splitlines()):
        h = HEAD_RE.match(line)
        if level is None:
            level = len(h.group(1)) if h and OQ_RE.match(line) else None
            continue
        if h:
            if len(h.group(1)) <= level:
                break
            cur = None
            continue
        a, m = ANSWER_RE.match(line), ITEM_RE.match(line)
        if a and cur:
            cur.update(answer=a.group(1).strip(), answer_line=i, end=i + 1)
        elif m and (top is None or len(m.group(1)) <= top):
            top = len(m.group(1)) if top is None else top
            cur = None if EMPTY_RE.fullmatch(line.strip()) else {
                "marker": m.group(2), "text": m.group(3), "indent": len(m.group(1)) + len(m.group(2)) + 1,
                "end": i + 1, "answer": None, "answer_line": None}
            if cur:
                qs.append(cur)
        elif not line.strip():
            continue
        elif cur and line[:1].isspace():
            cur["end"] = i + 1          # continuation line or a nested bullet
        else:
            cur = None                  # plain text such as "Decided by default"
    return qs


def with_answers(text, answers):
    """text with one `**Answer:** <a>` line under each answered item (index -> answer; blank removes it);
    every other byte unchanged. An answer is one line, so it can never start a card section."""
    lines = text.splitlines(keepends=True)
    qs = open_questions(text)
    for i in sorted((i for i in answers if i < len(qs)), reverse=True):
        q, a = qs[i], " ".join(str(answers[i]).split())
        new = f"{' ' * q['indent']}**Answer:** {a}\n" if a else None
        if q["answer_line"] is not None:
            lines[q["answer_line"]:q["answer_line"] + 1] = [new] if new else []
        elif new:
            if not lines[q["end"] - 1].endswith(("\n", "\r")):
                lines[q["end"] - 1] += "\n"
            lines.insert(q["end"], new)
    return "".join(lines)


def write_spec(card_id, loaded, new):
    """Write new as the card's SPEC section through the tracker, refused when the card left Spec ready or its SPEC
    is no longer the text the reader loaded. Section markers in new are neutralised. Returns the card read again."""
    fresh_next()   # the check below must see the board as it is now, not a copy that may be minutes old
    c = card(card_id)
    col = col_name(c["list_id"])
    if col != KIND_COLUMN["spec"]:
        raise SystemExit(f"the card is now in '{col}', not '{KIND_COLUMN['spec']}': nothing written")
    parts = sections(c.get("description"))
    if (parts.get("SPEC") or "") != loaded:
        raise SystemExit("the spec changed on the board since you opened it: nothing written (reopen it to see the new text)")
    parts["SPEC"] = _plain(new).strip("\n")
    update(card_id, description=check_size(render(parts)))
    return card(card_id)


def local_plan(c):
    """The local plan file for a card, only when it resolves inside the plans folder (plan_path is board data)."""
    try:
        p = commands.plan_path(c).resolve()
        return p if p.is_relative_to(C.PLANS.resolve()) else None
    except (OSError, ValueError):
        return None


def review_text(kind, c):
    """The text to review: the SPEC section, or the PLAN section unless your local plan file is newer (like pull_plan)."""
    parts = sections(c.get("description"))
    if kind == "spec":
        return parts.get("SPEC") or ""
    p = local_plan(c)
    if p and p.is_file() and p.stat().st_mtime > parse_iso(c.get("updated_at") or ""):
        return p.read_text()
    return parts.get("PLAN") or ""


def load_review(kind, card_id):
    """One board read: (card, the text to review, its checks). Call it from a thread worker."""
    c = card(card_id)
    text = review_text(kind, c)
    return c, text, spec_checks(text, c.get("tags")) if kind == "spec" else plan_checks(text)


def size_text(description):
    """The card's size against the limit: dim, a warning at 80 %, an error when over."""
    n, limit = card_size(description), max_card_chars()
    return Text(f"size {n:,} / {limit:,}", style="red" if n > limit else "yellow" if n >= 0.8 * limit else "dim")


def checks_text(checks):
    rail = Text()
    for label, value, ok in checks:
        rail.append("✓ " if ok else "✗ ", style="green" if ok else "red")
        rail.append(f"{label}  ")
        rail.append(f"{value}\n", style="dim")
    return rail


def review_file(kind, c):
    """The file `e` opens. Written 0600 from the card only when absent; never overwritten."""
    slug = slug_of(c)
    if not SLUG_RE.fullmatch(slug or ""):
        raise SystemExit(f"pl: card {c['id'][:8]} has the slug {slug!r}, which is not a safe file name (a-z, 0-9 and -)")
    if kind == "spec":
        path, text = C.PLANS.parent / "specs" / f"spec-{slug}.md", sections(c.get("description")).get("SPEC") or ""
    else:
        path, text = local_plan(c) or C.PLANS / f"{slug}.md", sections(c.get("description")).get("PLAN") or ""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(text.rstrip() + "\n")
    return path


def safe_link(href):
    return urlparse(str(href or "")).scheme.lower() in ("http", "https")


KIND_COLUMN = {"spec": "Spec ready", "plan": "Plan for review"}


def run_command(app, fn, after=None, kind=None, says=None, failed="", done=None, **kw):
    """Run a CLI command function in a thread worker; its printed output becomes a notification.
    With kind, the card is re-read first and the command is refused when it is no longer in that kind's column.
    says(card read again) replaces the printed output; failed prefixes an error; done runs either way."""
    def run():
        out = io.StringIO()
        try:
            if kind:
                col = col_name(card(kw["id"])["list_id"])
                if col != KIND_COLUMN[kind]:
                    raise SystemExit(f"the card is now in '{col}', not '{KIND_COLUMN[kind]}': nothing changed")
            with redirect_stdout(out):
                fn(argparse.Namespace(**kw))
        except (SystemExit, Exception) as e:
            app.call_from_thread(app.notify, f"{failed}{e}", severity="error", markup=False)
            if done:
                app.call_from_thread(done)
            return
        msg = out.getvalue().strip() or "done"
        if says:
            try:   # the command worked; a failed re-read only loses the column name
                msg = says(card(kw["id"]))
            except (SystemExit, Exception):  # noqa: BLE001
                pass
        app.call_from_thread(app.notify, msg, markup=False)
        if done:
            app.call_from_thread(done)
        if after:
            app.call_from_thread(after)
        app.call_from_thread(app.refresh_data)
    app.run_worker(run, thread=True, group="review")


def confirm_and_approve(app, card_id, title, kind, after=None, busy=None, done=None):
    def answered(yes):
        if yes:
            if busy:
                busy()
            run_command(app, commands.cmd_approve, after, kind=kind, id=card_id, force=False,
                        says=lambda c: f"Approved: {title} → {col_name(c['list_id'])}", failed="Not approved: ",
                        done=done)
    app.push_screen(ConfirmScreen(f"Approve the {kind} for {card_id[:8]}  {title}?"), answered)


class ConfirmScreen(ModalScreen):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical { width: 80; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("y", "answer(True)", "yes"), Binding("n", "answer(False)", "no"),
                Binding("escape", "answer(False)", "cancel")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical():
            yield Static(Text(self.message))
            yield Static(Text("y yes · n no", style="dim"))

    def action_answer(self, yes):
        self.dismiss(yes)


NOTES_TITLE = "notes (ctrl+x sends back with these)"


class ReviewScreen(Screen):
    DEFAULT_CSS = """
    #review-title { height: 1; padding: 0 1; }
    #review-doc { width: 3fr; border: round $primary; }
    #review-rail { width: 1fr; min-width: 40; }
    #review-checks { height: auto; border: round $primary; padding: 0 1; }
    #review-editor { width: 3fr; }
    #review-answers { height: 1fr; border: round $primary; padding: 0 1; }
    #review-answers Static { margin-top: 1; }
    #review-notes { height: 1fr; border: round $primary; }
    """
    BINDINGS = [Binding("a", "approve", "approve"), Binding("x", "send_back", "send back"),
                Binding("ctrl+x", "send_back", "send back", key_display="^x", priority=True),   # works while typing
                Binding("e", "edit", "edit"), Binding("E", "external_edit", "$EDITOR"),
                Binding("ctrl+s", "save", "save answers / edit"),
                Binding("left_square_bracket", "step(-1)", "previous", key_display="["),
                Binding("right_square_bracket", "step(1)", "next", key_display="]"), Binding("escape", "back", "back")]

    def __init__(self, kind, card_id, ids=None, notes_first=False, answers_first=False):
        super().__init__()
        self.kind, self.card_id, self.notes_first, self.answers_first = kind, card_id, notes_first, answers_first
        self.ids = list(ids or [card_id])
        self.card = None
        self.text, self.questions, self.answered, self.editing = "", [], {}, False
        self.sending = False   # a send back or approve is running: a second press does nothing

    def compose(self):
        yield Static(Text(f"{self.kind.upper()} REVIEW · loading {self.card_id[:8]}"), id="review-title")
        with Horizontal():
            with VerticalScroll(id="review-doc"):
                yield Markdown("", open_links=False, id="review-md")
            ed = TextArea(id="review-editor")
            ed.border_title = "edit the spec · ctrl+s save · esc cancel"
            ed.display = False
            yield ed
            with Vertical(id="review-rail"):
                yield Static(Text("loading", style="dim"), id="review-checks")
                qa = VerticalScroll(id="review-answers")
                qa.border_title = "open questions · ctrl+s save"
                qa.display = False
                yield qa
                ta = TextArea(id="review-notes")
                ta.border_title = NOTES_TITLE
                yield ta
        yield Footer()

    def on_mount(self):
        self.query_one("#review-notes" if self.notes_first else "#review-doc").focus()
        self.load()

    def load(self):
        cid, kind = self.card_id, self.kind

        def run():   # board read off the UI thread
            try:
                c, text, checks = load_review(kind, cid)
            except (SystemExit, Exception) as e:
                self.app.call_from_thread(self.app.notify, f"could not read the card: {e}", severity="error", markup=False)
                return
            self.app.call_from_thread(self._show, c, text, checks)
        self.run_worker(run, thread=True, group="review")

    def _dirty(self):
        """Answers typed and not saved, or the editor open: a reload must not replace the text."""
        return self.editing or any(b.value.strip() != (q["answer"] or "")
                                   for q, b in zip(self.questions, self.query("#review-answers Input")))

    async def _show(self, c, text, checks, force=False):
        if c["id"] != self.card_id or (self._dirty() and not force):
            return
        self.card, self.text = c, text
        i = self.ids.index(self.card_id) + 1 if self.card_id in self.ids else 1
        self.query_one("#review-title", Static).update(
            Text(f"{self.kind.upper()} REVIEW · {c['id'][:8]}  {str(c.get('title') or '')} · {i} of {len(self.ids)} · ")
            + size_text(c.get("description")))
        self.query_one("#review-checks", Static).update(checks_text(checks))
        await self.query_one("#review-md", Markdown).update(text)
        if self.kind == "spec":
            await self._show_questions(text)

    async def _show_questions(self, text):
        self.questions = open_questions(text)
        box = self.query_one("#review-answers", VerticalScroll)
        await box.remove_children()
        box.display = bool(self.questions)
        await box.mount_all([w for i, q in enumerate(self.questions) for w in (
            Static(Text(f"{q['marker']} {q['text']}")), Input(q["answer"] or "", placeholder="your answer", id=f"answer-{i}"))])
        if self.answers_first and self.questions:
            self.answers_first = False
            self.query_one("#answer-0", Input).focus()

    def _busy(self):
        if self._dirty():
            self.app.notify("save first (ctrl+s), or esc to drop the edit", severity="warning")
        return self._dirty()

    def action_save(self):
        if self.kind != "spec" or self.card is None:
            return
        if self.editing:
            new, answered = self.query_one("#review-editor", TextArea).text, {}
        else:
            boxes = list(self.query("#review-answers Input"))
            if not self._dirty():
                self.app.notify("nothing to save")
                return
            new = with_answers(self.text, {i: b.value for i, b in enumerate(boxes)})
            answered = {i: (q["marker"], q["text"], " ".join(b.value.split()))
                        for i, (q, b) in enumerate(zip(self.questions, boxes)) if b.value.strip() != (q["answer"] or "")}
        cid, loaded = self.card_id, self.text

        def run():   # re-read, check, write off the UI thread
            try:
                c = write_spec(cid, loaded, new)
            except (SystemExit, Exception) as e:
                self.app.call_from_thread(self.app.notify, str(e), severity="error", markup=False)
                return
            self.app.call_from_thread(self._saved, c, answered)
        self.run_worker(run, thread=True, group="review")

    async def _saved(self, c, answered):
        for i, a in answered.items():   # a cleared answer is no longer part of the send-back note
            if a[2]:
                self.answered[i] = a
            else:
                self.answered.pop(i, None)
        self._close_editor()
        text = review_text("spec", c)
        await self._show(c, text, spec_checks(text, c.get("tags")), force=True)
        self.app.notify("saved")

    def _close_editor(self):
        self.editing = False
        self.query_one("#review-editor").display = False
        self.query_one("#review-doc").display = True
        self.query_one("#review-doc").focus()

    def _close(self):
        if self.app.screen is self:
            self.app.pop_screen()

    def _in_flight(self, what=None):
        """what: show "<what>…" over the notes box and ignore further presses; None: back to normal."""
        self.sending = what is not None
        self.query_one("#review-notes").border_title = f"{what}…" if what else NOTES_TITLE

    def action_approve(self):
        if self.sending or self._busy():
            return
        title = str((self.card or {}).get("title") or "")
        confirm_and_approve(self.app, self.card_id, title, self.kind, self._close,
                            busy=lambda: self._in_flight("approving"), done=self._in_flight)

    def action_send_back(self):
        if self.sending or self._busy():
            return
        notes = self.query_one("#review-notes", TextArea).text.strip()
        if self.answered:   # the answers go to the spec writer with the notes
            n = len(self.answered)
            said = "".join(f"\n{m} {q}\n   Answer: {a}" for m, q, a in self.answered.values())
            notes = f"answered {n} open question{'s' * (n != 1)}:{said}" + (f"\n\n{notes}" if notes else "")
        notes = _plain(notes)
        if not notes:
            self.app.notify("write what to change first: send back needs notes in the notes box", severity="warning")
            return
        title, n = str((self.card or {}).get("title") or ""), sum(1 for line in notes.splitlines() if line.strip())
        self._in_flight("sending back")
        run_command(self.app, commands.cmd_reject, self._close, kind=self.kind, id=self.card_id, notes=notes,
                    says=lambda c: f"Sent back to {col_name(c['list_id'])}: {title} — {n} note{'s' * (n != 1)}",
                    failed="Not sent back: ", done=self._in_flight)

    def action_edit(self):
        """Spec: edit the whole SPEC section here. Plan: $EDITOR, as before."""
        if self.kind != "spec":
            return self.action_external_edit()
        if self.card is None or self._busy():
            return
        self.editing = True
        ed = self.query_one("#review-editor", TextArea)
        ed.load_text(self.text)
        self.query_one("#review-doc").display = False
        ed.display = True
        ed.focus()

    def action_external_edit(self):
        if self.card is None:
            return
        try:
            path = review_file(self.kind, self.card)
            before = path.read_text()
        except (SystemExit, Exception) as e:
            self.app.notify(str(e), severity="error", markup=False)
            return
        with self.app.suspend():
            _run([*shlex.split(os.environ.get("EDITOR") or "vi"), str(path)])
        new = path.read_text() if self.kind == "spec" and path.is_file() else before
        if new == before:
            return self.load()
        cid, loaded = self.card_id, self.text

        def run():   # a spec edited in $EDITOR goes to the card through the same checks as ctrl+s
            try:
                c = write_spec(cid, loaded, new)
            except (SystemExit, Exception) as e:
                self.app.call_from_thread(self.app.notify, f"{e}. Your edit is kept in {path}",
                                          severity="error", markup=False)
                return
            self.app.call_from_thread(self._saved, c, {})
        self.run_worker(run, thread=True, group="review")

    def action_step(self, d):
        if len(self.ids) < 2 or self._busy():
            return
        i = self.ids.index(self.card_id) if self.card_id in self.ids else 0
        self.card_id = self.ids[(i + d) % len(self.ids)]
        self.card, self.answered = None, {}
        self.query_one("#review-notes", TextArea).load_text("")
        self.load()

    def action_back(self):
        if self.editing:            # esc drops the edit; nothing is written
            self._close_editor()
        elif isinstance(self.focused, (TextArea, Input)):
            self.query_one("#review-doc").focus()
        else:
            self.app.pop_screen()

    def on_markdown_link_clicked(self, m):
        if safe_link(m.href):    # card text is untrusted: a file: or javascript: link is never opened
            self.app.open_url(m.href)
        else:
            self.app.notify("only web links open from here", markup=False)
