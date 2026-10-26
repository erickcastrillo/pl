"""pl doctor: one list of health checks; this slice has the local-model one."""
import argparse
import json

import pytest

from pl import config as C
from pl import accounts, doctor, local_model


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[models.local]\nhost = "http://localhost:11434"\nroute = "r:1"\nsummarise = "s:1"\n'
                                   '[accounts.acme]\nconfig_dir = "~/.claude-acme"\n')
    C.load("t")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield
    for k, v in saved.items():
        setattr(C, k, v)


def run(capsys, **kw):
    with pytest.raises(SystemExit) as e:
        doctor.cmd_doctor(argparse.Namespace(json=kw.get("json", False)))
    return e.value.code, capsys.readouterr().out


def test_doctor_reports_ollama_down_with_its_fix(capsys):
    code, out = run(capsys)
    assert code == 1
    assert "Ollama: down at http://localhost:11434" in out and "ollama serve" in out


def test_doctor_reports_each_jobs_model_pulled_or_not(capsys, monkeypatch):
    monkeypatch.setattr(local_model, "_request", lambda url, payload, timeout: json.dumps({"models": [{"name": "r:1"}]}))
    code, out = run(capsys)
    assert code == 1
    assert "Ollama: up, 1 models" in out
    assert "route: r:1 is pulled" in out and "summarise: s:1 is not pulled: run ollama pull s:1" in out


def test_doctor_json_lists_each_check(capsys, monkeypatch):
    monkeypatch.setattr(local_model, "_request",
                        lambda url, payload, timeout: json.dumps({"models": [{"name": "r:1"}, {"name": "s:1"}]}))
    code, out = run(capsys, json=True)
    assert code == 0
    assert json.loads(out) == {"checks": [
        {"name": "local_models", "ok": True,
         "lines": ["Ollama: up, 2 models", "route: r:1 is pulled", "summarise: s:1 is pulled"]},
        {"name": "assistant_account", "ok": True, "lines": ["assistant: acme (claude), first usable account"]}]}


def test_doctor_warns_when_the_host_is_on_the_lan(capsys, monkeypatch):
    C.MODELS = {"local": {"host": "http://192.168.1.9:11434", "allow_lan": True, "route": "r:1"}}
    monkeypatch.setattr(local_model, "_request", lambda url, payload, timeout: json.dumps({"models": [{"name": "r:1"}]}))
    _, out = run(capsys)
    assert "192.168.1.9 is on your network, not this machine" in out


def test_doctor_names_the_stage_the_assistant_account_comes_from(capsys):
    C.STAGES = {"plan": {"accounts": ["acme"]}}
    _, out = run(capsys)
    assert "assistant: acme (claude), from stage plan" in out


def test_doctor_fails_when_no_account_can_run_the_assistant(capsys):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "antigravity"}
    code, out = run(capsys)
    assert code == 1
    assert "FAIL assistant_account" in out and "[assistant] account" in out
