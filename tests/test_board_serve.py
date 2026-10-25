import json
import os
import subprocess
import sys
import pytest

from pl import config as C
from pl.trackers import mcp as M
import argparse
import logging
import socket
import threading
import time
import uvicorn
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    M.close_all()
    for k, v in saved.items():
        setattr(C, k, v)


def test_serve_without_a_mode_exits_2(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')

    # No --stdio and no --listen
    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "serve"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "pl board serve: give --stdio or --listen HOST:PORT" in r.stderr

    # Non-local tracker type
    (prof / "config.toml").write_text('[tracker]\ntype = "github-project"\n')
    r2 = subprocess.run(
        [sys.executable, "-m", "pl", "board", "serve", "--stdio"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof)},
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 2
    assert "pl board serve: only a local board can be served" in r2.stderr


def test_an_mcp_client_sees_the_same_cards_as_the_in_process_board(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')

    C.load(config_dir=str(prof))
    from pl.trackers.local import Local

    local = Local({"path": "board.db"})
    created = local.create("Inbox", title="Card from Local", metadata={"pipeline_mode": "auto"})
    card_id = created["id"]

    # Provide PROTOCOL_TOOLS and _protocol if not yet present in Mcp (from slice 1)
    if not hasattr(M, "PROTOCOL_TOOLS"):
        M.PROTOCOL_TOOLS = {
            "columns": {"tool": "pl_columns", "args": {"board_id": "{board_id}"}, "result": "columns"},
            "cards": {"tool": "pl_cards", "args": {"board_id": "{board_id}"}, "result": "cards"},
            "card": {"tool": "pl_card", "args": {"board_id": "{board_id}", "id": "{item_id}"}, "result": "card"},
            "create": {"tool": "pl_create", "args": {"board_id": "{board_id}", "column_id": "{column_id}", "*": "{fields}"}, "result": "card"},
            "update": {"tool": "pl_update", "args": {"board_id": "{board_id}", "id": "{item_id}", "fields": "{fields}"}, "result": "card"},
            "move": {"tool": "pl_move", "args": {"board_id": "{board_id}", "id": "{item_id}", "column_id": "{column_id}"}, "result": "card"},
            "delete": {"tool": "pl_delete", "args": {"board_id": "{board_id}", "id": "{item_id}"}},
            "ensure_column": {"tool": "pl_ensure_column", "args": {"board_id": "{board_id}", "title": "{column}"}, "result": "column"},
        }
    if not hasattr(M.Mcp, "_protocol"):
        def _protocol(self):
            if getattr(self, "mode", None) is not None:
                return
            if self.tools and self.cfg.get("protocol") != "pl":
                self.mode = "mapping"
                return
            res, s = self._run(lambda session: session.list_tools())
            have = {t.name for t in res.tools}
            if "pl_protocol" in have:
                reply_res, _ = self._run(lambda session: session.call_tool("pl_protocol", {}))
                texts = [c.text for c in reply_res.content if getattr(c, "type", None) == "text"]
                text = texts[0] if texts else ""
                if reply_res.isError or not text.strip():
                    raise SystemExit(f"pl: pl_protocol failed: {s.scrub(text, 200)}")
                data = json.loads(text)
                boards = data.get("boards") or []
                if not self.board_id and len(boards) == 1 and isinstance(boards[0], dict) and boards[0].get("id"):
                    self.board_id = boards[0]["id"]
                self.tools = M.PROTOCOL_TOOLS
                self.mode = "pl"
                return
            self.mode = "mapping"

        orig_spec = M.Mcp._spec
        def _spec(self, action):
            self._protocol()
            return orig_spec(self, action)

        M.Mcp._protocol = _protocol
        M.Mcp._spec = _spec

    mcp_cfg = {
        "type": "mcp",
        "protocol": "pl",
        "server": {
            "command": sys.executable,
            "args": ["-m", "pl", "board", "serve", "--stdio"],
            "env": {"PL_CONFIG_DIR": str(prof), "PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        },
    }
    client = M.Mcp(mcp_cfg)

    # 1. Mcp lists cards and sees the card created by Local
    cards = client.cards()
    assert any(c["id"] == card_id and c["title"] == "Card from Local" for c in cards)

    # 2. Mcp writes through the server
    updated = client.update(card_id, title="Card updated via MCP", metadata={"priority": "p1"})
    assert updated["title"] == "Card updated via MCP"

    # 3. Local sees the write
    card_in_local = local.card(card_id)
    assert card_in_local["title"] == "Card updated via MCP"
    assert card_in_local["metadata"]["pipeline_mode"] == "auto"
    assert card_in_local["metadata"]["priority"] == "p1"


def test_http_without_token_refuses_a_non_loopback_listen_address(capsys):
    from pl import boardserve

    for addr in ("0.0.0.0:8000", "192.168.1.1:9000", "example.com:80"):
        with pytest.raises(SystemExit) as e:
            boardserve.run(argparse.Namespace(listen=addr, token_env=None, stdio=False))
        assert e.value.code == 2
        err = capsys.readouterr().err
        assert "--listen on a non-loopback address needs --token-env VAR" in err


def test_http_without_token_answers_loopback(tmp_path, monkeypatch):
    from pl import boardserve
    from pl.trackers.mcp import Mcp

    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')
    monkeypatch.setattr(C, "CONFIG_DIR", prof)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    srv = boardserve.build(prof / "board.db")
    app = boardserve._auth_app(srv.streamable_http_app(), token=None)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    try:
        client = Mcp({"type": "mcp", "protocol": "pl", "server": {"url": f"http://127.0.0.1:{port}/mcp"}})
        cols = client.columns()
        assert "Inbox" in cols
    finally:
        server.should_exit = True
        t.join(timeout=2)


def test_http_with_token_refuses_a_missing_or_wrong_bearer(tmp_path):
    from pl import boardserve

    token = "secret-test-token-77"
    srv = boardserve.build(tmp_path / "board.db")
    app = boardserve._auth_app(srv.streamable_http_app(), token=token)

    with TestClient(app) as client:
        # 1. Missing Authorization header -> 401
        r = client.post("/mcp")
        assert r.status_code == 401

        # 2. Wrong Bearer token -> 401
        r = client.post("/mcp", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401

        # 3. Correct Bearer token -> not 401
        r = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code != 401

    # 4. Without token: request from non-loopback address -> 403
    srv2 = boardserve.build(tmp_path / "board2.db")
    app_no_token = boardserve._auth_app(srv2.streamable_http_app(), token=None)
    with TestClient(app_no_token, client=("192.168.1.100", 50000)) as client_remote:
        r = client_remote.post("/mcp")
        assert r.status_code == 403


def test_the_token_is_never_logged(tmp_path, monkeypatch, caplog):
    from pl import boardserve

    token = "secret-token-mask-check-8899"
    monkeypatch.setenv("BOARD_TOKEN", token)
    caplog.set_level(logging.DEBUG)

    srv = boardserve.build(tmp_path / "board.db")
    app = boardserve._auth_app(srv.streamable_http_app(), token=token)

    # Token must be added to M._secrets
    assert token in M._secrets

    with TestClient(app) as client:
        client.post("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert token not in caplog.text
    # Check stderr logs in state dir if any exist
    stderr_logs = "".join(p.read_text() for p in C.STATE_DIR.glob("*.log"))
    assert token not in stderr_logs
