"""Loops tab (each service loop and its live screen) and Activity tab (the event feed, newest first)."""
import argparse
import io
import json
import re
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, DataTable, Input, Label, Static

from pl import agents, loops, schedule, util
from pl import config as C
from pl.tui.chrome import NEW_ROW, new_row
from pl.tui.review import ConfirmScreen
from pl.usage import human
from pl.util import load_state, short_id, tmux

TAIL_LINES = 40
ACTIVE_RE = re.compile(r"active (\d+)([mhd]) ago")
UNIT = {"m": 60, "h": 3600, "d": 86400}
HINT = "n/a: no Claude turn seen yet · an idle loop restarts fresh over max_context (80% unless set; 0 = off)"
COLUMNS = (("loop", "name"), ("state", "state"), ("context", "context"), ("tokens 1h", "tokens"), ("tokens 7d", "tokens7d"),
           ("account", "account"), ("prompt", "prompt"), ("last active", "last"), ("next run", "next"), ("last result", "output"))


COPIERS = (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"])


def _copy_run(argv, text):
    try:
        subprocess.run(argv, input=text, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def event_text(e):
    """The plain text of one event: a header line with the full message, then the other fields as key=value."""
    key = next((k for k in ("message", "error", "reason", "text") if e.get(k)), None)
    text = str(e[key]) if key else ""
    rest = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "kind", "card", "profile", key))
    head = f"{str(e.get('ts') or '')[11:19]}  {e.get('kind') or ''}  {e.get('card') or ''}  {text}"
    return head + ("\n" + rest if rest else "")


def _short(sec):
    return f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h"


def _next_run(name, svc, runs):
    """When a scheduled loop runs next: the dispatcher's recorded time, else the schedule's next time from now."""
    at = (runs.get(name) or {}).get("next_at")
    if not at:
        at = schedule.next_run(schedule.parse_every(svc["every"]), datetime.now()).isoformat()
    return at.replace("T", " ")[:16]


def loop_rows(data):
    """One dict per configured loop, paused ones too: name, state, prompt, account, last active, next run, last result, pane, window."""
    rows = (data or {}).get("snapshot", {}).get("rows", [])
    by_name = {r["loop"]: r for r in rows if r.get("loop")}
    prs = [r["pr"] for r in rows if r.get("pr")]
    runs = load_state().get("services") or {}
    out = []
    for name, svc in C.SERVICES.items():
        r = by_name.get(name) or {}
        w = r.get("worker") or {}
        state = {"loop_busy": "working", "loop_off": "off"}.get(r.get("kind"), "on" if w else "off")
        m = ACTIVE_RE.search(str(r.get("text") or ""))
        every = re.match(r"/loop (\d+)([mhd]) ", svc["prompt"] + " ")
        nxt = "-"
        if svc.get("every"):
            nxt = _next_run(name, svc, runs)
        elif every and m and state != "off":
            left = int(every[1]) * UNIT[every[2]] - int(m[1]) * UNIT[m[2]]
            nxt = f"in ~{_short(left)}" if left > 0 else "due now"
        last_result = (runs.get(name) or {}).get("last_run", {}).get("result")
        if svc.get("every") and last_result:
            output = last_result
        elif name == "auto-review":
            output = f"{sum(p['state'] == 'decide' for p in prs)} need your decision, {sum(p['state'] != 'decide' for p in prs)} reviewed"
        elif name == "merge-gate":
            output = (f"{sum(p['state'] == 'merge' for p in prs)} merge-ready, {sum(p['state'] == 'rework' for p in prs)} need rework, "
                      f"{sum(p['state'] == 'gate' for p in prs)} awaiting the check")
        else:
            output = "not reported"
        u = ((data or {}).get("usage") or {}).get("loops", {}).get(name) or {}
        claude = (C.ACCOUNTS.get(svc.get("profile")) or {}).get("harness", "claude") == "claude"
        if state == "on" and u.get("dead"):
            state = "dead?"   # on for 3 fires of its interval, 0 tokens spent
        ctx = f"{u['context']}%" if claude and u.get("context") is not None else "n/a"
        tok = human(u["tokens_1h"]) if claude and u else "n/a"
        week = human(u["tokens_7d"]) if svc.get("every") and u.get("tokens_7d") is not None else "-"
        out.append({"name": name, "state": state, "prompt": svc["prompt"], "account": svc.get("profile") or "-",
                    "context": ctx, "tokens": tok, "tokens7d": week,
                    "last": f"{m[1]}{m[2]} ago" if m else "-", "next": nxt, "output": output,
                    "pane": w.get("pane"), "window": w.get("window")})
    for name, v in C.LOOPS_OFF.items():
        out.append({"name": name, "state": "paused", "prompt": v.get("prompt") or (f"/{v['skill']}" if v.get("skill") else ""),
                    "account": v.get("account") or "-", "context": "n/a", "tokens": "n/a", "tokens7d": "-", "last": "-",
                    "next": "-", "output": ((runs.get(name) or {}).get("last_run") or {}).get("result") or "not reported",
                    "pane": None, "window": None})
    if C.TRACKER.get("type") == "github-project":
        out.append(_auto_merge_row())
    return out


def _call(fn, *args, **kw):
    """Run a pl.loops function as the CLI does; a refusal (SystemExit, bad input) comes back as text."""
    err = io.StringIO()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            fn(*args, **kw)
    except SystemExit as e:
        if e.code in (0, None):
            return None
        return err.getvalue().strip() or str(e.code)
    except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
        return str(e)
    return None


class EditLoopScreen(ModalScreen):
    """The fields of `pl loops edit`, filled with the loop's current values; dismisses with the fields that changed."""
    DEFAULT_CSS = """
    EditLoopScreen { align: center middle; }
    EditLoopScreen > Vertical { width: 90; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "save"), Binding("escape", "cancel", "cancel")]
    FIELDS = (("skill", "skill"), ("prompt", "prompt"), ("every", "every"), ("hours", "hours"), ("account", "account"))

    def __init__(self, name, current):
        super().__init__()
        self.loop_name = name
        self.current = current

    def compose(self):
        with Vertical():
            yield Static(Text(f"Edit loop {self.loop_name}", style="bold"))
            for key, label in self.FIELDS:
                yield Label(label)
                yield Input(value=str(self.current.get(key) or ""), id=f"loop-{key}")
            yield Checkbox("writes (off = read-only)", bool(self.current.get("writes")), id="loop-writes")
            yield Static(Text("ctrl+s save · esc cancel · a cleared field stays as it was", style="dim"))

    def on_input_submitted(self, _):
        self.action_save()

    def action_save(self):
        changes = {}
        for key, _ in self.FIELDS:
            value = self.query_one(f"#loop-{key}", Input).value.strip()
            if value and value != str(self.current.get(key) or ""):
                changes[key] = value
        writes = self.query_one("#loop-writes", Checkbox).value
        if writes != bool(self.current.get("writes")):
            changes["writes"] = writes
        self.dismiss(changes)

    def action_cancel(self):
        self.dismiss(None)


class NewLoopScreen(ModalScreen):
    """The fields of `pl loops add`; saving runs the function the command runs and keeps the form open on a refusal."""
    DEFAULT_CSS = """
    NewLoopScreen { align: center middle; }
    NewLoopScreen > Vertical { width: 90; height: auto; max-height: 100%; overflow-y: auto; border: round $accent; padding: 1 2; background: $surface; }
    NewLoopScreen .hint { color: $text-muted; }
    NewLoopScreen .error { color: $error; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "save"), Binding("escape", "cancel", "cancel")]
    FIELDS = (("name", "a short name, letters, digits and -"),
              ("skill", "a skill to run, or leave empty and give a prompt"),
              ("prompt", "what the loop does, when there is no skill"),
              ("every", "30m, hourly, daily HH:MM, weekdays HH:MM or cron:<expr>"),
              ("hours", "HH:MM-HH:MM [weekdays], empty = any time"),
              ("account", "the account the loop runs on"))
    DEFAULTS = {"every": "hourly"}
    # a refusal goes under the first field whose name it mentions; skill, prompt and template all show on prompt
    ERROR_FIELDS = (("every", "every"), ("hours", "hours"), ("account", "account"),
                    ("skill", "prompt"), ("prompt", "prompt"), ("template", "prompt"))

    def compose(self):
        defaults = {**self.DEFAULTS, "account": next(iter(C.ACCOUNTS), "")}
        with Vertical():
            yield Static(Text("New loop", style="bold"))
            for key, hint in self.FIELDS:
                yield Label(key)
                yield Input(value=defaults.get(key, ""), id=f"new-loop-{key}")
                yield Static(hint, classes="hint")
                yield Static("", id=f"new-loop-{key}-error", classes="error")
            yield Checkbox("writes (off = read-only)", False, id="new-loop-writes")
            yield Static(Text("ctrl+s save · esc cancel", style="dim"))

    def on_input_submitted(self, _):
        self.action_save()

    def _show_error(self, key, message):
        self.query_one(f"#new-loop-{key}-error", Static).update(Text(message))

    def action_save(self):
        for key, _ in self.FIELDS:
            self._show_error(key, "")
        value = {key: self.query_one(f"#new-loop-{key}", Input).value.strip() or None for key, _ in self.FIELDS}
        name = value["name"]
        if not name:
            self._show_error("name", "give the loop a name")
            return
        if name in C.SERVICES or name in C.LOOPS_OFF:
            self._show_error("name", f"a loop named {name} exists")
            return
        writes = self.query_one("#new-loop-writes", Checkbox).value
        args = argparse.Namespace(**value, template=None, team=None, writes=writes, yes=True, dry_run=False)
        problem = _call(loops._cmd_add, args)
        if problem:
            problem = problem.removeprefix("pl loops: ")
            field = next((f for word, f in self.ERROR_FIELDS if word in problem), "name")
            self._show_error(field, problem)
            return
        self.dismiss(name)

    def action_cancel(self):
        self.dismiss(None)


def _auto_merge_row():
    from pl import merges   # merges -> merge_check -> tui.prs would loop back into this package
    s = util.load_state().get("services", {}).get("auto-merge", {})
    last, nxt = (s.get("last_run") or {}), s.get("next_at")
    on, waits = C.AUTO_MERGE.get("enabled") is True, s.get("waits")
    return {"name": "auto-merge", "state": "waiting" if on and waits else "on" if on else "off",
            "prompt": "built in: merges what passes every rule", "account": merges.account() or "-", "context": "n/a", "tokens": "n/a", "tokens7d": "-",
            "last": util.ago(last.get("at")), "next": nxt[11:16] if nxt else "-",
            "output": f"waiting: {waits}" if on and waits else last.get("result") or "not run yet", "pane": None, "window": None}


class LoopsView(Widget):
    DEFAULT_CSS = """
    LoopsView { height: 1fr; }
    #loops-table { width: 130; height: 1fr; border: round $panel-lighten-2; }
    #loops-screen { width: 1fr; height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    """
    BINDINGS = [Binding("n", "new", "new loop"), Binding("e", "edit", "edit"), Binding("p", "pause", "pause/resume"), Binding("g", "run_now", "run now"),
                Binding("d", "delete", "delete"), Binding("w", "open_window", "open window"), Binding("R", "restart", "restart loop")]

    def __init__(self):
        super().__init__()
        self.loops = []

    def compose(self):
        with Horizontal():
            yield DataTable(id="loops-table", cursor_type="row", zebra_stripes=True)
            yield Static(Text("select a loop", style="dim"), id="loops-screen")

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns(*(label for label, _ in COLUMNS))
        t.border_subtitle = HINT

    def selected(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return next((x for x in self.loops if x["name"] == key), None)

    def show(self, data):
        t = self.query_one(DataTable)
        keep = t.cursor_row
        loops = loop_rows(data)
        if loops != self.loops:   # an unchanged table is not rebuilt
            self.loops = loops
            t.clear()
            t.add_row(new_row("loop"), *[""] * (len(COLUMNS) - 1), key=NEW_ROW)
            for x in self.loops:
                t.add_row(*(Text(str(x[k])) for _, k in COLUMNS), key=x["name"])
            t.move_cursor(row=min(max(keep, 0), len(self.loops)))
        self.load_tail()

    def on_data_table_row_highlighted(self, _):
        self.load_tail()

    def on_data_table_row_selected(self, event):
        if event.row_key.value == NEW_ROW:
            self.action_new()

    def action_new(self):
        def added(name):
            if name:
                self.app.notify(f"added loop {name}", markup=False)
        self.app.push_screen(NewLoopScreen(), added)

    def load_tail(self):
        x = self.selected()
        if x is None:
            return
        name, pane = x["name"], x["pane"]

        def run():   # tmux off the UI thread
            try:
                lines = agents.pane_tail(pane, TAIL_LINES)
            except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, f"could not read {name}'s screen: {e}", severity="error", markup=False)
                return
            self.app.call_from_thread(self._show_tail, name, pane, lines)
        self.run_worker(run, thread=True, group="loops-tail")

    def _show_tail(self, name, pane, lines):
        x = self.selected()
        if x is None or x["name"] != name:
            return
        body = Text(f"{name} loop\n" if pane else f"{name} loop is off; showing the dispatcher\n", style="bold")
        body.append("\n".join(lines), style="not bold")
        self.query_one("#loops-screen", Static).update(body)

    def _change(self, done, fn, *args, **kw):
        def run():
            problem = _call(fn, *args, **kw)
            self.app.call_from_thread(self.app.notify, problem or done, severity="error" if problem else "information", markup=False)
        self.run_worker(run, thread=True, group="loops-action")

    def action_edit(self):
        x = self.selected()
        if x is None:
            return
        name = x["name"]
        svc = C.SERVICES.get(name) or C.LOOPS_OFF.get(name) or {}
        current = {"skill": svc.get("skill"), "prompt": None if svc.get("skill") else svc.get("prompt"),
                   "every": svc.get("every"), "hours": svc.get("hours"), "account": svc.get("profile") or svc.get("account"),
                   "writes": svc.get("writes")}

        def saved(changes):
            if changes:
                self._change(f"edited {name}", loops.edit, name, **changes)
        self.app.push_screen(EditLoopScreen(name, current), saved)

    def action_pause(self):
        x = self.selected()
        if x is not None:
            resume = x["state"] == "paused"
            self._change(f"{'resumed' if resume else 'paused'} {x['name']}", loops.set_enabled, x["name"], resume)

    def action_run_now(self):
        x = self.selected()
        if x is not None:
            self._change(f"queued {x['name']}; the dispatcher starts it on its next pass", loops.run, x["name"])

    def action_delete(self):
        x = self.selected()
        if x is None:
            return
        name = x["name"]

        def answered(yes):
            if yes:
                self._change(f"deleted {name}", loops.remove, name)
        self.app.push_screen(ConfirmScreen(f"Delete the {name} loop? pl removes [loops.{name}] from config.toml."), answered)

    def action_open_window(self):
        x = self.selected()
        if x is None or not x["window"]:
            self.app.notify("this loop has no window: the dispatcher restarts stopped loops", markup=False)
            return
        name = x["name"]

        def run():
            try:
                msg = agents.jump_to_window(name)
            except Exception as e:  # noqa: BLE001
                msg = f"could not open the window: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)
        self.run_worker(run, thread=True, group="loops-action")

    def action_restart(self):
        x = self.selected()
        if x is None or not x["window"]:
            self.app.notify("this loop is not running: the dispatcher starts it on its next pass", markup=False)
            return
        name, wid = x["name"], x["window"]

        def kill():
            try:
                tmux("kill-window", "-t", wid)   # the loop's own window id only; argv list, no shell
                msg = f"stopped {name}; the dispatcher starts it again on its next pass"
            except (SystemExit, Exception) as e:
                msg = f"could not stop {name}: {e}"
            self.app.call_from_thread(self.app.notify, msg, markup=False)

        def answered(yes):
            if yes:
                self.run_worker(kill, thread=True, group="loops-action")
        self.app.push_screen(ConfirmScreen(f"Restart the {name} loop? pl closes its tmux window {wid}; "
                                           "the dispatcher starts it again on its next pass."), answered)


MAX_EVENTS = 500
MAX_BYTES = 500 * 4096
FILTERS = ["all", "errors", "needs you", "dispatcher", "loops"]
NEEDS_COLUMNS = {"Plan for review", "Manual", "Spec ready"}


def read_events(path):
    """events.jsonl, newest first; malformed lines are skipped."""
    try:
        with open(path, "rb") as f:   # only the newest lines: the file grows without bound
            f.seek(0, 2)
            f.seek(max(0, f.tell() - MAX_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines()[-MAX_EVENTS:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out[::-1]


def matches(e, f):
    kind = str(e.get("kind") or "")
    if f == "errors":
        return kind == "error"
    if f == "needs you":
        return kind == "moved" and isinstance(e.get("to"), str) and e["to"] in NEEDS_COLUMNS
    if f == "dispatcher":
        return kind in ("started", "moved", "error")
    if f == "loops":
        return bool(e.get("loop")) or kind.startswith("loop")
    return True


class ActivityView(Widget):
    DEFAULT_CSS = """
    ActivityView { height: 1fr; }
    #activity-filter { height: 1; margin: 0 1; }
    #activity-table { height: 1fr; border: round $panel-lighten-2; }
    """
    BINDINGS = [Binding("f", "next_filter", "filter"), Binding("y", "copy", "copy")]

    def __init__(self):
        super().__init__()
        self.filter = "all"

    def compose(self):
        with Vertical():
            yield Static(id="activity-filter")
            yield DataTable(id="activity-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        self.query_one(DataTable).add_columns("time", "kind", "card", "detail")

    def show(self, data=None):
        bar = Text("filter  ")
        for f in FILTERS:
            bar.append(f" {f} ", style="bold reverse" if f == self.filter else "dim")
        bar.append("   f next filter", style="dim")
        self.query_one("#activity-filter", Static).update(bar)
        t = self.query_one(DataTable)
        shown = [e for e in read_events(C.STATE_DIR / "events.jsonl") if matches(e, self.filter)]
        if shown == getattr(self, "_shown", None):   # an unchanged table is not rebuilt
            return
        self._shown = shown
        t.clear()
        for e in shown:
            detail = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "kind", "card", "profile"))
            t.add_row(Text(str(e.get("ts") or "")[:19].replace("T", " ")), Text(str(e.get("kind") or "")),
                      Text(short_id(e.get("card") or "")), Text(detail))

    def action_next_filter(self):
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        try:
            self.show()
        except Exception as e:  # noqa: BLE001 - a bad event line must not end the console
            self.app.notify(f"could not show the activity feed: {e}", severity="error", markup=False)

    def action_copy(self):
        row = self.query_one(DataTable).cursor_row
        shown = getattr(self, "_shown", None) or []
        if not 0 <= row < len(shown):
            self.app.notify("select an event first", markup=False)
            return
        text = event_text(shown[row])
        self.app.copy_to_clipboard(text)
        for argv in COPIERS:
            if shutil.which(argv[0]):
                _copy_run(argv, text)
                break
        self.app.notify(f"copied: {text[:60]}", markup=False)
