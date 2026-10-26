"""pl hook: Claude Code Notification and Stop hooks report a stage agent's state at once."""
import json
import shlex
import time
import types
from datetime import datetime, timezone

import pytest

from pl import accounts, agents, alerts, dispatch, harnesses, hooks
from pl import config as C

REAL_ENABLED = hooks.enabled   # conftest turns hooks off for every other test
CARD = "o/r#1"
WINDOW = "@7"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "enabled", REAL_ENABLED)
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude", "codex": tmp_path / ".codex"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}, "codex": {"harness": "codex"}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(**worker):
    return {"id": CARD, "title": "Card 1", "list_id": "In progress",
            "metadata": {"worker": {"stage": "run", "window": WINDOW, "pane": "%7", **worker}}}


@pytest.fixture
def board(monkeypatch):
    """The card the hook looks up, the writes it makes and the notifications it sends."""
    state = types.SimpleNamespace(card=_card(), writes=[], notes=[])
    monkeypatch.setattr(hooks.board, "card", lambda cid: state.card if cid == CARD else None)
    monkeypatch.setattr(hooks.board, "update", lambda cid, **f: state.writes.append((cid, f)))
    monkeypatch.setattr(hooks, "notify", lambda t, m: state.notes.append((t, m)))
    return state


def _run(event, card=CARD, window=WINDOW, text=()):
    return hooks.cmd_hook(types.SimpleNamespace(event=event, card=card, window=window, text=list(text)))


def _worker(board):
    return board.writes[-1][1]["metadata"]["worker"]


def test_hook_notification_updates_card_and_opens_alert(board):
    _run("Notification")
    w = _worker(board)
    assert w["permission_wait"] == "waiting: permission"
    assert w["hook_event"]["event"] == "Notification" and w["stage"] == "run"
    assert alerts.get(f"permission_wait:{CARD}") and board.notes


def test_hook_stop_records_finished_turn(board):
    board.card = _card(permission_wait="waiting: permission")
    _run("Stop")
    w = _worker(board)
    assert w["finished_turn"] is True and "permission_wait" not in w
    assert w["hook_event"]["event"] == "Stop"


@pytest.mark.parametrize("card,window", [("o/other#9", WINDOW), (CARD, "@99")])
def test_hook_validation_exits_2_on_foreign_card_or_window(board, card, window):
    with pytest.raises(SystemExit) as e:
        _run("Notification", card, window)
    assert e.value.code == 2 and board.writes == [] and alerts.get(f"permission_wait:{card}") is None


def test_hook_sanitizes_and_never_executes_free_text(board, tmp_path):
    marker = tmp_path / "ran"
    _run("Notification", text=[f"$(touch {marker}) sk-ant-api03-" + "a" * 60, "x" * 900])
    assert not marker.exists()
    stored = json.dumps(board.writes)
    assert "touch" not in stored and "sk-ant" not in stored and "xxxx" not in stored


def test_hook_failsafe_exits_0_on_internal_error(board, monkeypatch):
    def locked(*a, **k):
        raise OSError("state folder locked")
    monkeypatch.setattr(hooks.board, "update", locked)
    _run("Notification")


def _script(account="claude", **kw):
    return harnesses.launch_script(harnesses.get(kw.pop("harness", "claude")), account, "go", "sid", "run:x", **kw)


def _settings(script):
    argv = shlex.split(script.splitlines()[-1])
    return json.loads(argv[argv.index("--settings") + 1]) if "--settings" in argv else None


def test_launch_script_includes_hooks_for_claude_stage():
    s = _settings(_script(stage="run", hooks=(CARD, WINDOW)))
    assert set(s["hooks"]) == {"Notification", "Stop"}
    cmd = s["hooks"]["Stop"][0]["hooks"][0]
    assert cmd["command"] == f"pl hook Stop --card {shlex.quote(CARD)} --window {WINDOW} || true"
    assert cmd["type"] == "command" and cmd["timeout"] == 2


def test_launch_script_omits_hooks_when_disabled_or_non_stage():
    C.DISPATCH = {**C.DISPATCH, "hooks": False}
    assert _settings(_script(stage="run", hooks=(CARD, WINDOW))) is None
    C.DISPATCH = {**C.DISPATCH, "hooks": True}
    assert _settings(_script()) is None                          # an assistant or a loop passes no card
    assert _settings(_script(stage="run")) is None


def test_launch_script_omits_hooks_for_non_claude_harnesses():
    assert _settings(_script("codex", harness="codex", stage="run", hooks=(CARD, WINDOW))) is None


def test_hook_settings_keep_the_prompt_last():
    argv = shlex.split(_script(stage="run", hooks=(CARD, WINDOW)).splitlines()[-1])
    assert argv[-1] == "go"


def _alive(monkeypatch, status="idle", activity=None):
    def run(argv):
        out = "" if "capture-pane" in argv else str(int(activity if activity is not None else time.time()))
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(harnesses, "_run", run)
    monkeypatch.setattr(agents, "pane_exists", lambda pane: True)
    return {"sid": {"status": status, "statusUpdatedAt": time.time() * 1000}}


def test_dispatch_bypasses_quiet_timer_when_finished_turn(monkeypatch):
    reg = _alive(monkeypatch)
    w = {"session_id": "sid", "pane": "%7", "harness": "claude"}
    assert agents.run_waiting(w, reg) is None                    # the pane just changed: still inside the quiet timer
    assert agents.run_waiting({**w, "finished_turn": True}, reg) == "waiting (finished turn)"
    assert agents.run_waiting({**w, "finished_turn": True}, _alive(monkeypatch, "busy")) is None


def _hook_worker(event, at, **kw):
    return {"permission_wait": "waiting: permission", "pane": "%7",
            "hook_event": {"event": event, "at": datetime.fromtimestamp(at, timezone.utc).isoformat()}, **kw}


def test_dispatch_reconciliation_picks_newer_timestamp(monkeypatch):
    now = time.time()
    _alive(monkeypatch, activity=now - 60)                       # the screen last changed a minute ago
    assert hooks.hook_permission(_hook_worker("Notification", now)) == "waiting: permission"
    _alive(monkeypatch, activity=now)                            # the screen moved on after the hook: answered
    assert hooks.hook_permission(_hook_worker("Notification", now - 60)) is None
    _alive(monkeypatch, activity=now - 60)
    assert hooks.hook_permission(_hook_worker("Stop", now)) is None


def _pass(monkeypatch, c, ask=None):
    writes = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [c])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: writes.append((cid, f)))
    monkeypatch.setattr(dispatch, "start_worker", lambda *a: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "notify", lambda *a: None)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w["session_id"]))
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: ask)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, max_prep=2, pull=False)
    return writes


def _hooked_card():
    w = _hook_worker("Notification", time.time(), stage="spec", window=WINDOW, session_id="s", profile="claude",
                     harness="claude", started_at="2020-01-01T00:00:00+00:00")
    return {**_card(), "list_id": "Inbox", "metadata": {"pipeline_mode": "auto", "profile": "claude", "worker": w}}


def test_a_hook_permission_wait_keeps_its_alert_when_the_screen_shows_nothing(monkeypatch):
    _alive(monkeypatch, activity=time.time() - 60)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    alerts.open(f"permission_wait:{CARD}", "warn", "agent waiting for permission", "answer it")
    writes = _pass(monkeypatch, _hooked_card())
    assert not alerts.get(f"permission_wait:{CARD}").get("resolved_at")
    assert not [f for _, f in writes if "permission_wait" not in f.get("metadata", {}).get("worker", {"permission_wait": 1})]


def test_dispatch_reconciliation_prefers_limit_over_hook(monkeypatch):
    _alive(monkeypatch, activity=time.time() - 60)
    asked = []
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: "You've hit your usage limit")
    monkeypatch.setattr(hooks, "hook_permission", lambda w: asked.append(w))
    monkeypatch.setattr(accounts, "mark_exhausted", lambda prof, screen: "2026-10-24T12:00:00+00:00", raising=False)
    monkeypatch.setattr(dispatch, "mark_exhausted", lambda prof, screen: "2026-10-24T12:00:00+00:00")
    monkeypatch.setattr(dispatch, "healthy_profile", lambda *a, **k: None)
    writes = _pass(monkeypatch, _hooked_card())
    assert asked == [] and any(f["metadata"]["worker"].get("limit_hit") for _, f in writes if "worker" in f.get("metadata", {}))


def _emitted(monkeypatch):
    seen = []
    monkeypatch.setattr(hooks.events, "emit", lambda kind, card=None, **d: kind.startswith("alert_") or seen.append((kind, card, d)))
    return seen


@pytest.mark.parametrize("event", ["Notification", "Stop"])
def test_hook_emits_state_transition_event(board, monkeypatch, event):
    seen = _emitted(monkeypatch)
    _run(event)
    assert seen == [("state_transition", CARD, {"source": "hook", "event": event})]


def test_a_foreign_hook_emits_nothing(board, monkeypatch):
    seen = _emitted(monkeypatch)
    with pytest.raises(SystemExit):
        _run("Stop", window="@99")
    assert seen == []


def test_screen_permission_wait_emits_one_screen_transition(monkeypatch):
    seen = []
    monkeypatch.setattr(dispatch.events, "emit", lambda kind, card=None, **d: seen.append((kind, card, d)))
    _alive(monkeypatch, activity=time.time() - 60)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    c = _hooked_card()
    c["metadata"]["worker"].pop("permission_wait")
    c["metadata"]["worker"].pop("hook_event")
    _pass(monkeypatch, c, ask="waiting: permission")
    assert [s for s in seen if s[0] == "state_transition"] == [
        ("state_transition", CARD, {"source": "screen", "transition": "permission_wait"})]

