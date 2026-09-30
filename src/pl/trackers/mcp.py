"""MCP tracker: any MCP server is the board once config maps pl's card actions to its tools.

[tracker] type = "mcp"; mcp_config = "<the harness's MCP config file>" and server = "<name>": pl reads that
server's entry ({"mcpServers": {name: {command, args, env} | {url, headers}}}) when the tracker starts and uses
it as-is; its env and header values are masked like secrets. Or inline: server = { command, args, env } (stdio)
or { url, headers } (streamable HTTP), whose strings may hold ${VAR}, read from the environment when the
session opens; resolved values never reach logs or error text. [tracker.tools.<action>] for columns, cards, card, create, update,
delete, ensure_column (and optional move): tool, args (values may hold {board_id}, {item_id},
{column_id}, {column}, {title}, {description}; a value exactly "{name}" becomes the raw value, so
"{fields}" is the write's field dict; the key "*" with "{fields}" spreads the fields into the top-level
arguments, for servers that take flat write arguments), result (dotted path into the reply's JSON), fields (server key ->
pl key, applied to replies and reversed on writes); columns also takes title_key / id_key; cards also
takes page (the server's page-number argument) and page_size, and then reads page after page until a short one;
a page the server cut at its size cap is read again one card at a time (page_size_arg, default "per_page", must be
in args), and a card cut even alone is skipped with one error event per card per hour.

One MCP session per server config per process: a daemon thread runs an asyncio loop whose single task
owns the session and serves calls from a queue; it opens on first use and closes at exit. Each call's
timeout is enforced inside that loop, so a stuck call ends the session normally and the SDK stops the
server process. A stdio server's stderr goes through a pipe to STATE_DIR/mcp-<name or hash>.log (0600),
with resolved secrets masked line by line.
"""
import asyncio
import atexit
import concurrent.futures
import contextlib
import hashlib
import importlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from pl import config as C

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PH_RE = re.compile(r"\{(\w+)\}")
CUT_RE = re.compile(r"\n[^\n]*\[truncated\b[^\n]*\s*\Z", re.I)   # the marker line a size-capped server appends

_sessions = {}
_lock = threading.Lock()
MAX_PAGES = 100  # cards: at most this many pages per read
SKIP_EVERY = 3600  # seconds between error events for the same card skipped from the list
_skipped = {}  # (board_id, card id) -> when its last skip event was emitted
CLOSE_WAIT = 10  # seconds close() lets an in-flight call finish before cancelling it
_secrets = set()  # every resolved ${VAR} value; masked in the SDK's and httpx's log records


class _Mask(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        secrets = sorted(_secrets, key=len, reverse=True)
        if any(s in msg for s in secrets):
            for s in secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True


_MASK = _Mask()


def _mask_loggers():
    """Attach the mask to the mcp/httpx/httpcore loggers and their children (imported first so they exist).

    A logger's filter sees only records logged on that logger itself, so each child needs its own.
    """
    for mod in ("httpx", "mcp.client.session", "mcp.client.stdio", "mcp.client.streamable_http", "mcp.shared.session"):
        importlib.import_module(mod)
    for name in ("mcp", "httpx", "httpcore", *logging.root.manager.loggerDict):
        if name.split(".")[0] in ("mcp", "httpx", "httpcore"):
            lg = logging.getLogger(name)
            if _MASK not in lg.filters:
                lg.addFilter(_MASK)


def _resolve(v, secrets):
    if isinstance(v, str):
        def sub(m):
            val = os.environ.get(m.group(1))
            if not val:
                raise SystemExit(f"pl: set {m.group(1)} (the MCP tracker server config uses it)")
            secrets.append(val)
            _secrets.add(val)
            return val
        return VAR_RE.sub(sub, v)
    if isinstance(v, dict):
        return {k: _resolve(x, secrets) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(x, secrets) for x in v]
    return v


ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(server, secrets):
    """The harness file's entry with ${VAR} and ${VAR:-default} expanded in command, args, env, url and headers."""
    def sub(m):
        val = os.environ.get(m.group(1))
        if not val:
            if m.group(2) is None:
                raise SystemExit(f"pl: set {m.group(1)} (the MCP server entry in the harness's MCP config uses it)")
            val = m.group(2)
        if val:
            secrets.append(val)
        return val

    def ex(v):
        if isinstance(v, str):
            return ENV_RE.sub(sub, v)
        if isinstance(v, list):
            return [ex(x) for x in v]
        if isinstance(v, dict):
            return {k: ex(x) for k, x in v.items()}
        return v
    return {k: ex(v) if k in ("command", "args", "env", "url", "headers") else v for k, v in server.items()}


def _as_is_secrets(server):
    """Values to mask for a harness-file entry: env and header values, every args string, and the url except
    scheme and host (path and its segments, query and each query value, userinfo). Strings of 3 characters or
    fewer are left alone (flags like -c)."""
    out = [v for k in ("env", "headers") for v in (server.get(k) or {}).values() if isinstance(v, str) and v]
    out += [a for a in server.get("args") or [] if isinstance(a, str) and len(a) > 3]
    url = server.get("url")
    if isinstance(url, str) and url:
        from urllib.parse import unquote, urlsplit
        u = urlsplit(url)
        rest = url.split(u.netloc, 1)[-1] if u.netloc else url
        parts = [rest if u.query or u.fragment else "", *u.path.split("/"), u.query, u.fragment, u.netloc.rpartition("@")[0] if "@" in u.netloc else ""]
        parts += [x for kv in u.query.split("&") for x in kv.split("=", 1)[1:]]
        parts += [u.username or "", u.password or ""]
        out += [x for p in parts if p for x in {p, unquote(p)} if len(x) > 3]
    return list(dict.fromkeys(out))


def _expand(v, ctx):
    if isinstance(v, str):
        m = PH_RE.fullmatch(v)
        if m and m.group(1) in ctx:
            return ctx[m.group(1)]
        return PH_RE.sub(lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), v)
    if isinstance(v, dict):
        return {k: _expand(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x, ctx) for x in v]
    return v


def _mask(obj, secrets):
    """obj with every secret masked in its strings (before json.dumps, whose escaping could hide one)."""
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "***")
        return obj
    if isinstance(obj, dict):
        return {_mask(k, secrets): _mask(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v, secrets) for v in obj]
    return obj


def _describe(e):
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


class _Cut(SystemExit):
    """A reply the server cut at its size cap."""


class _Session:
    """One ClientSession on a background loop. Calls are serialised through the loop's queue."""

    def __init__(self, server, timeout, as_is=False):
        # The SDK and httpx log whole messages and URLs at DEBUG/INFO; those can carry resolved secrets.
        for name in ("mcp", "httpx", "httpcore"):
            if logging.getLogger(name).level == logging.NOTSET:
                logging.getLogger(name).setLevel(logging.WARNING)
        self.timeout = timeout
        self.secrets = []
        self.name = server.get("name") or hashlib.sha256(json.dumps(server, sort_keys=True, default=str).encode()).hexdigest()[:12]
        if as_is:  # from the harness's MCP config file: ${VAR} / ${VAR:-default} expanded as the harness does
            self.server = _expand_env(server, self.secrets)
            self.secrets += _as_is_secrets(self.server)
            self.secrets.sort(key=len, reverse=True)   # a longer secret first, so a shorter one inside it cannot split it
            _secrets.update(self.secrets)
        else:
            self.server = _resolve(server, self.secrets)
        self.errlog = self.errthread = None
        self.done = threading.Event()  # set when _serve has fully exited (the SDK has stopped the server)
        _mask_loggers()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="pl-mcp", daemon=True)
        self.thread.start()
        self.ready = concurrent.futures.Future()
        self.queue = None
        self.main = asyncio.run_coroutine_threadsafe(self._serve(), self.loop)
        try:
            self.ready.result(timeout)
        except concurrent.futures.TimeoutError:
            self.close()
            raise SystemExit(f"pl: MCP tracker server did not start within {timeout:g}s") from None
        except SystemExit:
            self.close()
            raise
        except BaseException as e:
            self.close()
            raise SystemExit(f"pl: MCP tracker server failed to start: {self.scrub(_describe(e), 300)}") from None

    def scrub(self, text, limit=None):
        """Mask every resolved secret, then cut (cutting first could leave a secret's prefix)."""
        for s in self.secrets:
            text = text.replace(s, "***")
        return text if limit is None else text[:limit]

    def _errlog(self):
        """The server's stderr: a pipe whose lines are masked and appended to the 0600 log file."""
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = C.STATE_DIR / f"mcp-{re.sub(r'[^A-Za-z0-9_-]', '_', self.name)}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        r, w = os.pipe()

        def copy():
            with os.fdopen(fd, "a") as out, os.fdopen(r, errors="replace") as src:
                for line in src:
                    out.write(self.scrub(line))
                    out.flush()

        self.errthread = threading.Thread(target=copy, name="pl-mcp-stderr", daemon=True)
        self.errthread.start()
        self.errlog = os.fdopen(w, "w")
        return self.errlog

    def _transport(self):
        s = self.server
        if s.get("url"):
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
            return streamable_http_client(s["url"], http_client=create_mcp_http_client(headers=s.get("headers") or {}))
        if s.get("command"):
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            return stdio_client(StdioServerParameters(command=s["command"], args=list(s.get("args") or []),
                                                      env=s.get("env") or None), errlog=self._errlog())
        raise SystemExit("pl: [tracker] server needs command (stdio) or url (streamable HTTP)")

    async def _serve(self):
        from mcp import ClientSession
        try:
            async with contextlib.AsyncExitStack() as stack:
                streams = await stack.enter_async_context(self._transport())
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                self.queue = asyncio.Queue()
                self.ready.set_result(None)
                while (job := await self.queue.get()) is not None:
                    fn, fut, deadline = job
                    left = deadline - time.monotonic()
                    if left <= 0:  # the caller's time ran out while queued; nothing was sent
                        fut.set_exception(SystemExit(f"pl: MCP tracker call timed out after {self.timeout:g}s"))
                        continue
                    try:
                        fut.set_result(await asyncio.wait_for(fn(session), left))
                    except TimeoutError:
                        fut.set_exception(SystemExit(f"pl: MCP tracker call timed out after {self.timeout:g}s"))
                        break
                    except Exception as e:
                        fut.set_exception(e)
        except BaseException as e:
            if not self.ready.done():
                self.ready.set_exception(e)
            if not isinstance(e, (Exception, asyncio.CancelledError)):
                raise
        finally:
            self._fail_queued()
            if self.errlog:
                self.errlog.close()
            self.done.set()

    def _fail_queued(self):
        """Queued callers fail now instead of at their own timeout."""
        while self.queue is not None and not self.queue.empty():
            job = self.queue.get_nowait()
            if job is not None and not job[1].done():
                job[1].set_exception(SystemExit("pl: MCP tracker session closed before the call ran"))

    def run(self, fn):
        """fn(session) on the loop, within self.timeout of now (time spent queued counts)."""
        fut = concurrent.futures.Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (fn, fut, time.monotonic() + self.timeout))
        try:
            return fut.result(self.timeout + 15)  # the loop enforces the deadline; this only guards a dead loop
        except concurrent.futures.TimeoutError:
            raise SystemExit(f"pl: MCP tracker call timed out after {self.timeout:g}s") from None

    def close(self):
        if self.queue is not None and not self.main.done():
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        with contextlib.suppress(Exception):
            self.main.result(CLOSE_WAIT)
        if not self.main.done():
            self.main.cancel()
        self.done.wait(5)  # the cancelled task still runs the SDK's shutdown (close stdin, wait, terminate)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        if self.errthread:
            self.errthread.join(2)


def _session(server, timeout, as_is=False):
    key = json.dumps([server, timeout, as_is], sort_keys=True, default=str)
    with _lock:
        s = _sessions.get(key)
        if s is None or s.main.done():
            s = _sessions[key] = _Session(server, timeout, as_is)
        return key, s


def _drop(key, s):
    """Close s and forget it, unless another caller already replaced it with a new session."""
    with _lock:
        if _sessions.get(key) is s:
            del _sessions[key]
    s.close()


def close_all():
    """Close every open MCP session (registered with atexit; tests call it too)."""
    with _lock:
        ss = list(_sessions.values())
        _sessions.clear()
    for s in ss:
        s.close()


atexit.register(close_all)


def _from_file(path, name):
    """Server `name`'s entry in the harness's MCP config file. Errors name the path and server, never a value."""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = (C.CONFIG_DIR or Path.cwd()) / p
    try:
        servers = json.loads(p.read_text())["mcpServers"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"pl: cannot read MCP server {name!r} from {p} ({type(e).__name__})") from None
    if not isinstance(name, str) or not isinstance(servers, dict) or not isinstance(servers.get(name), dict):
        raise SystemExit(f"pl: {p} has no MCP server {name!r}")
    return {"name": name, **servers[name]}


class Mcp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.server = cfg.get("server")
        self.as_is = bool(cfg.get("mcp_config"))
        if self.as_is:
            self.server = _from_file(cfg["mcp_config"], self.server)
        if not isinstance(self.server, dict):
            raise SystemExit('pl: [tracker] type = "mcp" needs mcp_config = "<file>" and server = "<name>", '
                             'or server = { command, args, env } or { url, headers }')
        self.tools = cfg.get("tools") or {}
        self.board_id = cfg.get("board_id", "")
        self.timeout = float(cfg.get("timeout") or 60)
        self._columns = None

    def _spec(self, action):
        spec = self.tools.get(action)
        if not spec or not spec.get("tool"):
            raise SystemExit(f"pl: the MCP tracker has no [tracker.tools.{action}] mapping")
        return spec

    def _run(self, fn):
        key, s = _session(self.server, self.timeout, self.as_is)
        try:
            return s.run(fn), s
        except SystemExit:
            _drop(key, s)
            raise
        except Exception as e:
            _drop(key, s)
            raise SystemExit(f"pl: MCP tracker call failed: {s.scrub(_describe(e), 300)}") from None

    def _call(self, action, extra=None, **ctx):
        spec = self._spec(action)
        tool = spec["tool"]
        args = _expand(spec.get("args") or {}, {"board_id": self.board_id, **ctx})
        spread = args.pop("*", None)
        if isinstance(spread, dict):
            args = {**spread, **args}   # mapped arguments win over a field of the same name
        args.update(extra or {})
        res, s = self._run(lambda session: session.call_tool(tool, args))
        texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
        text = texts[0] if texts else ""
        if res.isError:
            raise SystemExit(f"pl: MCP tool {tool} ({action}) failed: {s.scrub(text, 200)}")
        if not text.strip():
            return {}
        try:
            obj = json.loads(text)
        except ValueError:
            if cut := CUT_RE.search(text):   # a size-capped server sliced the reply: never parse the partial JSON
                if action == "cards":
                    call = ", ".join(f"{k}={v}" for k, v in args.items() if k in ("action", "page", "per_page", "fields"))
                    raise _Cut(s.scrub(f"pl: the board server cut the card list reply ({tool}: {call}) at "
                                       f"{cut.start():,} characters; page it smaller", 200)) from None
                raise _Cut(f"pl: the board server cut this card off at {cut.start():,} characters; it is too large "
                           "to read \u2014 split it into smaller cards") from None
            raise SystemExit(f"pl: MCP tool {tool} ({action}): non-JSON reply: {s.scrub(text, 200)}") from None
        for part in spec["result"].split(".") if spec.get("result") else []:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
                obj = obj[int(part)]
            else:
                raise SystemExit(f"pl: MCP tool {tool} ({action}): reply has no {spec['result']!r}")
        return obj

    def _norm(self, action, item):
        """The card dict shape, after the action's fields map (server key -> pl key)."""
        if not isinstance(item, dict):
            raise SystemExit(f"pl: MCP tracker {action}: expected an item, got {type(item).__name__}")
        out = dict(item)
        for sk, pk in (self.tools.get(action, {}).get("fields") or {}).items():
            if sk in item:
                out[pk] = item[sk]
        for k, d in (("title", ""), ("description", ""), ("tags", []), ("metadata", {}), ("list_id", None),
                     ("updated_at", ""), ("assigned_to", None)):
            if out.get(k) is None:
                out[k] = d
        return out

    def _out(self, action, fields):
        rev = {pk: sk for sk, pk in (self.tools.get(action, {}).get("fields") or {}).items()}
        return {rev.get(k, k): v for k, v in fields.items()}

    def _col(self, name):
        if name not in self.columns():
            raise SystemExit(f"pl: the board has no column {name!r} (for Inbox run: pl board init)")
        return self.columns()[name]

    def columns(self):
        if self._columns is None:
            spec = self._spec("columns")
            tk, ik = spec.get("title_key") or "title", spec.get("id_key") or "id"
            rows = self._call("columns")
            if not isinstance(rows, list):
                raise SystemExit("pl: MCP tracker columns: expected a list")
            for r in rows:
                for k in (tk, ik):
                    if not isinstance(r, dict) or k not in r:
                        raise SystemExit(f"pl: MCP tool {spec['tool']} (columns): a row has no {k!r} key")
            self._columns = {r[tk]: r[ik] for r in rows}
        return self._columns

    def cards(self, query=None):
        """Every card. Only query keys in the mapping's query table (pl key -> server argument) reach the
        server; the rest are dropped (callers still filter client-side)."""
        spec = self.tools.get("cards", {})
        qmap = spec.get("query") or {}
        extra = {qmap[k]: v for k, v in (query or {}).items() if k in qmap}
        pk, size = spec.get("page"), int(spec.get("page_size") or 1)
        sk = spec.get("page_size_arg") or "per_page"
        rows = []
        for n in range(1, MAX_PAGES + 1):
            try:
                page = self._page(extra, {pk: n} if pk else {})
            except _Cut:
                if not (pk and size > 1 and sk in (spec.get("args") or {})):
                    raise
                page = [self._one(extra, pk, sk, i) for i in range((n - 1) * size + 1, n * size + 1)]
                page = page[:page.index([])] if [] in page else page   # the list ended inside this page
            rows += page
            if not pk or len(page) < size:
                break
        return [self._norm("cards", r) for r in rows if r is not None]

    def _page(self, extra, paging):
        page = self._call("cards", extra={**extra, **paging})
        if not isinstance(page, list):
            raise SystemExit("pl: MCP tracker cards: expected a list")
        return page

    def _one(self, extra, pk, sk, i):
        """Card number i of the list read alone: its row, [] past the end, or None when it is too large even alone."""
        try:
            got = self._page(extra, {pk: i, sk: 1})
            return got[0] if got else []
        except _Cut:
            pass
        try:
            cid = str((self._page(extra, {pk: i, sk: 1, "fields": "id"}) or [{}])[0].get("id") or "")
        except SystemExit:
            cid = ""
        key, now = (self.board_id, cid or i), time.time()
        if now - _skipped.get(key, 0) >= SKIP_EVERY:
            _skipped[key] = now
            from pl import events
            events.emit("error", cid or None, message=f"pl: skipped card {cid or f'number {i}'} while listing the board: "
                        "the server cuts it off even alone; split it into smaller cards")
        return None

    def card(self, item_id):
        got = self._call("card", item_id=item_id)
        if isinstance(got, list):
            got = got[0] if got else None
        if not got:
            raise SystemExit(f"pl: no card with id {item_id}")
        return self._norm("card", got)

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        fields = {"title": title, "description": description, "tags": list(tags), "metadata": dict(metadata or {}),
                  **({"assigned_to": assigned_to} if assigned_to else {})}
        got = self._call("create", column=column, column_id=self._col(column), title=title, description=description,
                         fields=self._out("create", fields))
        return self._norm("create", got) if isinstance(got, dict) and got else {}

    def update(self, item_id, *, verify=True, **fields):
        """Write with a client-side metadata merge, then re-read and assert every sent field persisted.

        verify=False sends fields exactly as given: no pre-read, no check. column= goes through the
        move tool when one is mapped, else as list_id in the update's fields.
        """
        column = fields.pop("column", None)
        col_id = self._col(column) if column is not None else None
        move = column is not None and "move" in self.tools
        if column is not None and not move:
            fields["list_id"] = col_id
        if verify and "metadata" in fields:
            fields["metadata"] = {**(self.card(item_id).get("metadata") or {}), **fields["metadata"]}
        got = {}
        if fields or not move:  # fields first: a failed write must not leave the card moved
            got = self._call("update", item_id=item_id, fields=self._out("update", fields))
        if move:
            got = self._call("move", item_id=item_id, column=column, column_id=col_id)
        if not verify:
            return self._norm("update", got) if isinstance(got, dict) and got else {}
        got = self.card(item_id)
        if move:
            fields["list_id"] = col_id
        for k, v in fields.items():
            if k == "metadata":
                for mk, mv in v.items():
                    if (got.get("metadata") or {}).get(mk) != mv:
                        raise SystemExit(f"pl: metadata.{mk} did not persist on {str(item_id)[:8]}")
            elif (got.get(k) or "") != (v or ""):
                raise SystemExit(f"pl: field {k} did not persist on {str(item_id)[:8]}")
        return got

    def delete(self, item_id):
        self._call("delete", item_id=item_id)

    def ensure_column(self, name, *, description=""):
        if name in self.columns():
            return self.columns()[name]
        col = self._call("ensure_column", column=name, title=name, description=description)
        ik = self._spec("ensure_column").get("id_key") or "id"
        if not isinstance(col, dict) or not col.get(ik):
            text = json.dumps(_mask(col, list(_secrets)), default=str, ensure_ascii=False)
            raise SystemExit(f"pl: create column {name!r} failed: {text[:300]}")
        self._columns[name] = col[ik]
        return col[ik]

    def url(self, item_id):
        tpl = self.cfg.get("card_url")
        return _expand(tpl, {"board_id": self.board_id, "item_id": item_id}) if tpl else str(item_id)

    def test(self):
        """list_tools, then name any mapped tool the server lacks."""
        res, _ = self._run(lambda session: session.list_tools())
        have = {t.name for t in res.tools}
        missing = sorted({s["tool"] for s in self.tools.values() if isinstance(s, dict) and s.get("tool")} - have)
        if missing:
            return f"problem: the MCP server is missing mapped tools: {', '.join(missing)}"
        return f"ok: mcp board {self.board_id}, {len(self.columns())} columns, {len(have)} server tools"
