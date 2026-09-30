"""Dashboard: six tiles for the chosen window, 14-day throughput, where work waits, decide next, health."""
import shutil
import statistics
import time
from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Digits, Label, Sparkline, Static

from pl import config as C
from pl import standup
from pl.tui.loops import COPIERS, _copy_run
from pl.tui.needs import NeedsView, needs_groups, waiting_total
from pl.tui.prs import pr_groups
from pl.usage import human
from pl.util import age, parse_iso

WINDOWS = [("1h", "last hour", 3600), ("24h", "last 24 hours", 86400), ("7d", "last 7 days", 604800)]
TILES = [("specs", "SPECS WRITTEN"), ("plans", "PLANS WRITTEN"), ("opened", "PRS OPENED"), ("merged", "PRS MERGED"),
         ("ready", "READY TO MERGE"), ("waiting", "WAITING ON YOU")]


def _t(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def count_since(stamps, seconds, now=None):
    now = now or datetime.now(timezone.utc)
    return sum(1 for s in stamps or () if (t := _t(s)) and now - timedelta(seconds=seconds) < t <= now)


def per_day(stamps, days=14, now=None):
    today = (now or datetime.now(timezone.utc)).date()
    out = [0] * days
    for s in stamps or ():
        t = _t(s)
        if t:
            ago = (today - t.astimezone(timezone.utc).date()).days
            if 0 <= ago < days:
                out[days - 1 - ago] += 1
    return out


def column_waits(rows):
    """(column, cards, oldest age, median age) per column except Done, from updated_at."""
    out = []
    for col in C.COLUMNS:
        if col == "Done":
            continue
        ts = sorted(parse_iso(r["card"].get("updated_at") or "") for r in rows if r.get("card") and r.get("col") == col)
        out.append((col, len(ts), age(ts[0]) if ts else "-", age(statistics.median(ts)) if ts else "-"))
    return out


def tile_values(data, window):
    key, _, seconds = WINDOWS[window]
    m = (data.get("metrics_by_window") or {}).get(key) or {}
    act = data.get("pr_activity") or {}
    rows = data["snapshot"]["rows"]
    return {"specs": m.get("specs_written", 0), "plans": m.get("plans_written", 0),
            "opened": count_since(act.get("opened"), seconds) if act else "?",
            "merged": count_since(act.get("merged"), seconds) if act else "?",
            "ready": len(pr_groups(rows)["merge"]), "waiting": waiting_total(needs_groups(rows))}


# row key → (tab, Needs-you group to select there, or None)
DECIDE_TARGET = {"merge": ("prs", None), "specs": ("needs", "specs"), "plans": ("needs", "plans"),
                 "rework": ("needs", "rework"), "decide": ("needs", "decide"), "manual": ("needs", "manual")}


def decide_next(rows):
    """(key, headline, sub line, colour, count) for every Decide-next row; a row with count 0 is still listed."""
    g, p = needs_groups(rows), pr_groups(rows)
    plans = sorted(parse_iso(r["card"].get("updated_at") or "") for r in g["plans"])
    return [("merge", len(p["merge"]), f"{len(p['merge'])} PRs pass the merge check", "merge them, oldest first", "green"),
            ("specs", len(g["specs"]), f"{len(g['specs'])} specs wait for your review", "approve, or send back with notes", "#e0a040"),
            ("plans", len(plans), f"{len(plans)} plans wait a median of {age(statistics.median(plans)) if plans else '-'}",
             f"review; the oldest waited {age(plans[0]) if plans else '-'}", "#e0a040"),
            ("rework", len(p["rework"]), f"{len(p['rework'])} PRs need rework", "send back to a run agent, or close", "red"),
            ("decide", len(p["decide"]), f"{len(p['decide'])} PRs stopped for your call", "auto-review gave up on purpose", "#e0a040"),
            ("manual", len(g["manual"]), f"{len(g['manual'])} manual tasks", "no agent runs these", "#5f9fff")]


def health_tokens(data, window=1):
    """(label, value) for the Health panel: tokens in the chosen window, split by account; every configured account
    with none (other harnesses included) shows n/a. A session two accounts share a folder for may show as "a+b"."""
    u = data.get("usage")
    if not u:
        return ("spend", "not tracked yet")
    key = WINDOWS[window][0]
    by = (u.get("by_window") or {}).get(key) or {}
    covered = {x for k in by for x in k.split("+")}
    parts = [f"{a} {human(n)}" for a, n in sorted(by.items(), key=lambda kv: -kv[1])]
    parts += [f"{a} n/a" for a in u.get("accounts") or [] if a not in covered]
    return (f"tokens {key}", human(sum(by.values())) + (f" ({' · '.join(parts)})" if parts else ""))


def health(data, window=1):
    rows = data["snapshot"]["rows"]
    loops = [r for r in rows if r.get("loop")]
    # errors_last is the most recent error of all time, the same in every window
    err = next((m["errors_last"] for m in (data.get("metrics_by_window") or {}).values() if m and m.get("errors_last")), None)
    act = data.get("pr_activity")
    week = WINDOWS[2][2]
    cycle = (f"opened {count_since(act.get('opened'), week)} · merged {count_since(act.get('merged'), week)} in 7 days"
             if act else "GitHub not reachable")
    lines = [("agents running", f"{sum(1 for r in rows if r.get('kind') == 'working')} working"),
             ("loops", f"{sum(1 for r in loops if r.get('kind') != 'loop_off')} of {len(loops)} on"),
             ("harness accounts", data["snapshot"]["prof"]),
             ("last dispatcher error", f"{err.get('message', err.get('kind'))} · {(err.get('ts') or '')[11:16]}" if err else "none"),
             ("PR review cycle", cycle),
             health_tokens(data, window)]
    t = Text()
    for k, v in lines:
        t.append(f"{k:<24}", style="dim")
        t.append(f"{v}\n\n", style="red" if k == "last dispatcher error" and err else "")
    return t


def copy_text(app, text):
    app.copy_to_clipboard(text)
    for argv in COPIERS:
        if shutil.which(argv[0]):
            _copy_run(argv, text)
            break


class StandupPanel(VerticalScroll, can_focus=True):
    """The last 24 hours' standup text, always on the Dashboard; y copies it. Rebuilt off the UI thread when the
    board snapshot changes; the two-to-three GitHub searches are reused for 5 minutes."""
    BINDINGS = [Binding("y", "copy", "copy for Slack")]
    PR_TTL = 300

    def __init__(self, **kw):
        super().__init__(**kw)
        self.text, self.slack, self._key, self._prs, self._prs_at = "", "", None, None, 0.0

    def compose(self):
        yield Static(Text("building the standup…", style="dim"), id="standup-panel-text")

    def on_mount(self):
        self.border_title = "Standup (last 24 h) · y copy · s full"

    def update_for(self, snapshot):
        key = (snapshot.get("rows"), snapshot.get("needs"))   # not the whole snapshot: its clock changes every refresh
        if key != self._key:
            self._key = key
            self.build(snapshot)

    @work(thread=True, exclusive=True, group="standup-panel")
    def build(self, snapshot):
        start = standup.parse_since(None)
        try:
            if self._prs is None or time.monotonic() - self._prs_at > self.PR_TTL:
                self._prs, self._prs_at = standup.pr_summary(start), time.monotonic()
            got, slack = standup.text(snapshot, start, self._prs), standup.text(snapshot, start, self._prs, fmt="slack")
        except Exception as e:  # noqa: BLE001 - a bad event line must not end the console
            got = slack = f"could not build the standup: {type(e).__name__}: {e}"
        self.app.call_from_thread(self.shown, got, slack)

    def shown(self, got, slack):
        self.text, self.slack = got, slack
        self.query_one("#standup-panel-text", Static).update(Text(got))

    def action_copy(self):
        if not self.text:
            self.app.notify("still building", markup=False)
            return
        copy_text(self.app, self.slack)
        self.app.notify("copied for Slack", markup=False)


class DashboardView(Vertical):
    def compose(self):
        yield Static(Text(""), id="window-bar")
        with Horizontal(id="tiles"):
            for key, title in TILES:
                with Vertical(id=f"tile-{key}", classes=f"tile tile-{key}"):
                    yield Label(title, classes="tile-title")
                    yield Digits("-")
                    yield Label("", classes="tile-sub")
        with Horizontal(classes="row"):
            with Vertical(id="throughput", classes="panel"):
                for key, name in (("specs", "specs written"), ("plans", "plans written"), ("merged", "PRs merged")):
                    yield Label(name)
                    yield Sparkline([0, 0], id=f"spark-{key}")
                    yield Label("", id=f"spark-{key}-sub", classes="dim")
            waits = DataTable(id="waits", show_cursor=False, classes="panel")
            waits.add_columns("column", "cards", "oldest", "median")
            yield waits
        with Horizontal(classes="row"):
            decide = DataTable(id="decide", cursor_type="row", show_header=False, classes="panel")
            decide.add_columns("what", "then")
            yield decide
            yield Static(Text(""), id="health", classes="panel")
            yield StandupPanel(id="standup-panel", classes="panel")

    def on_mount(self):
        self.query_one("#throughput").border_title = "Throughput · last 14 days"
        self.query_one("#waits").border_title = "Where work waits"
        self.query_one("#decide").border_title = "Decide next"
        self.query_one("#health").border_title = "Health"

    def show(self, data, window):
        bar = Text("Window  ", style="dim")
        for i, (_, label, _) in enumerate(WINDOWS):
            bar.append(f" {label} ", style="bold #e0a040 reverse" if i == window else "")
            bar.append("  ")
        bar.append(f"   profile {C.PROFILE_NAME or 'legacy'} · w changes window · r refresh", style="dim")
        mem = data.get("memory")
        if mem:
            bar.append(f"   memory: {mem['free'] / 1024 ** 3:.1f} GB free", style="bold red" if mem["low"] else "dim")
            if mem["low"]:
                bar.append("  LOW MEMORY: new agents held", style="bold red reverse")
        m = data.get("machine")
        if m:
            pct = m.get("agent_memory_pct")
            bar.append(f"   machine: {len(m.get('profiles') or [])} profiles, {m.get('live_agents', 0)}/"
                       f"{m.get('max_live_agents', '?')} agents" + (f", {pct}% agent memory" if pct is not None else ""), style="dim")
            if m.get("hold"):
                bar.append(f"  HOLD: {m['hold']}", style="bold red")
        self.query_one("#window-bar", Static).update(bar)
        vals = tile_values(data, window)
        m7 = (data.get("metrics_by_window") or {}).get("7d") or {}
        act = data.get("pr_activity") or {}
        subs = {"specs": f"{m7.get('specs_written', 0)} this week", "plans": f"{m7.get('plans_written', 0)} this week",
                "opened": f"{count_since(act.get('opened'), WINDOWS[2][2])} this week" if act else "GitHub not reachable",
                "merged": f"{count_since(act.get('merged'), WINDOWS[2][2])} this week" if act else "GitHub not reachable",
                "ready": "merge check passed · now", "waiting": "specs, plans, PRs, manual"}
        for key, _ in TILES:
            box = self.query_one(f"#tile-{key}")
            box.query_one(Digits).update(str(vals[key]))
            box.query_one(".tile-sub", Label).update(subs[key])
        daily = data.get("daily") or {}
        series = {"specs": daily.get("specs") or [0] * 14, "plans": daily.get("plans") or [0] * 14,
                  "merged": per_day(act.get("merged"))}
        for key, vals_ in series.items():
            # one point draws a solid block; pad so an empty series is a flat baseline
            self.query_one(f"#spark-{key}", Sparkline).data = vals_ if len(vals_) > 1 else [0, 0, *vals_][-2:]
            self.query_one(f"#spark-{key}-sub", Label).update(
                f"peak {max(vals_, default=0)} · {sum(vals_)} in 14 days" if any(vals_) else "no data yet")
        rows = column_waits(data["snapshot"]["rows"])
        if rows != getattr(self, "_waits", None):   # an unchanged table is not rebuilt
            self._waits = rows
            waits = self.query_one("#waits", DataTable)
            waits.clear()
            for col, n, oldest, median in rows:
                waits.add_row(Text(col), Text(str(n), style="bold"), Text(oldest), Text(median, style="dim"))
        items = decide_next(data["snapshot"]["rows"])
        if items != getattr(self, "_decide", None):   # an unchanged table is not rebuilt; the cursor stays put
            self._decide = items
            table = self.query_one("#decide", DataTable)
            keep = table.cursor_row
            table.clear()
            for key, n, head, sub, colour in items:
                table.add_row(Text(f"› {head}", style=f"bold {colour}" if n else "dim"), Text(sub, style="dim"), key=key)
            table.move_cursor(row=keep)
        self.query_one("#health", Static).update(health(data, window))
        self.query_one(StandupPanel).update_for(data["snapshot"])

    def on_data_table_row_selected(self, e):
        if e.data_table.id != "decide":
            return
        tab, group = DECIDE_TARGET[e.row_key.value]
        self.app.action_tab(tab)
        if group:
            self.app.query_one(NeedsView).select_group(group)


class StandupScreen(ModalScreen):
    """The pl standup text for the last 24 hours, from the last refresh's snapshot; y copies it, esc closes."""
    DEFAULT_CSS = """
    StandupScreen { align: center middle; }
    StandupScreen > Vertical { width: 90; height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    #standup-body { height: 1fr; }
    """
    BINDINGS = [Binding("y", "copy", "copy"), Binding("escape", "close", "close")]

    def __init__(self, snapshot):
        super().__init__()
        self.snapshot, self.text, self.slack = snapshot, "", ""

    def compose(self):
        with Vertical():
            with VerticalScroll(id="standup-body"):
                yield Static(Text("building the standup…", style="dim"), id="standup-text")
            yield Static(Text("y copy for Slack · esc close", style="dim"))

    def on_mount(self):
        self.build()

    @work(thread=True, group="standup")
    def build(self):
        """Off the UI thread: the two PR searches. The board and tmux are not read again."""
        start = standup.parse_since(None)
        try:
            prs = standup.pr_summary(start)
            got, slack = standup.text(self.snapshot, start, prs), standup.text(self.snapshot, start, prs, fmt="slack")
        except Exception as e:  # noqa: BLE001 - a bad event line must not end the console
            got = slack = f"could not build the standup: {type(e).__name__}: {e}"
        self.app.call_from_thread(self.shown, got, slack)

    def shown(self, got, slack):
        self.text, self.slack = got, slack
        self.query_one("#standup-text", Static).update(Text(got))

    def action_copy(self):
        if not self.text:
            self.app.notify("still building", markup=False)
            return
        copy_text(self.app, self.slack)
        self.app.notify("copied for Slack", markup=False)

    def action_close(self):
        self.dismiss(None)
