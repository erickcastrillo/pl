"""WP6: any MCP server is the tracker once config maps pl's card actions to its tools.

Every test talks to a real MCP server (FastMCP, run with sys.executable from tmp_path) over the real protocol.
"""
import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pl import config as C
from pl import trackers
from pl.trackers import mcp as M

SERVER = textwrap.dedent('''
    import json, os, sys
    from mcp.server.fastmcp import FastMCP, Context

    srv = FastMCP("fake-board", host="127.0.0.1", port=int(os.environ.get("PORT") or 0) or 8000)
    COLS = {"col-1": "Inbox", "col-2": "Spec ready"}
    ITEMS = {}

    def shape(i):
        return dict(i)

    @srv.tool()
    def boards(action: str, board_id: str = "", item_id: str = "", column_id: str = "", title: str = "",
               description: str = "", fields: dict | None = None) -> str:
        """A fake item/board tool backed by a dict."""
        if board_id != "b-1":
            raise ValueError("unknown board " + board_id)
        if action == "lists":
            return json.dumps({"lists": [{"name": v, "key": k} for k, v in COLS.items()]})
        if action == "list_create":
            k = "col-%d" % (len(COLS) + 1)
            COLS[k] = title
            return json.dumps({"list": {"name": title, "key": k, "about": description}})
        if action == "item_list":
            return json.dumps({"data": {"items": [shape(i) for i in ITEMS.values()]}})
        if action == "item_get":
            if item_id not in ITEMS:
                raise ValueError("no item " + item_id)
            return json.dumps({"item": shape(ITEMS[item_id])})
        if action == "item_create":
            n = "it-%d" % (len(ITEMS) + 1)
            f = dict(fields or {})
            ITEMS[n] = {"id": n, "title": f.get("title", ""), "description": f.get("description", ""),
                        "tags": f.get("tags", []), "metadata": f.get("metadata", {}), "column": column_id,
                        "modified": "2026-09-28T10:00:00+00:00", "assigned_to": f.get("assigned_to")}
            return json.dumps({"item": ITEMS[n]})
        if action == "item_update":
            ITEMS[item_id].update(fields or {})
            return json.dumps({"item": ITEMS[item_id]})
        if action == "item_update_lossy":
            f = dict(fields or {})
            f.pop("title", None)
            ITEMS[item_id].update(f)
            return json.dumps({"item": ITEMS[item_id]})
        if action == "item_delete":
            ITEMS.pop(item_id)
            return ""
        if action == "garbage":
            return "not json at all"
        if action == "cut":   # what a size-capped server sends for a card over its reply limit
            full = json.dumps({"item": {"id": "it-9", "title": "big", "description": "x" * 60000}})
            return full[:48000] + "\\n...[truncated to fit; use fields/section/query/pagination for the rest]"
        raise ValueError("unknown action " + action)

    @srv.tool()
    def whoami(ctx: Context) -> str:
        """Says which process answered and whether the expected secret arrived."""
        req = getattr(ctx.request_context, "request", None)
        header = req.headers.get("authorization") if req is not None else None
        return json.dumps([{"id": str(os.getpid()), "title": "pid",
                            "env_ok": os.environ.get("FAKE_TOKEN") == os.environ.get("EXPECT"),
                            "header_ok": header == "Bearer " + (os.environ.get("EXPECT") or "")}])

    @srv.tool()
    def leaky(ctx: Context) -> str:
        """A careless server that echoes the caller's auth header in its error."""
        raise ValueError("bad auth: " + str(ctx.request_context.request.headers.get("authorization")))

    @srv.tool()
    def echo_env(pad: int = 0, fail: bool = False) -> str:
        """Pads the env secret so it straddles a cut; raises it (isError) or returns it (non-JSON)."""
        text = "x" * pad + (os.environ.get("FAKE_TOKEN") or "")
        if fail:
            raise ValueError(text)
        return text

    @srv.tool()
    def slow(seconds: float = 30) -> str:
        """Hangs mid-call."""
        import time
        time.sleep(seconds)
        return "[]"

    @srv.tool()
    def seen(column_id: str = "", other: str = "") -> str:
        """Echoes the arguments that reached the server."""
        return json.dumps([{"id": "s-1", "column_id": column_id, "other": other}])

    @srv.tool()
    def leak_reply(title: str = "") -> str:
        """A column reply without an id that echoes the env secret."""
        return json.dumps({"error": "denied for " + (os.environ.get("FAKE_TOKEN") or "")})

    @srv.tool()
    def stderr_leak() -> str:
        """Prints the env secret to stderr."""
        print("server saw token " + (os.environ.get("FAKE_TOKEN") or ""), file=sys.stderr, flush=True)
        return "[]"

    @srv.tool()
    def bad_rows() -> str:
        """Column rows missing the id key."""
        return json.dumps({"lists": [{"name": "Inbox"}]})

    @srv.tool()
    def flat(action: str, item_id: str = "", title: str = "", metadata: dict | None = None, page: int = 1) -> str:
        """Flat write arguments and a paged list of five rows, two per page."""
        if action == "item_list":
            return json.dumps({"rows": [{"id": "f-%d" % i} for i in range(5)][(page - 1) * 2:page * 2]})
        return json.dumps({"item": {"id": item_id, "title": title, "metadata": metadata or {}}})

    @srv.tool()
    def capped(page: int = 1, per_page: int = 10, fields: str = "*") -> str:
        """23 cards, cut like the server's responseGuard (48,000 chars + marker). A page of 10 is over the cap;
        one card alone fits, except c-7 (huge metadata), which fits only when asked for its id alone."""
        rows = [{"id": "c-%d" % i, "title": "t%d" % i, "column": "col-1", "modified": "2026-09-29",
                 "metadata": {"pad": "m" * (60000 if i == 7 else 6000)}} for i in range(23)]
        if fields == "id":
            rows = [{"id": r["id"]} for r in rows]
        text = json.dumps({"success": True, "list_items": rows[(page - 1) * per_page:page * per_page]}, indent=2)
        if len(text) > 48000:
            text = text[:48000] + "\\n...[truncated to fit; use fields/section/query/pagination for the rest]"
        return text

    print("fake-board stderr line", file=sys.stderr, flush=True)
    srv.run(transport=sys.argv[1] if len(sys.argv) > 1 else "stdio")
''')

SECRET = "sentinel-secret-7f3a91"


def tools(**over):
    t = {
        "columns": {"tool": "boards", "args": {"action": "lists", "board_id": "{board_id}"}, "result": "lists",
                    "title_key": "name", "id_key": "key"},
        "cards": {"tool": "boards", "args": {"action": "item_list", "board_id": "{board_id}"}, "result": "data.items"},
        "card": {"tool": "boards", "args": {"action": "item_get", "board_id": "{board_id}", "item_id": "{item_id}"},
                 "result": "item"},
        "create": {"tool": "boards", "args": {"action": "item_create", "board_id": "{board_id}",
                                              "column_id": "{column_id}", "fields": "{fields}"}, "result": "item"},
        "update": {"tool": "boards", "args": {"action": "item_update", "board_id": "{board_id}",
                                              "item_id": "{item_id}", "fields": "{fields}"}, "result": "item"},
        "delete": {"tool": "boards", "args": {"action": "item_delete", "board_id": "{board_id}", "item_id": "{item_id}"}},
        "ensure_column": {"tool": "boards", "args": {"action": "list_create", "board_id": "{board_id}",
                                                     "title": "{column}", "description": "{description}"},
                          "result": "list", "id_key": "key"},
    }
    for k in ("cards", "card", "create", "update"):
        t[k]["fields"] = {"column": "list_id", "modified": "updated_at"}
    t.update(over)
    return t


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    trackers.reset()
    yield tmp_path
    M.close_all()
    for k, v in saved.items():
        setattr(C, k, v)
    trackers.reset()


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "fake_board_server.py"
    p.write_text(SERVER)
    return p


def stdio_cfg(script, **over):
    cfg = {"type": "mcp", "board_id": "b-1", "server": {"command": sys.executable, "args": [str(script)]},
           "tools": tools()}
    cfg.update(over)
    return cfg


def tracker(cfg):
    C.TRACKER = cfg
    trackers.reset("tracker")
    return trackers.get("tracker")


def test_registry_builds_the_mcp_connector(script):
    assert isinstance(tracker(stdio_cfg(script)), M.Mcp)


def test_create_cards_update_column_delete_round_trip(script):
    t = tracker(stdio_cfg(script))
    assert t.columns() == {"Inbox": "col-1", "Spec ready": "col-2"}
    made = t.create("Inbox", title="Add login", description="why", tags=["frontend"], metadata={"pipeline_mode": "auto"})
    assert made["id"] == "it-1" and made["list_id"] == "col-1"
    got = t.cards()
    assert [c["id"] for c in got] == ["it-1"]
    c = got[0]
    assert (c["title"], c["description"], c["tags"], c["metadata"]) == ("Add login", "why", ["frontend"],
                                                                         {"pipeline_mode": "auto"})
    assert c["updated_at"] == "2026-09-28T10:00:00+00:00" and c["assigned_to"] is None
    moved = t.update("it-1", column="Spec ready", metadata={"worker": "w1"})
    assert moved["list_id"] == "col-2"
    assert t.card("it-1")["metadata"] == {"pipeline_mode": "auto", "worker": "w1"}  # client-side merge
    t.delete("it-1")
    assert t.cards() == []


def test_fields_expansion_and_dotted_result_paths(script):
    t = tracker(stdio_cfg(script))
    t.create("Inbox", title="T", metadata={"a": 1})
    # verify=False writes exactly the given fields: the metadata replaces, no merge.
    t.update("it-1", verify=False, metadata={"b": 2}, title="T2")
    c = t.card("it-1")
    assert c["metadata"] == {"b": 2} and c["title"] == "T2"


def test_update_verify_asserts_the_field_persisted(script):
    t = tracker(stdio_cfg(script, tools=tools(update={
        "tool": "boards", "args": {"action": "item_update_lossy", "board_id": "{board_id}", "item_id": "{item_id}",
                                   "fields": "{fields}"}, "result": "item"})))
    t.create("Inbox", title="T")
    with pytest.raises(SystemExit, match="field title did not persist"):
        t.update("it-1", title="changed")


def test_unknown_column_is_refused(script):
    t = tracker(stdio_cfg(script))
    with pytest.raises(SystemExit, match="no column 'Nope'"):
        t.create("Nope", title="T")


def test_ensure_column_creates_once(script):
    t = tracker(stdio_cfg(script))
    assert t.ensure_column("Inbox") == "col-1"
    assert t.ensure_column("Done", description="finished") == "col-3"
    assert t.columns()["Done"] == "col-3"


def test_is_error_raises_with_the_tool_name(script):
    t = tracker(stdio_cfg(script))
    with pytest.raises(SystemExit) as e:
        t.card("missing-id")
    assert "boards" in str(e.value) and "no item missing-id" in str(e.value)


def test_unparseable_json_raises_with_the_tool_name(script):
    t = tracker(stdio_cfg(script, tools=tools(cards={"tool": "boards", "args": {"action": "garbage",
                                                                                "board_id": "{board_id}"}})))
    with pytest.raises(SystemExit) as e:
        t.cards()
    assert "boards" in str(e.value) and "not json at all" in str(e.value)


def test_a_reply_the_server_truncated_says_the_card_is_too_large(script):
    t = tracker(stdio_cfg(script, tools=tools(card={"tool": "boards", "args": {"action": "cut", "board_id": "{board_id}"},
                                                    "result": "item"})))
    with pytest.raises(SystemExit) as e:
        t.card("it-9")
    assert str(e.value) == ("pl: the board server cut this card off at 48,000 characters; it is too large to read "
                            "\u2014 split it into smaller cards")


def _capped(page_size_arg=True):
    args = {"per_page": 10} if page_size_arg else {}
    return tools(cards={"tool": "capped", "args": args, "result": "list_items", "page": "page", "page_size": 10,
                        "fields": {"column": "list_id", "modified": "updated_at"}})


def test_a_cut_list_page_is_reread_one_card_at_a_time_and_a_too_large_card_is_skipped(script, monkeypatch):
    from pl import events
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, card=None, **d: seen.append((kind, card, d.get("message", ""))))
    monkeypatch.setattr(M, "_skipped", {}, raising=False)
    t = tracker(stdio_cfg(script, tools=_capped()))
    got = t.cards()
    assert [c["id"] for c in got] == ["c-%d" % i for i in range(23) if i != 7]
    assert got[0]["list_id"] == "col-1" and got[0]["updated_at"] == "2026-09-29" and got[0]["tags"] == []
    assert [(k, c) for k, c, _ in seen] == [("error", "c-7")]
    assert "c-7" in seen[0][2] and "this card" not in seen[0][2] and len(seen[0][2]) <= 200
    tracker(stdio_cfg(script, tools=_capped())).cards()   # the next pass within the hour: no second event
    assert len(seen) == 1


def test_a_cut_list_that_cannot_be_paged_smaller_names_the_list_call(script):
    t = tracker(stdio_cfg(script, tools=_capped(page_size_arg=False)))
    with pytest.raises(SystemExit) as e:
        t.cards()
    msg = str(e.value)
    assert "this card" not in msg and "card list" in msg and "capped" in msg and "page=1" in msg


def test_missing_env_var_raises_set_var(script, monkeypatch):
    monkeypatch.delenv("PL_TEST_TOKEN", raising=False)
    cfg = stdio_cfg(script)
    cfg["server"]["env"] = {"FAKE_TOKEN": "${PL_TEST_TOKEN}"}
    with pytest.raises(SystemExit, match="set PL_TEST_TOKEN"):
        tracker(cfg).columns()


def test_env_var_reaches_the_stdio_server_and_one_session_is_reused(script, monkeypatch):
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    monkeypatch.setenv("EXPECT", SECRET)
    cfg = stdio_cfg(script, tools=tools(cards={"tool": "whoami", "args": {}}))
    cfg["server"]["env"] = {"FAKE_TOKEN": "${PL_TEST_TOKEN}", "EXPECT": "${EXPECT}"}
    first = tracker(cfg).cards()[0]
    assert first["env_ok"] is True
    assert tracker(cfg).cards()[0]["id"] == first["id"]  # new tracker object (dispatch resets each pass), same server


def test_test_reports_a_mapped_tool_the_server_lacks(script):
    t = tracker(stdio_cfg(script, tools=tools(delete={"tool": "remove_item", "args": {}})))
    out = t.test()
    assert "remove_item" in out and "missing" in out
    assert "ok" in tracker(stdio_cfg(script)).test()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_header_secret_never_leaks(script, monkeypatch, caplog):
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "EXPECT": SECRET}
    proc = subprocess.Popen([sys.executable, str(script), "streamable-http"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
        cfg = {"type": "mcp", "board_id": "b-1",
               "tools": tools(cards={"tool": "whoami", "args": {}}, card={"tool": "leaky", "args": {}}),
               "server": {"url": f"http://127.0.0.1:{port}/mcp", "headers": {"Authorization": "Bearer ${PL_TEST_TOKEN}"}}}
        t = tracker(cfg)
        assert t.cards()[0]["header_ok"] is True  # the resolved header really was sent
        with pytest.raises(SystemExit) as e:
            t.card("any")
        assert "leaky" in str(e.value) and "bad auth: Bearer ***" in str(e.value)
        assert SECRET not in str(e.value)
        assert SECRET not in caplog.text
    finally:
        M.close_all()
        proc.terminate()
        proc.wait(timeout=10)


def test_connection_failure_does_not_leak_the_header(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    cfg = {"type": "mcp", "board_id": "b-1", "tools": tools(), "timeout": 5,
           "server": {"url": f"http://127.0.0.1:{_free_port()}/mcp",
                      "headers": {"Authorization": "Bearer ${PL_TEST_TOKEN}"}}}
    with pytest.raises(SystemExit) as e:
        tracker(cfg).columns()
    assert SECRET not in str(e.value)
    assert SECRET not in caplog.text


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("fail", [True, False], ids=["isError", "non-json"])
def test_a_secret_straddling_the_error_cut_is_never_leaked(script, monkeypatch, fail):
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    cfg = stdio_cfg(script)
    cfg["server"]["env"] = {"FAKE_TOKEN": "${PL_TEST_TOKEN}"}
    for pad in range(160, 200):
        t = tracker({**cfg, "tools": tools(cards={"tool": "echo_env", "args": {"pad": pad, "fail": fail}})})
        with pytest.raises(SystemExit) as e:
            t.cards()
        msg = str(e.value)
        assert "echo_env" in msg
        assert not any(SECRET[:n] in msg for n in range(4, len(SECRET) + 1)), (pad, msg[-40:])


def test_a_stuck_call_times_out_and_the_server_process_is_gone(script):
    t = tracker(stdio_cfg(script, timeout=3, tools=tools(cards={"tool": "whoami", "args": {}},
                                                         card={"tool": "slow", "args": {"seconds": 60}})))
    pid = int(t.cards()[0]["id"])
    with pytest.raises(SystemExit, match="timed out"):
        t.card("any")
    deadline = time.time() + 10
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


def test_close_lets_ctrl_c_through(script, monkeypatch):
    tracker(stdio_cfg(script)).columns()
    (s,) = M._sessions.values()
    real = s.main.result
    calls = []

    def interrupted(timeout=None):  # Ctrl-C lands in the first wait only; later waits behave normally
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(timeout)

    monkeypatch.setattr(s.main, "result", interrupted)
    with pytest.raises(KeyboardInterrupt):
        s.close()
    assert len(calls) == 1
    monkeypatch.setattr(s.main, "result", real)


def test_close_during_a_stuck_call_stops_the_server(script, monkeypatch):
    monkeypatch.setattr(M, "CLOSE_WAIT", 1, raising=False)
    t = tracker(stdio_cfg(script, timeout=30, tools=tools(cards={"tool": "whoami", "args": {}},
                                                          card={"tool": "slow", "args": {"seconds": 60}})))
    pid = int(t.cards()[0]["id"])
    threading.Thread(target=lambda: pytest.raises(SystemExit, t.card, "any"), daemon=True).start()
    time.sleep(0.5)
    M.close_all()  # what atexit runs, also after Ctrl-C
    deadline = time.time() + 8
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    alive = _alive(pid)
    if alive:
        os.kill(pid, signal.SIGKILL)
    assert not alive


def test_a_caller_queued_behind_a_stuck_call_fails_in_time_and_spares_the_new_session(script):
    t = tracker(stdio_cfg(script, timeout=2, tools=tools(cards={"tool": "whoami", "args": {}},
                                                         card={"tool": "slow", "args": {"seconds": 60}})))
    t.cards()
    (old,) = M._sessions.values()
    t0 = time.time()
    log, pids, done = {}, [], threading.Event()

    def stuck():
        with pytest.raises(SystemExit):
            t.card("a")

    def queued():
        time.sleep(0.3)
        try:
            t.cards()
        except SystemExit as e:
            log["b"] = str(e)
        log["b_took"] = time.time() - t0
        done.set()

    def fresh():
        time.sleep(4)
        while not done.is_set() or time.time() - t0 < 5:
            try:
                pids.append(tracker(t.cfg).cards()[0]["id"])
            except SystemExit as e:
                pids.append(str(e))
            time.sleep(0.3)

    ths = [threading.Thread(target=f) for f in (stuck, queued, fresh)]
    [h.start() for h in ths]
    [h.join(40) for h in ths]
    assert log["b_took"] < 2 + 3, log  # the caller's 2s timeout plus the dead session's shutdown, not 2s + 15s
    assert len(set(pids)) == 1 and pids[0].isdigit(), pids  # the queued failure did not close the new session
    key, (cur,) = next(iter(M._sessions)), M._sessions.values()
    M._drop(key, old)  # a stale caller dropping its dead session leaves the live one alone
    assert M._sessions.get(key) is cur and t.cards()[0]["id"] == pids[0]


def test_time_spent_queued_counts_against_the_callers_timeout(script):
    t = tracker(stdio_cfg(script, timeout=2, tools=tools(card={"tool": "slow", "args": {"seconds": 1.5}})))
    t.columns()
    first = threading.Thread(target=pytest.raises, args=(SystemExit, t.card, "a"))  # 1.5s: inside its own timeout
    first.start()
    time.sleep(0.3)
    with pytest.raises(SystemExit, match="timed out"):  # queued 1.2s + 1.5s would pass 2s
        t.card("b")
    first.join()


@pytest.mark.parametrize("secret", [SECRET, 'sentinel-\u00e9"q\\-7f3a'], ids=["plain", "json-escaped"])
def test_ensure_column_failure_masks_the_secret(script, monkeypatch, secret):
    monkeypatch.setenv("PL_TEST_TOKEN", secret)
    cfg = stdio_cfg(script, tools=tools(ensure_column={"tool": "leak_reply", "args": {"title": "{column}"}}))
    cfg["server"]["env"] = {"FAKE_TOKEN": "${PL_TEST_TOKEN}"}
    with pytest.raises(SystemExit) as e:
        tracker(cfg).ensure_column("Brand new")
    msg = str(e.value)
    assert "create column 'Brand new' failed" in msg and "denied for ***" in msg
    assert "sentinel" not in msg


def test_stdio_server_stderr_log_masks_a_secret_the_server_prints(script, monkeypatch):
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    cfg = stdio_cfg(script, tools=tools(cards={"tool": "stderr_leak", "args": {}}))
    cfg["server"]["env"] = {"FAKE_TOKEN": "${PL_TEST_TOKEN}"}
    tracker(cfg).cards()
    M.close_all()
    (log,) = C.STATE_DIR.glob("mcp-*.log")
    text = log.read_text()
    assert "server saw token ***" in text and "fake-board stderr line" in text
    assert SECRET not in text


def test_columns_row_missing_a_key_names_the_key_and_tool(script):
    t = tracker(stdio_cfg(script, tools=tools(columns={"tool": "bad_rows", "args": {}, "result": "lists",
                                                       "title_key": "name", "id_key": "key"})))
    with pytest.raises(SystemExit) as e:
        t.columns()
    assert "bad_rows" in str(e.value) and "'key'" in str(e.value)


def test_cards_query_passes_only_mapped_keys_under_the_server_name(script):
    t = tracker(stdio_cfg(script, tools=tools(cards={"tool": "seen", "args": {},
                                                     "query": {"list_id": "column_id"}})))
    got = t.cards({"list_id": "col-2", "other": "must-not-arrive"})[0]
    assert got["column_id"] == "col-2" and got["other"] == ""


def test_move_without_verify_writes_fields_first_so_a_failed_write_leaves_the_card_unmoved(script):
    t = tracker(stdio_cfg(script, tools=tools(
        move={"tool": "boards", "args": {"action": "item_update", "board_id": "{board_id}", "item_id": "{item_id}",
                                         "fields": {"column": "{column_id}"}}, "result": "item"},
        update={"tool": "boards", "args": {"action": "explode", "board_id": "{board_id}"}})))
    t.create("Inbox", title="T")
    with pytest.raises(SystemExit, match="explode"):
        t.update("it-1", verify=False, column="Spec ready", title="T2")
    assert t.card("it-1")["list_id"] == "col-1"


def test_card_text_with_placeholders_and_vars_reaches_the_server_literally(script, monkeypatch):
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    raw = "{board_id} {fields} ${PL_TEST_TOKEN}"
    t = tracker(stdio_cfg(script))
    t.create("Inbox", title=raw, description=raw, metadata={"k": raw})
    c = t.card("it-1")
    assert (c["title"], c["description"], c["metadata"]) == (raw, raw, {"k": raw})
    t.update("it-1", title=raw + "!", metadata={"j": raw})
    c = t.card("it-1")
    assert c["title"] == raw + "!" and c["metadata"] == {"k": raw, "j": raw}


def test_debug_logs_of_the_sdk_and_httpx_never_show_a_resolved_secret(script, monkeypatch, caplog):
    port = _free_port()
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, str(script), "streamable-http"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    names = ("mcp", "httpx", "httpcore")
    levels = {n: logging.getLogger(n).level for n in names}
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        for n in names:
            logging.getLogger(n).setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
        cfg = {"type": "mcp", "board_id": "b-1", "tools": tools(),
               "server": {"url": f"http://127.0.0.1:{port}/mcp?token=${{PL_TEST_TOKEN}}"}}
        assert tracker(cfg).columns() == {"Inbox": "col-1", "Spec ready": "col-2"}
        assert "HTTP Request" in caplog.text  # httpx really logged the URL
        assert SECRET not in caplog.text
    finally:
        for n, lv in levels.items():
            logging.getLogger(n).setLevel(lv)
        M.close_all()
        proc.terminate()
        proc.wait(timeout=10)


def test_stdio_server_stderr_goes_to_a_private_log_file(script, capfd):
    tracker(stdio_cfg(script)).columns()
    M.close_all()
    _, err = capfd.readouterr()
    assert "fake-board stderr line" not in err
    logs = list(C.STATE_DIR.glob("mcp-*.log"))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert "fake-board stderr line" in logs[0].read_text()


REFUSE = {"tool": "boards", "args": {"action": "refuse_write", "board_id": "{board_id}", "item_id": "{item_id}"}}


def _bridge(script):
    """Pipeline board and intake board on one fake server; the intake refuses every write."""
    C.TRACKER = stdio_cfg(script)
    C.INTAKE = stdio_cfg(script, tools=tools(update=REFUSE), columns=["Triage"], skip_tags=[])
    trackers.reset()
    pipe = trackers.get("tracker")
    for col in ("Needs Human Review", "Done"):
        pipe.ensure_column(col)
    trackers.reset()
    C.USER_NAME, C.USER_EMAIL, C.USER_UUID = "Me", "me@x", None
    return trackers.get("tracker"), trackers.get("intake")


def test_a_rejected_product_write_carries_on(script, capsys):
    from argparse import Namespace

    from pl import commands, product
    pipe, intake = _bridge(script)
    pc = intake.create("Inbox", title="ask", metadata={"k": 1})
    q = pipe.create("Inbox", title="work", metadata={"product_card": pc["id"], "pr_urls": ["u"]})
    product.mirror_to_product(pipe.card(q["id"]), "PR open", False)   # the intake refuses the move
    assert pipe.card(q["id"])["metadata"]["product_synced_col"] == "PR open"
    commands.cmd_done(Namespace(id=q["id"], note=None))
    assert pipe.card(q["id"])["list_id"] == pipe.columns()["Done"]
    assert "done " + q["id"][:8] in capsys.readouterr().out


def test_product_cards_mine_filters_client_side(script):
    from pl import product
    _, intake = _bridge(script)
    mine = intake.create("Inbox", title="mine", assigned_to="me@x")
    intake.create("Inbox", title="theirs", assigned_to="someone@else")
    intake.create("Inbox", title="nobody's")                       # unassigned: never mine, even with no uuid set
    assert [c["id"] for c in product.product_cards_mine()] == [mine["id"]]


def test_flat_write_arguments_and_paged_cards(script):
    t = tracker(stdio_cfg(script, tools=tools(
        cards={"tool": "flat", "args": {"action": "item_list"}, "result": "rows", "page": "page", "page_size": 2},
        update={"tool": "flat", "args": {"action": "item_update", "item_id": "{item_id}", "*": "{fields}"}, "result": "item"})))
    assert [c["id"] for c in t.cards()] == ["f-0", "f-1", "f-2", "f-3", "f-4"]
    got = t.update("f-1", verify=False, title="T", metadata={"a": 1})
    assert (got["title"], got["metadata"]) == ("T", {"a": 1})


def test_get_unknown_type_names_the_valid_types(monkeypatch):
    monkeypatch.setattr(C, "TRACKER", {"type": "nope"})
    trackers.reset()
    with pytest.raises(SystemExit, match=r"unknown tracker type 'nope' \(valid: github-issues, github-project, mcp\)"):
        trackers.get("tracker")


def test_reset_drops_the_cached_tracker(monkeypatch):
    monkeypatch.setattr(C, "TRACKER", {"type": "mcp", "server": {"command": "x"}, "board_id": "b", "tools": {}})
    trackers.reset()
    first = trackers.get("tracker")
    assert trackers.get("tracker") is first
    trackers.reset("tracker")
    assert trackers.get("tracker") is not first
    trackers.reset()


def test_a_field_named_like_a_mapped_argument_cannot_override_it(monkeypatch):
    from types import SimpleNamespace as NS
    calls = []

    class Sess:
        def call_tool(self, tool, args):
            calls.append(dict(args))
            return NS(isError=False, content=[NS(type="text", text="{}")])

    class FakeS:
        def run(self, fn):
            return fn(Sess())

        def scrub(self, t, n=None):
            return t
    monkeypatch.setattr(M, "_session", lambda server, timeout, as_is=False: ("k", FakeS()))
    tools = {"update": {"tool": "boards", "args": {"action": "item_update", "board_id": "{board_id}",
                                                    "item_id": "{item_id}", "*": "{fields}"}}}
    t = M.Mcp({"server": {"command": "x"}, "board_id": "B", "tools": tools})
    t.update("REAL", verify=False, action="item_delete", board_id="X", note="kept")
    assert calls[-1]["action"] == "item_update" and calls[-1]["board_id"] == "B" and calls[-1]["note"] == "kept"


@pytest.mark.parametrize("where", ["relative", "tilde"])
def test_mcp_config_file_server_is_used_as_is(script, tmp_path, monkeypatch, where):
    """[tracker] mcp_config + server name: the harness's own server entry, env passed through unchanged."""
    (tmp_path / "prof").mkdir()
    (tmp_path / "prof" / "mcp.json").write_text(__import__("json").dumps({"mcpServers": {
        "other": {"command": "nope"},
        "brd": {"type": "stdio", "command": sys.executable, "args": [str(script)],
                "env": {"FAKE_TOKEN": SECRET, "EXPECT": SECRET}}}}))
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "prof")
    path = "mcp.json" if where == "relative" else "~/prof/mcp.json"
    cfg = {"type": "mcp", "board_id": "b-1", "mcp_config": path, "server": "brd",
           "tools": tools(cards={"tool": "whoami", "args": {}})}
    assert tracker(cfg).cards()[0]["env_ok"] is True
    t = tracker({**cfg, "tools": tools(cards={"tool": "echo_env", "args": {"fail": True}})})
    with pytest.raises(SystemExit) as e:
        t.cards()
    assert "echo_env" in str(e.value) and SECRET not in str(e.value)


@pytest.mark.parametrize("content", [None, "{not json " + SECRET, '{"mcpServers": {"other": {"env": {"K": "%s"}}}}' % SECRET],
                         ids=["missing-file", "bad-json", "missing-server"])
def test_mcp_config_problem_names_path_and_server_never_values(tmp_path, monkeypatch, content):
    f = tmp_path / "mcp.json"
    if content is not None:
        f.write_text(content)
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        tracker({"type": "mcp", "board_id": "b-1", "mcp_config": "mcp.json", "server": "brd", "tools": tools()})
    msg = str(e.value)
    assert str(f) in msg and "'brd'" in msg and SECRET not in msg


def _as_is(tmp_path, monkeypatch, server):
    (tmp_path / "prof").mkdir(exist_ok=True)
    (tmp_path / "prof" / "mcp.json").write_text(__import__("json").dumps({"mcpServers": {"brd": server}}))
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "prof")
    return {"type": "mcp", "board_id": "b-1", "mcp_config": "mcp.json", "server": "brd", "timeout": 8,
            "tools": tools(cards={"tool": "whoami", "args": {}})}


def _leaks(msg):
    from pl import events
    events.emit("error", message=msg)   # what the dispatcher does with the text
    logs = "".join(p.read_text() for p in C.STATE_DIR.glob("mcp-*.log"))
    return [w for w, t in (("error", msg), ("events", (C.STATE_DIR / "events.jsonl").read_text()), ("log", logs))
            if SECRET in t]


@pytest.mark.parametrize("url", [f"https://mcp.example.invalid/mcp?token={SECRET}",
                                 f"https://u:{SECRET}@mcp.example.invalid/mcp"], ids=["query", "userinfo"])
def test_as_is_url_secret_never_reaches_error_events_or_logs(tmp_path, monkeypatch, caplog, url):
    import httpx
    import mcp.shared._httpx_utils as HU

    def client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, request=r)),
                                 headers=headers or {})
    monkeypatch.setattr(HU, "create_mcp_http_client", client)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SystemExit) as e:
        tracker(_as_is(tmp_path, monkeypatch, {"type": "http", "url": url})).cards()
    assert "mcp.example.invalid" in str(e.value)
    assert _leaks(str(e.value)) == [] and SECRET not in caplog.text


def test_as_is_args_secret_never_reaches_error_events_or_the_stderr_log(tmp_path, monkeypatch):
    code = "import sys; sys.stderr.write('bad key: ' + ' '.join(sys.argv) + chr(10)); sys.exit(1)"
    cfg = _as_is(tmp_path, monkeypatch, {"command": sys.executable, "args": ["-c", code, "--api-key", SECRET]})
    with pytest.raises(SystemExit) as e:
        tracker(cfg).cards()
    M.close_all()
    assert "bad key:" in "".join(p.read_text() for p in C.STATE_DIR.glob("mcp-*.log"))
    assert _leaks(str(e.value)) == []


def test_as_is_expands_vars_and_defaults_and_masks_the_values(script, tmp_path, monkeypatch):
    monkeypatch.setenv("PL_TEST_TOKEN", SECRET)
    monkeypatch.delenv("PL_UNSET_VAR", raising=False)
    cfg = _as_is(tmp_path, monkeypatch, {"command": sys.executable, "args": ["${PL_SCRIPT}"],
                                         "env": {"FAKE_TOKEN": "${PL_TEST_TOKEN}",
                                                 "EXPECT": "${PL_UNSET_VAR:-%s}" % SECRET}})
    monkeypatch.setenv("PL_SCRIPT", str(script))
    assert tracker(cfg).cards()[0]["env_ok"] is True
    t = tracker({**cfg, "tools": tools(cards={"tool": "echo_env", "args": {"fail": True}})})
    with pytest.raises(SystemExit) as e:
        t.cards()
    assert "echo_env" in str(e.value)
    tracker({**cfg, "tools": tools(cards={"tool": "stderr_leak", "args": {}})}).cards()
    M.close_all()
    assert "server saw token ***" in "".join(p.read_text() for p in C.STATE_DIR.glob("mcp-*.log"))
    assert _leaks(str(e.value)) == []
    code = "import os, sys; sys.stderr.write('part ' + os.environ['TOK'][2:] + chr(10)); sys.exit(1)"
    cfg = _as_is(tmp_path, monkeypatch, {"command": sys.executable, "args": ["-c", code], "env": {"TOK": "k=${PL_TEST_TOKEN}"}})
    with pytest.raises(SystemExit):
        tracker(cfg).cards()
    M.close_all()
    assert "part ***" in "".join(p.read_text() for p in C.STATE_DIR.glob("mcp-*.log"))   # the expanded value alone
    assert _leaks("") == []


def test_as_is_missing_var_without_default_names_only_the_var(tmp_path, monkeypatch):
    monkeypatch.delenv("PL_MISSING_VAR", raising=False)
    cfg = _as_is(tmp_path, monkeypatch, {"command": "never-run", "args": ["--k", SECRET],
                                         "env": {"A": SECRET, "B": "${PL_MISSING_VAR}"}})
    with pytest.raises(SystemExit) as e:
        tracker(cfg).cards()
    assert "PL_MISSING_VAR" in str(e.value) and SECRET not in str(e.value)
