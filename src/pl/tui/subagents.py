"""Background tab: the sub-agents a harness session spawned (Claude Code's Agent tool) and the selected one's transcript.

Claude Code writes each sub-agent to <account config_dir>/projects/<project>/<session id>/subagents/agent-<id>.jsonl,
with agent-<id>.meta.json beside it (agentType, description). Only those files are read, and only their tail."""
import json
import os
import re
import stat
import shutil
import time
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from pl import config as C
from pl import harnesses
from pl.trackers import mcp
from pl.tui.loops import COPIERS, _copy_run

RECENT = 6 * 3600          # transcripts modified longer ago are hidden
IDLE = 120                 # seconds without a write before an agent counts as stopped
TAIL_BYTES = 256 * 1024    # never read more than this from one transcript
TAIL_ENTRIES = 60
REFRESH = 3
META_BYTES = 64 * 1024
TOKEN_RE = re.compile(r"(?i:bearer\s+)[A-Za-z0-9._~+/=-]{8,}|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                      r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
BIG = "last entry larger than 256 KB"
_cache = {}   # path -> ((size, mtime_ns), entries)
_meta_cache = {}   # meta path -> ((size, mtime_ns, ino), meta)


def mask(text):
    """pl's secret masking (every resolved ${VAR} value), plus common token shapes a transcript may hold."""
    text = mcp._mask(str(text), sorted(mcp._secrets, key=len, reverse=True))
    return TOKEN_RE.sub("***", PEM_RE.sub("***", text))


def _nofollow(p, flags):
    return os.open(p, flags | os.O_NOFOLLOW)


def read_tail(path, cap=TAIL_BYTES, ident=None):
    """The transcript's last entries, reading at most cap bytes from the end; a cut first line is dropped.
    ident: the (st_dev, st_ino) that was checked; a different file at the path is not read."""
    try:
        with open(path, "rb", opener=_nofollow) as f:
            fst = os.fstat(f.fileno())
            if ident and (fst.st_dev, fst.st_ino) != ident:
                return []
            f.seek(0, 2)
            start = max(0, f.tell() - cap)
            f.seek(start)
            data = f.read(cap)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(e, dict) and isinstance(e.get("message", {}), dict):
            out.append(e)
    return out


def _blocks(e):
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def state(entries, age):
    """running / waiting / error / done, from the last message entry and seconds since the file was written.
    error: the last entry is an API error (isApiErrorMessage or error key), or, once idle, a tool result with is_error.
    running: written in the last 2 minutes. Otherwise, idle for 2 minutes:
    waiting: the last entry is a tool call with no result yet, or a tool result or prompt with no reply yet.
             (A sub-agent cannot ask the user anything, so its text is not read for questions.)
    done: the last entry is assistant text with no pending tool call (or there is nothing to read).
    unknown (set by scan): the file is not empty but its tail holds no whole entry (one entry larger than 256 KB)."""
    msgs = [e for e in entries if isinstance(e.get("message"), dict)]
    last = msgs[-1] if msgs else None
    if last and (last.get("isApiErrorMessage") or last.get("error")):
        return "error"
    if age < IDLE:
        return "running"
    if last is None:
        return "done"
    blocks = _blocks(last)
    if any(b.get("type") == "tool_result" and b.get("is_error") for b in blocks):
        return "error"
    if last["message"].get("role") != "assistant" or any(b.get("type") == "tool_use" for b in blocks):
        return "waiting"
    return "done"


def _short_input(inp):
    if isinstance(inp, dict):
        for k in ("command", "file_path", "pattern", "path", "url", "description", "query", "prompt"):
            if isinstance(inp.get(k), str):
                return inp[k]
    return json.dumps(inp, ensure_ascii=False, default=str)


def _first_line(v):
    if isinstance(v, list):
        v = " ".join(str(b.get("text") or "") for b in v if isinstance(b, dict))
    s = mask(v or "").strip()
    return s.splitlines()[0] if s else ""


def render(entries):
    """Plain text lines, oldest first: role text, tool name + short input, the first line of each tool result.
    Raw strings are masked before they are cut or json-encoded."""
    out, secrets = [], sorted(mcp._secrets, key=len, reverse=True)
    for e in entries:
        role = (e.get("message") or {}).get("role")
        ts = str(e.get("timestamp") or "")[11:19]
        err = e.get("isApiErrorMessage") or e.get("error")
        for b in _blocks(e):
            kind = b.get("type")
            if kind == "text":
                body = mask(b.get("text") or "").strip().splitlines()[:6]
                if body:
                    out.append(f"{ts} {'API error' if err else role}: {body[0]}")
                    out.extend(f"         {x}" for x in body[1:])
            elif kind == "tool_use":
                out.append(f"{ts} tool {b.get('name')}: {mask(_short_input(mcp._mask(b.get('input'), secrets)))}"[:200])
            elif kind == "tool_result":
                out.append(f"{ts} {'result (error)' if b.get('is_error') else 'result'}: {_first_line(b.get('content'))}"[:200])
    return out


def tail_lines(path, ident=None):
    """The rendered last entries, or one line saying the last entry is too big to show."""
    lines = render(read_tail(path, ident=ident)[-TAIL_ENTRIES:])
    try:
        return lines or ([BIG] if os.stat(path).st_size else [])
    except OSError:
        return lines


def _age(sec):
    return "<1m" if sec < 60 else f"{int(sec // 60)}m" if sec < 3600 else f"{int(sec // 3600)}h"


def _entries(path, st):
    key = (st.st_size, st.st_mtime_ns)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    entries = read_tail(path, ident=(st.st_dev, st.st_ino))[-TAIL_ENTRIES:]
    _cache[path] = (key, entries)
    return entries


def _meta(root, path):
    """The sidecar agent-<id>.meta.json: a plain file, not a link to a credential file, at most 64 KB; cached."""
    m = path.with_name(path.name[:-len(".jsonl")] + ".meta.json")
    try:
        st = m.lstat()
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
        hit = _meta_cache.get(m)
        if hit and hit[0] == key:
            return hit[1]
        if not stat.S_ISREG(st.st_mode) or st.st_size > META_BYTES or harnesses.is_secret_file(root, st):
            return {}
        with open(m, "rb", opener=_nofollow) as f:
            fst = os.fstat(f.fileno())
            d = json.loads(f.read(META_BYTES)) if (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino) else {}
    except (OSError, ValueError, RecursionError):
        return {}
    d = d if isinstance(d, dict) else {}
    _meta_cache[m] = (key, d)
    return d


def scan(data=None):
    """(rows newest first, notes): Claude accounts' sub-agents written in the last 6 hours; one note per other harness."""
    cards = {}
    for r in ((data or {}).get("snapshot") or {}).get("rows") or []:
        sid, c = (r.get("worker") or {}).get("session_id"), r.get("card")
        if sid and c:
            cards[sid] = f"{str(c.get('id'))[:8]} {c.get('title') or ''}".strip()
    rows, notes, seen, now = [], [], set(), time.time()
    for name in C.ACCOUNTS:
        h = (C.ACCOUNTS.get(name) or {}).get("harness") or "claude"
        if h != "claude":
            notes.append(f"no background-agent view for {h}")
            continue
        root = Path(C.PROFILES.get(name) or "").expanduser()
        try:
            if (root / "projects").is_symlink():   # a linked projects folder would widen what may be read
                continue
            projects = (root / "projects").resolve()
        except (OSError, RuntimeError):
            continue
        for p in projects.glob("*/*/subagents/agent-*.jsonl"):
            try:
                st = p.stat()
                if now - st.st_mtime > RECENT:
                    continue
                r = p.resolve()
                if not r.is_relative_to(projects) or harnesses.is_secret_file(root, st):
                    continue
            except (OSError, RuntimeError):
                continue
            meta, session, entries = _meta(root, p), p.parent.parent.name, _entries(r, st)
            seen.add(r)
            what = ": ".join(str(x) for x in (meta.get("agentType"), meta.get("description")) if x)
            rows.append({"path": r, "ident": (st.st_dev, st.st_ino), "agent": p.name[len("agent-"):-len(".jsonl")], "parent": session[:8],
                         "card": cards.get(session, "-"), "what": mask(what or "-"),
                         "state": state(entries, now - st.st_mtime) if entries or not st.st_size else "unknown",
                         "age": now - st.st_mtime,
                         "key": (st.st_size, st.st_mtime_ns)})
    rows.sort(key=lambda r: r["age"])
    for k in [k for k in _cache if k not in seen]:
        del _cache[k]
    for k in [k for k in _meta_cache if k.with_name(k.name[:-len(".meta.json")] + ".jsonl") not in seen]:
        del _meta_cache[k]
    return rows, notes


class SubagentsView(Widget):
    DEFAULT_CSS = """
    SubagentsView { height: 1fr; }
    #subagents-status { height: 1; margin: 0 1; }
    #subagents-table { width: 90; height: 1fr; border: round $panel-lighten-2; }
    #subagents-screen { width: 1fr; height: 1fr; border: round $panel-lighten-2; padding: 0 1; }
    """
    BINDINGS = [Binding("y", "copy", "copy")]

    def __init__(self):
        super().__init__()
        self.data, self.rows, self.lines, self.redraws = None, [], [], 0
        self._shown, self._tail_key = None, None

    def compose(self):
        with Vertical():
            yield Static(id="subagents-status")
            with Horizontal():
                yield DataTable(id="subagents-table", cursor_type="row", zebra_stripes=True)
                yield Static(Text("select a background agent", style="dim"), id="subagents-screen", markup=False)

    def on_mount(self):
        self.query_one(DataTable).add_columns("parent", "card", "agent", "state", "last active")
        self.set_interval(REFRESH, self.tick)

    def show(self, data):
        self.data = data

    def selected(self):
        t = self.query_one(DataTable)
        return self.rows[t.cursor_row] if self.rows and 0 <= t.cursor_row < len(self.rows) else None

    def tick(self):
        """Scan and read the selected tail off the UI thread, only while this tab is visible."""
        if getattr(self.app, "active_tab", None) != "subagents":
            return
        sel = self.selected()
        want, data, last_key = sel and sel["path"], self.data, self._tail_key

        def run():
            try:
                rows, notes = scan(data)
                pick = next((r for r in rows if r["path"] == want), rows[0] if rows else None)
                lines = None
                if pick and (pick["path"], pick["key"], pick["state"]) != last_key:   # state also moves with time
                    lines = tail_lines(pick["path"], pick["ident"])
            except Exception as e:  # noqa: BLE001 - a worker that raises kills the app
                self.app.call_from_thread(self.app.notify, f"could not list background agents: {e}", severity="error", markup=False)
                return
            self.app.call_from_thread(self._apply, rows, notes, pick, lines)
        self.run_worker(run, thread=True, group="subagents", exclusive=True)

    def _apply(self, rows, notes, pick, lines):
        status = Text(f"{len(rows)} background agents active in the last 6 h", style="dim")
        for n in notes:
            status.append(f"   {n}", style="dim")
        self.query_one("#subagents-status", Static).update(status)
        t = self.query_one(DataTable)
        shown = [(r["parent"], r["card"], r["what"], r["state"], _age(r["age"])) for r in rows]
        self.rows = rows
        if shown != self._shown:   # an unchanged table is not rebuilt
            self._shown = shown
            t.clear()
            for x in shown:
                t.add_row(*(Text(str(v)) for v in x))
        if pick is not None:
            t.move_cursor(row=next(i for i, r in enumerate(rows) if r["path"] == pick["path"]))
        if lines is not None:   # the file changed: redraw
            self._tail_key, self.lines = (pick["path"], pick["key"], pick["state"]), lines
            body = Text(f"{pick['what']}  ({pick['state']})\n", style="bold")
            body.append("\n".join(lines[-TAIL_ENTRIES:]), style="not bold")
            self.query_one("#subagents-screen", Static).update(body)
            self.redraws += 1

    def on_data_table_row_highlighted(self, _):
        sel = self.selected()
        if sel is not None and (self._tail_key or (None,))[0] != sel["path"]:
            self.tick()

    def action_copy(self):
        if not self.lines:
            self.app.notify("select a background agent first", markup=False)
            return
        text = "\n".join(self.lines[-TAIL_ENTRIES:])
        self.app.copy_to_clipboard(text)
        for argv in COPIERS:
            if shutil.which(argv[0]):
                _copy_run(argv, text)
                break
        self.app.notify(f"copied {len(self.lines[-TAIL_ENTRIES:])} lines", markup=False)
