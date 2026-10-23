import json
import os
import subprocess
import sys
import pytest


@pytest.fixture(autouse=True)
def machine_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("TG_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")


def _run_chat(*args, env=None):
    cmd = [sys.executable, "-m", "pl", "chat", *args]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return r


def test_add_writes_a_messaging_connector_with_a_var_reference():
    r = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r.returncode == 0, r.stderr
    from pl import chat
    connectors = chat.load()
    assert "telegram" in connectors
    c = connectors["telegram"]
    assert c["role"] == "messaging"
    assert c["adapter"] == "telegram"
    assert c["token"] == "${TG_TOKEN}"
    assert c["allow"] == ["123"]
    assert c["routes"] == ["gates"]


def test_a_literal_token_is_refused_with_exit_2():
    r = _run_chat("add", "telegram", "--token", "123456:ABC-DEF", "--allow", "123", "--yes")
    assert r.returncode == 2
    assert "token" in r.stderr.lower()
    assert ("--token-env" in r.stderr or "quote" in r.stderr)


def test_add_again_says_already_exists_unchanged():
    r1 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r1.returncode == 0
    r2 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    assert r2.returncode == 0
    assert "already exists, unchanged" in r2.stdout

    r3 = _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "456", "--yes")
    assert r3.returncode != 0
    assert "--replace" in r3.stderr or "--replace" in r3.stdout


def test_list_json_never_shows_the_token():
    _run_chat("add", "telegram", "--token", "${TG_TOKEN}", "--allow", "123", "--yes")
    r = _run_chat("list", "--json")
    assert r.returncode == 0, r.stderr
    assert "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" not in r.stdout
    data = json.loads(r.stdout)
    assert "connectors" in data or "telegram" in data
    # verify token in the json is '${TG_TOKEN}'
    val = data["connectors"]["telegram"] if "connectors" in data else data["telegram"]
    assert val["token"] == "${TG_TOKEN}"
