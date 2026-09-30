"""Loops tab (each service loop and its live screen) and Activity tab (the event feed, newest first)."""
import json
import re
import shutil
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from pl import agents
from pl import config as C
from pl.tui.review import ConfirmScreen
from pl.usage import human
from pl.util import tmux

TAIL_LINES = 40
ACTIVE_RE = re.compile(r"active (\d+)([mhd]) ago")
UNIT = {"m": 60, "h": 3600, "d": 86400}
HINT = "n/a: no Claude turn seen yet · an idle loop restarts fresh over max_context (80% unless set; 0 = off)"


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


def loop_rows(data):
    """One dict per configured service loop: name, state, prompt, account, last active, next pass, output line, pane, window."""
    rows = (data or {}).get("snapshot", {}).get("rows", [])
    by_name = {r["loop"]: r for r in rows if r.get("loop")}
    prs = [r["pr"] for r in rows if r.get("pr")]
    out = []
    for name, svc in C.SERVICES.items():
        r = by_name.get(name) or {}
        w = r.get("worker") or {}
        state = {"loop_busy": "working", "loop_off": "off"}.get(r.get("kind"), "on" if w else "off")
        m = ACTIVE_RE.search(str(r.get("text") or ""))
        every = re.match(r"/loop (\d+)([mhd]) ", svc["prompt"] + " ")
        nxt = "-"
        if every and m and state != "off":
            left = int(every[1]) * UNIT[every[2]] - int(m[1]) * UNIT[m[2]]
            nxt = f"in ~{_short(left)}" if left > 0 else "due now"
        if name == "auto-review":
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
        out.append({"name": name, "state": state, "prompt": svc["prompt"], "account": svc.get("profile") or "-",
                    "context": ctx, "tokens": tok,
                    "last": f"{m[1]}{m[2]} ago" if m else "-", "next": nxt, "output": output,
                    "pane": w.get("pane"), "window": w.get("window")})
    return out


class LoopsView(Widget):
    DEFAULT_CSS = """
    LoopsView { height: 1fr; }
    #loops-table { width: 130; height: 1fr; border: round $panel-lighten-2; }
    #loops-screen { width: 1fr; height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    """
    BINDINGS = [Binding("w", "open_window", "open window"), Binding("R", "restart", "restart loop")]

    def __init__(self):
        super().__init__()
        self.loops = []

    def compose(self):
        with Horizontal():
            yield DataTable(id="loops-table", cursor_type="row", zebra_stripes=True)
            yield Static(Text("select a loop", style="dim"), id="loops-screen")

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns("loop", "state", "context", "tokens 1h", "account", "prompt", "last active", "next pass", "output")
        t.border_subtitle = HINT

    def selected(self):
        t = self.query_one(DataTable)
        return self.loops[t.cursor_row] if self.loops and 0 <= t.cursor_row < len(self.loops) else None

    def show(self, data):
        t = self.query_one(DataTable)
        keep = t.cursor_row
        loops = loop_rows(data)
        if loops != self.loops:   # an unchanged table is not rebuilt
            self.loops = loops
            t.clear()
            for x in self.loops:
                t.add_row(*(Text(str(x[k])) for k in ("name", "state", "context", "tokens", "account", "prompt", "last", "next", "output")))
            if self.loops:
                t.move_cursor(row=min(max(keep, 0), len(self.loops) - 1))
        self.load_tail()

    def on_data_table_row_highlighted(self, _):
        self.load_tail()

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
                      Text(str(e.get("card") or "")[:8]), Text(detail))

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
