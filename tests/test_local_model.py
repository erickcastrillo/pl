"""The optional local model (Ollama on this machine): off by default, loopback only, None on any failure."""
import json
import socket
import urllib.error

import pytest
import tomlkit

from pl import cli, local_model
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def calls(monkeypatch):
    """Every request the module makes; the test sets calls.reply (text, or an exception to raise)."""
    class Calls(list):
        reply = json.dumps({"message": {"role": "assistant", "content": "All good."}})

    got = Calls()

    def fake(url, payload, timeout):
        got.append((url, payload, timeout))
        if isinstance(got.reply, BaseException):
            raise got.reply
        return got.reply

    monkeypatch.setattr(local_model, "_request", fake)
    return got


def _on(**kw):
    C.LOCAL_MODEL = {"enabled": True, **kw}


def test_off_by_default_and_never_calls(calls):
    assert C.LOCAL_MODEL == {}
    assert local_model.settings() == {"enabled": False, "url": "http://localhost:11434", "model": "gemma4", "timeout": 30}
    assert local_model.ask("sys", "text") is None
    assert local_model.chat("sys", "text")[1].startswith("off")
    assert calls == []


def test_config_reads_the_local_model_table(fake_home):
    (fake_home / ".pl-t" / "config.toml").write_text('[local_model]\nenabled = true\nmodel = "gemma4:e4b"\ntimeout = 5\n')
    C.load("t")
    s = local_model.settings()
    assert s["enabled"] is True and s["model"] == "gemma4:e4b" and s["timeout"] == 5 and s["url"] == "http://localhost:11434"


def test_ask_posts_a_chat_without_streaming(calls):
    _on(model="gemma4:e4b", timeout=7)
    assert local_model.ask("be brief", "the text") == "All good."
    url, payload, timeout = calls[0]
    assert url == "http://localhost:11434/api/chat" and timeout == 7
    assert payload == {"model": "gemma4:e4b", "stream": False,
                       "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "the text"}]}


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://[::1]:11434", "http://localhost:9999/"])
def test_loopback_hosts_are_allowed(calls, url):
    _on(url=url)
    assert local_model.ask("s", "t") == "All good."
    assert calls[0][0] == url.rstrip("/") + "/api/chat"


@pytest.mark.parametrize("url", ["http://example.com:11434", "http://10.0.0.5:11434", "https://ollama.example.org",
                                 "http://localhost.example.com", "http://user@evil.test", "file:///etc/passwd",
                                 "localhost:11434", "", 5])
def test_non_loopback_url_is_refused_and_never_called(calls, url):
    _on(url=url)
    assert local_model.ask("s", "t") is None
    answer, why = local_model.chat("s", "t")
    assert answer is None and "loopback" in why
    assert local_model.available()[0] is False
    assert calls == []


@pytest.mark.parametrize("err", [TimeoutError("timed out"), socket.timeout("timed out"),
                                 urllib.error.URLError("refused"), OSError("down"), ValueError("weird")])
def test_failures_give_none(calls, err):
    _on()
    calls.reply = err
    assert local_model.ask("s", "t") is None
    assert local_model.chat("s", "t")[1]


@pytest.mark.parametrize("body", ["not json", "[]", '{"message": {}}', '{"message": {"content": 3}}',
                                  '{"message": {"content": "   "}}', '{"error": "model not found"}'])
def test_bad_or_empty_answers_give_none(calls, body):
    _on()
    calls.reply = body
    assert local_model.ask("s", "t") is None


def test_enabled_must_be_true_itself(calls):
    C.LOCAL_MODEL = {"enabled": "yes"}
    assert local_model.ask("s", "t") is None and calls == []


def test_control_characters_are_stripped(calls):
    _on()
    calls.reply = json.dumps({"message": {"content": "Hi\x1b[31m red\x07 bell‮ flip\r\nnext\tline\x00"}})
    assert local_model.ask("s", "t") == "Hi[31m red bell flip\nnext\tline"


def test_answer_is_capped(calls):
    _on()
    calls.reply = json.dumps({"message": {"content": "x" * 5000}})
    got = local_model.ask("s", "t", max_chars=100)
    assert len(got) == 100 and got.endswith("…")


def test_available_checks_the_model_is_pulled(calls):
    calls.reply = json.dumps({"models": [{"name": "llama3:latest"}, {"name": "gemma4:latest"}]})
    ok, line = local_model.available()
    assert ok and "gemma4" in line
    assert calls[0][:2] == ("http://localhost:11434/api/tags", None)
    C.LOCAL_MODEL = {"model": "gemma4:e4b"}
    ok, line = local_model.available()
    assert not ok and "ollama pull gemma4:e4b" in line


def test_available_says_when_ollama_does_not_answer(calls):
    calls.reply = urllib.error.URLError("Connection refused")
    ok, line = local_model.available()
    assert not ok and "Ollama" in line and "http://localhost:11434" in line


def test_validate_checks_the_table():
    good = '[local_model]\nenabled = true\nurl = "http://127.0.0.1:11434"\nmodel = "gemma4"\ntimeout = 30\n'
    assert C.validate(tomlkit.parse(good)) == []
    for bad, key in (('enabled = "yes"', "enabled"), ('url = "http://example.com"', "loopback"),
                     ("model = 3", "model"), ("timeout = 0", "timeout"), ("timeout = true", "timeout"),
                     ('api_key = "x"', "api_key")):
        errs = C.validate(tomlkit.parse(f"[local_model]\n{bad}\n"))
        assert any(key in e for e in errs), (bad, errs)


def test_cli_check_prints_the_settings_and_the_line(calls, monkeypatch, capsys):
    calls.reply = json.dumps({"models": [{"name": "gemma4:latest"}]})
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "local-model"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "enabled: no" in out and "url: http://localhost:11434" in out and "model: gemma4" in out and "gemma4 is pulled" in out


def test_cli_check_fails_when_not_available(calls, fake_home, monkeypatch, capsys):
    calls.reply = OSError("down")
    (fake_home / ".pl-t" / "config.toml").write_text("[local_model]\nenabled = true\n")
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "local-model", "check"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1 and "enabled: yes" in capsys.readouterr().out
