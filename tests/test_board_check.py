import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from pl import config as C

sys.path.insert(0, str(Path(__file__).parent))
from test_tracker_mcp import PROTO_SERVER


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_the_local_board_passes_every_rule(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')

    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check", "local"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stdout: {r.stdout}\nstderr: {r.stderr}"
    for i in range(1, 12):
        assert f"PASS R{i}" in r.stdout


def test_a_server_that_drops_metadata_fails_r4_and_exits_1(tmp_path):
    srv_script = tmp_path / "proto_board_server.py"
    srv_script.write_text(PROTO_SERVER)

    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(json.dumps({
        "mcpServers": {
            "lossy": {
                "command": sys.executable,
                "args": [str(srv_script)],
                "env": {"LOSSY": "1"},
            }
        }
    }))

    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check", "lossy", "--mcp-config", str(mcp_config), "--board", "b-1"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1, f"stdout: {r.stdout}\nstderr: {r.stderr}"
    assert "PASS R1" in r.stdout
    assert "PASS R2" in r.stdout
    assert "PASS R3" in r.stdout
    assert "FAIL R4" in r.stdout


def test_check_json_shape(tmp_path):
    prof = tmp_path / ".pl-test"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')

    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check", "local", "--json"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stdout: {r.stdout}\nstderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert data["server"] == "local"
    assert isinstance(data["rules"], list)
    assert len(data["rules"]) == 11
    ids = [rule["id"] for rule in data["rules"]]
    assert ids == [f"R{i}" for i in range(1, 12)]
    for rule in data["rules"]:
        assert rule["ok"] is True
        assert rule["reason"] is None


def test_unknown_server_name_exits_2(tmp_path):
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {}}))

    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check", "unknown_server", "--mcp-config", str(mcp_config)],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "unknown_server" in r.stderr


def test_missing_server_arg_exits_2():
    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "give local, a URL" in r.stderr


def test_invalid_header_env_exits_2():
    r = subprocess.run(
        [sys.executable, "-m", "pl", "board", "check", "http://127.0.0.1:9999", "--header-env", "InvalidNoEquals"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "--header-env must be HEADER=VAR" in r.stderr


def test_rules_structure():
    from pl import boardcheck
    assert len(boardcheck.RULES) == 11
    ids = [r[0] for r in boardcheck.RULES]
    assert ids == [f"R{i}" for i in range(1, 12)]
    for r in boardcheck.RULES:
        assert len(r) == 3
        assert callable(r[2])



def test_check_passes_against_pl_board_serve_as_a_hub_stand_in(tmp_path, monkeypatch):
    import socket
    import threading
    import time
    import uvicorn
    from pl import boardcheck, boardserve
    from pl.trackers.mcp import Mcp

    prof = tmp_path / ".pl-hub"
    prof.mkdir()
    (prof / "config.toml").write_text('[tracker]\ntype = "local"\n')
    monkeypatch.setattr(C, "CONFIG_DIR", prof)
    monkeypatch.setenv("HUB_TOKEN", "s3cret")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = boardserve._auth_app(boardserve.build(prof / "board.db").streamable_http_app(), token="s3cret")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    try:
        hub = Mcp({"type": "mcp", "protocol": "pl", "server": {
            "url": f"http://127.0.0.1:{port}/mcp", "headers": {"Authorization": "Bearer ${HUB_TOKEN}"}}})
        results = boardcheck.check(hub)
    finally:
        server.should_exit = True
        t.join(timeout=2)
    assert [r["id"] for r in results] == [f"R{i}" for i in range(1, 12)]
    assert [r for r in results if not r["ok"]] == []
