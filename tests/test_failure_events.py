"""Plan 48 slice 3: failures pl detects reach the Activity log as masked, levelled entries."""
import dataclasses
import json

import pytest

from pl import accounts, assistant, dispatch, events, harnesses, triggers
from pl import config as C
from pl.tui import loops
from pl.trackers import mcp

SECRET = "s3cr3t-value-of-a-token"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setenv("PL_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text(f'[tracker]\ntype = "local"\n[accounts.acme]\nconfig_dir = "{tmp_path}/acct"\n')
    (tmp_path / "acct").mkdir()
    C.load("t")
    harnesses._CACHE.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)
    harnesses._CACHE.clear()


def _logged(kind=None):
    p = C.STATE_DIR / "events.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if kind is None or r["kind"] == kind]


def _card(cid="o/r#1", **meta):
    return {"id": cid, "title": "t", "list_id": "Inbox", "updated_at": "2026-10-01",
            "metadata": {"pipeline_mode": "auto", **meta}}


def _fake_pass(monkeypatch, cs):
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)


def test_failure_masks_then_truncates(monkeypatch):
    monkeypatch.setitem(mcp._secrets, SECRET, "X") if isinstance(mcp._secrets, dict) else mcp._secrets.add(SECRET)
    msg = "a" * (events.ERROR_CHARS - 10) + SECRET + "b" * 300
    events.failure("sync_x", "warning", msg, "o/r#1", step="s")
    [e] = _logged("sync_x")
    assert e["level"] == "warning" and e["card"] == "o/r#1" and e["step"] == "s"
    assert SECRET not in json.dumps(e) and SECRET[:6] not in e["message"]
    assert len(e["message"]) <= events.ERROR_CHARS


def test_failure_never_raises_on_disk_error(fake_home):
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    C.STATE_DIR.chmod(0o500)
    try:
        events.failure("x", "error", "boom")
    finally:
        C.STATE_DIR.chmod(0o700)


def test_pass_step_skipped_logged_as_warning(monkeypatch, capsys):
    _fake_pass(monkeypatch, [])
    monkeypatch.setattr(triggers, "run_due", lambda dry=False: (_ for _ in ()).throw(RuntimeError("trigger boom")))
    dispatch.dispatch_once(1, False, pull=False)
    assert "triggers skipped this pass: trigger boom" in capsys.readouterr().err
    [e] = _logged("pass_step_skipped")
    assert e["level"] == "warning" and e["step"] == "triggers" and "trigger boom" in e["message"]


def test_card_skipped_bad_harness_logged(monkeypatch, capsys):
    _fake_pass(monkeypatch, [_card(worker={"harness": "nope", "stage": "spec"})])
    dispatch.dispatch_once(1, False, pull=False)
    assert "skipped" in capsys.readouterr().err
    [e] = _logged("card_skipped")
    assert e["level"] == "warning" and e["card"] == "o/r#1"


def test_assistant_no_free_account_logs_harness_start_failed(monkeypatch):
    monkeypatch.setattr(assistant, "account", lambda: None)
    with pytest.raises(SystemExit) as exc:
        assistant.ensure()
    [e] = _logged("harness_start_failed")
    assert e["level"] == "error" and e["stage"] == "assistant" and e["message"] == str(exc.value.code)


def test_assistant_no_interactive_template_names_account_and_harness(monkeypatch):
    monkeypatch.setattr(assistant, "account", lambda: "acme")
    h = dataclasses.replace(harnesses.account_harness("acme"), interactive=None)
    monkeypatch.setattr(harnesses, "account_harness", lambda name: h)
    with pytest.raises(SystemExit):
        assistant.ensure()
    [e] = _logged("harness_start_failed")
    assert e["account"] == "acme" and e["harness"] == h.name


def test_assistant_busy_lock_logs_nothing(monkeypatch):
    def busy(wait=None, where=None):
        raise SystemExit(assistant.BUSY)
    monkeypatch.setattr(assistant, "_locked", busy)
    with pytest.raises(SystemExit):
        assistant.ensure()
    assert _logged("harness_start_failed") == []


def test_stage_start_tmux_failure_logs_harness_start_failed(monkeypatch):
    _fake_pass(monkeypatch, [_card()])

    def boom(*a, **k):
        raise SystemExit("pl: tmux new-window: no server")
    monkeypatch.setattr(dispatch, "start_worker", boom)
    monkeypatch.setattr(dispatch, "_start_who", lambda stage, c: {"account": "acme", "harness": "claude"})
    with pytest.raises(SystemExit):
        dispatch.dispatch_once(1, False, pull=False)
    [e] = _logged("harness_start_failed")
    assert e["level"] == "error" and e["card"] == "o/r#1" and e["account"] == "acme" and e["harness"] == "claude"
    assert e["stage"] and "tmux new-window" in e["message"]


def test_check_error_logs_account_check_failed(monkeypatch):
    monkeypatch.setattr(accounts, "_call", lambda name, h: ("error", "boom", None))
    accounts._check("acme")
    [e] = _logged("account_check_failed")
    assert e["level"] == "warning" and e["account"] == "acme"


def test_check_limited_logs_no_failure(monkeypatch):
    monkeypatch.setattr(accounts, "_call", lambda name, h: ("limited", "limit", None))
    accounts._check("acme")
    assert _logged("account_check_failed") == [] and len(_logged("account_check")) == 1


def test_mark_exhausted_logs_account_parked():
    accounts.mark_exhausted("acme", "You've hit your limit · resets 9pm")
    [e] = _logged("account_parked")
    assert e["level"] == "warning" and e["account"] == "acme" and "limit" in e["reason"]


def _record(**run):
    d = C.STATE_DIR / "loops"
    d.mkdir(parents=True, exist_ok=True)
    (d / "nightly.run.json").write_text(json.dumps(run))


def _run_loop():
    st = {}
    dispatch._ensure_scheduled_loop(st, "nightly", {"every": "1h"}, {}, [], False)


def test_loop_nonzero_exit_logs_loop_failed():
    _record(exit=2, result="it broke")
    _run_loop()
    [e] = _logged("loop_failed")
    assert e["level"] == "error" and e["loop"] == "nightly" and e["exit"] == 2 and "it broke" in e["message"]


def test_loop_zero_exit_logs_nothing():
    _record(exit=0, result="fine")
    _run_loop()
    assert _logged("loop_failed") == []


def test_activity_reads_failure_levels():
    events.failure("harness_start_failed", "error", "no account")
    events.failure("account_parked", "warning", "parked")
    rows = {e["kind"]: e for e in loops.read_events(C.STATE_DIR / "events.jsonl")}
    assert rows["harness_start_failed"]["level"] == "error" and rows["account_parked"]["level"] == "warning"
