"""pl doctor: one list of health checks; this slice has the local-model one."""
import argparse
import json

import pytest

from pl import config as C
from pl import accounts, doctor, local_model


class FakeBoard:
    def __init__(self, line):
        self.line = line

    def test(self):
        if isinstance(self.line, BaseException):
            raise self.line
        return self.line


def fake_trackers(monkeypatch, **lines):
    """Fake doctor.trackers.get: one board per kind; returns the list of kinds asked for."""
    asked = []

    def get(kind):
        asked.append(kind)
        board = FakeBoard(lines.get(kind, "ok: fake"))
        return board
    monkeypatch.setattr(doctor.trackers, "get", get)
    return asked


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    fake_trackers(monkeypatch)
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


def test_doctor_json_lists_each_check(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    C.ATTENTION = tmp_path / "attn.sh"
    monkeypatch.setattr(local_model, "_request",
                        lambda url, payload, timeout: json.dumps({"models": [{"name": "r:1"}, {"name": "s:1"}]}))
    code, out = run(capsys, json=True)
    assert code == 0
    assert json.loads(out) == {"checks": [
        {"name": "local_models", "ok": True,
         "lines": ["Ollama: up, 2 models", "route: r:1 is pulled", "summarise: s:1 is pulled"]},
        {"name": "tracker", "ok": True, "lines": ["[tracker] ok: fake"]},
        {"name": "assistant_account", "ok": True, "lines": ["assistant: acme (claude), first usable account"]},
        {"name": "notify_settings", "ok": True, "lines": ["quiet hours: off"]},
        {"name": "notifications", "ok": True, "lines": [f"attention_cmd: {tmp_path / 'attn.sh'}", "chat alerts: none"]},
        {"name": "queued_writes", "ok": True, "lines": ["Queued writes: none"]}]}


def test_doctor_warns_when_the_host_is_on_the_lan(capsys, monkeypatch):
    C.MODELS = {"local": {"host": "http://192.168.1.9:11434", "allow_lan": True, "route": "r:1"}}
    monkeypatch.setattr(local_model, "_request", lambda url, payload, timeout: json.dumps({"models": [{"name": "r:1"}]}))
    _, out = run(capsys)
    assert "192.168.1.9 is on your network, not this machine" in out


def test_doctor_ok_tracker_without_intake(capsys, monkeypatch):
    monkeypatch.setattr(local_model, "_request", lambda *a: json.dumps({"models": [{"name": "r:1"}, {"name": "s:1"}]}))
    C.INTAKE = {}
    code, out = run(capsys)
    assert code == 0
    assert "ok tracker\n  [tracker] ok: fake" in out


def test_doctor_fails_when_intake_test_is_not_ok(capsys, monkeypatch):
    fake_trackers(monkeypatch, intake="cannot reach board")
    C.INTAKE = {"type": "local", "path": "elsewhere"}
    code, out = run(capsys)
    assert code == 1
    assert "FAIL tracker\n  [tracker] ok: fake\n  [intake] cannot reach board" in out


def test_doctor_fails_with_the_error_when_building_the_tracker_raises(capsys, monkeypatch):
    fake_trackers(monkeypatch, tracker=SystemExit("pl: set VIKUNJA_TOKEN to the token"))
    C.INTAKE = {}
    code, out = run(capsys)
    assert code == 1
    assert "FAIL tracker" in out and "[tracker] pl: set VIKUNJA_TOKEN" in out


def test_doctor_masks_an_error_from_a_tracker(capsys, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "hunter2hunter2")
    from pl.trackers import mcp
    mcp._resolve("${SOME_TOKEN}", [])
    fake_trackers(monkeypatch, tracker=RuntimeError("boom hunter2hunter2"))
    C.INTAKE = {}
    _, out = run(capsys)
    assert "[tracker] error: boom" in out and "hunter2hunter2" not in out


@pytest.mark.parametrize("pair", [("local", "vikunja"), ("github-project", "github-issues"), ("mcp", "local")])
def test_doctor_json_lists_both_boards(capsys, monkeypatch, pair):
    fake_trackers(monkeypatch)
    C.TRACKER, C.INTAKE = {"type": pair[0], "name": "a"}, {"type": pair[1], "name": "b"}
    _, out = run(capsys, json=True)
    lines = next(c for c in json.loads(out)["checks"] if c["name"] == "tracker")["lines"]
    assert len(lines) == 2 and lines[0].startswith("[tracker]") and lines[1].startswith("[intake]")


def test_doctor_tests_a_shared_board_once(capsys, monkeypatch):
    asked = fake_trackers(monkeypatch)
    C.TRACKER = {"type": "vikunja", "url": "http://x", "columns": ["A"]}
    C.INTAKE = {**C.TRACKER, "columns": ["B"]}
    _, out = run(capsys, json=True)
    lines = next(c for c in json.loads(out)["checks"] if c["name"] == "tracker")["lines"]
    assert asked == ["tracker"] and lines == ["[tracker] and [intake] ok: fake"]


def test_doctor_names_the_stage_the_assistant_account_comes_from(capsys):
    C.STAGES = {"plan": {"accounts": ["acme"]}}
    _, out = run(capsys)
    assert "assistant: acme (claude), from stage plan" in out


def test_doctor_fails_when_no_account_can_run_the_assistant(capsys):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "antigravity"}
    code, out = run(capsys)
    assert code == 1
    assert "FAIL assistant_account" in out and "[assistant] account" in out


@pytest.mark.parametrize("value", ["10pm-7am", "25:00-07:00", "22:00"])
def test_doctor_names_a_bad_notify_quiet(capsys, monkeypatch, value):
    C.NOTIFY = {**C.NOTIFY, "quiet": value}
    code, out = run(capsys)
    assert code == 1
    assert "FAIL notify_settings" in out and "notify.quiet" in out and "HH:MM-HH:MM" in out


def test_doctor_reports_quiet_hours_when_valid(capsys, monkeypatch):
    C.NOTIFY = {**C.NOTIFY, "quiet": "22:00-07:00"}
    _, out = run(capsys)
    assert "ok notify_settings" in out and "quiet hours: 22:00-07:00" in out


def _machine_with_connector(tmp_path, monkeypatch, routes):
    mdir = tmp_path / "machine"
    mdir.mkdir(exist_ok=True)
    monkeypatch.setenv("PL_MACHINE_DIR", str(mdir))
    (mdir / "machine.toml").write_text('[connectors.tg]\nrole = "messaging"\nadapter = "telegram"\n'
                                       f'token = "${{T}}"\nallow = ["1"]\nroutes = {json.dumps(routes)}\n')


def test_doctor_warns_when_nothing_can_notify(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    C.ATTENTION = None
    _, out = run(capsys)
    assert "ok notifications" in out
    assert "warning:" in out and "pl setup" in out and "[paths] attention_cmd" in out and "pl chat route alerts" in out


def test_doctor_json_reports_the_warning_as_ok(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    C.ATTENTION = None
    _, out = run(capsys, json=True)
    check = next(c for c in json.loads(out)["checks"] if c["name"] == "notifications")
    assert check["ok"] is True and check["lines"][0].startswith("warning:")


@pytest.mark.parametrize("how", ["attention", "alerts_route"])
def test_doctor_no_warning_with_attention_or_alerts_route(capsys, monkeypatch, tmp_path, how):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    C.ATTENTION = None
    if how == "attention":
        C.ATTENTION = tmp_path / "attn.sh"
    else:
        _machine_with_connector(tmp_path, monkeypatch, ["gates", "alerts"])
    _, out = run(capsys, json=True)
    check = next(c for c in json.loads(out)["checks"] if c["name"] == "notifications")
    assert check["ok"] is True and not any(ln.startswith("warning:") for ln in check["lines"])
