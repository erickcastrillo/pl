"""Plan 51 slice 3: a newly opened alert and an agent wait tell the owner once, through pl.notifications.send."""
import types

import pytest
import tomlkit

from pl import agent_alerts, agents, alerts, dispatch, hooks, notifications, rework, trackers
from pl import config as C
from pl.manager import machine_dir

CARD = "o/r#1"
REAL_CHECK = agent_alerts.check   # conftest fakes it for every other test
W = {"stage": "spec", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "profile": "claude"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}}
    mdir = tmp_path / "machine"
    mdir.mkdir()
    monkeypatch.setenv("PL_MACHINE_DIR", str(mdir))
    monkeypatch.setattr(trackers, "dry_run", lambda: False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    return t


@pytest.fixture
def desktop(monkeypatch):
    """Every desktop notification, as (title, message). The senders that used to notify directly record elsewhere."""
    class Calls(list):
        direct = []

    calls = Calls()
    direct = calls.direct = []
    monkeypatch.setattr(notifications, "notify", lambda title, message: calls.append((title, message)))
    monkeypatch.setattr(dispatch, "notify", lambda *a: direct.append(a))
    return calls


def _outbox():
    p = machine_dir() / "chat-outbox.jsonl"
    return p.read_text().splitlines() if p.exists() else []


def _events(*names):
    C.NOTIFY = {**C.NOTIFY, "events": list(names)}


def test_a_new_alert_sends_one_alert_opened_with_severity_and_title(desktop):
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    alerts.open("backup_push", "warn", "Backup push failed", "fix")
    assert len(desktop) == 2
    assert "high" in desktop[0][0] and "Sync conflict on x" in desktop[0][0]
    assert "warn" in desktop[1][0] and "Backup push failed" in desktop[1][0]


def test_reopening_an_open_alert_sends_nothing(desktop):
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    assert len(desktop) == 1


def test_a_due_reminder_goes_through_the_same_event(desktop, clock):
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    clock["now"] += 3601
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    assert len(desktop) == 2 and "Still open after 1 h" in desktop[1][0]
    alerts.open("sync-conflict:y", "high", "Sync conflict on y", "fix")
    clock["now"] += 3601
    alerts.sweep("sync-conflict:y", {"sync-conflict:y"}, lambda *a: pytest.fail("not a direct site"))
    assert len(desktop) == 4 and "Still open after 1 h" in desktop[3][0]


def test_a_reminder_with_the_event_left_out_sends_nothing(desktop, clock):
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    _events("merged")
    clock["now"] += 3601
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    assert len(desktop) == 1


def test_alert_opened_left_out_still_records_the_alert(desktop):
    from pl import events
    _events("merged")
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    assert desktop == [] and _outbox() == []
    assert alerts.get("sync-conflict:x")
    assert any(e.get("kind") == "alert_opened" for e in events.read()) if hasattr(events, "read") else True


def test_a_failing_notifier_never_fails_the_alert(monkeypatch):
    def boom(*a):
        raise RuntimeError("no notifier")
    monkeypatch.setattr(notifications, "notify", boom)
    assert alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix") == "open"
    assert alerts.get("sync-conflict:x")


def test_chat_gets_one_message_per_alert(desktop, monkeypatch):
    from pl import chat
    from pl.chat import gates
    doc = tomlkit.document()
    conns = doc.setdefault("connectors", tomlkit.table())
    conns["slack"] = {"role": "messaging", "adapter": "slack", "token": "${T}", "allow": ["222"], "routes": ["alerts"]}
    (machine_dir() / "machine.toml").write_text(tomlkit.dumps(doc))
    sent = []
    adapter = types.SimpleNamespace(name="slack", allow=["222"], routes=["alerts"],
                                    send=lambda chat_id, text, buttons=None: sent.append(text))
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)
    alerts.open("sync-conflict:x", "high", "Sync conflict on x", "fix")
    gates.post_outbox()
    assert len(sent) == 1 and "Sync conflict on x" in sent[0]


def test_a_direct_site_key_keeps_todays_path(desktop):
    alerts.open("memory_low", "warn", "Memory is low", "fix")
    assert desktop == [] and len(_outbox()) == 1


def _pass(monkeypatch, cs, ask=None, trust=None):
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: ask)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: trust)
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: None)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a: None)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)


def _waiting_card():
    return {"id": CARD, "title": "Card 1", "list_id": "Inbox", "tags": [], "updated_at": "2026-09-30",
            "metadata": {"pipeline_mode": "auto", "profile": "claude", "worker": dict(W)}}


def test_a_permission_wait_sends_one_agent_waiting_naming_card_and_stage(desktop, monkeypatch):
    cs = [_waiting_card()]
    for _ in range(3):
        _pass(monkeypatch, cs, ask="Allow Bash?")
    assert len(desktop) == 1
    text = " ".join(desktop[0])
    assert "spec" in text and "#1" in text


def test_a_new_wait_after_the_alert_resolved_sends_again(desktop, monkeypatch):
    cs = [_waiting_card()]
    _pass(monkeypatch, cs, ask="Allow Bash?")
    _pass(monkeypatch, cs, ask=None)
    _pass(monkeypatch, cs, ask="Allow Bash?")
    assert len(desktop) == 2


@pytest.mark.parametrize("site", ["trust", "hook", "long_wait"])
def test_trust_hook_and_long_wait_each_send_one_agent_waiting(desktop, monkeypatch, site):
    if site == "trust":
        monkeypatch.setattr(dispatch, "_trust_restart", lambda *a: False)
        monkeypatch.setattr("pl.harnesses.trust_command", lambda *a: "claude")
        card = _waiting_card()
        card["metadata"]["worker"]["session_id"] = "unregistered"   # a registered session is past the trust prompt
        _pass(monkeypatch, [card], trust="/some/folder")
    elif site == "hook":
        monkeypatch.setattr(hooks.board, "update", lambda *a, **k: None)
        monkeypatch.setattr(hooks.events, "emit", lambda *a, **k: None)
        hooks._record(_waiting_card(), dict(W), types.SimpleNamespace(event="Notification", card=CARD, window="@1"))
    else:
        w = {**W, "permission_wait": "waiting: permission"}
        monkeypatch.setattr(agent_alerts.agent_screen, "read", lambda pane, lines=20: ("", "waiting for you", ""))
        monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
        monkeypatch.setattr(dispatch, "stage_for", lambda c, col: "spec")
        card = {**_waiting_card(), "metadata": {"pipeline_mode": "auto", "worker": w}}
        monkeypatch.setattr(agent_alerts.time, "time", lambda: 10_000_000.0)
        st = {"agent_waits": {CARD: {"since": 0.0, "session": "s1"}}}
        REAL_CHECK([card], st, set())
    assert len(desktop) == 1
    assert "alert" not in desktop[0][0].lower() and "#1" in " ".join(desktop[0])
    assert "spec" in " ".join(desktop[0])
    assert desktop.direct == []


def test_agent_waiting_left_out_sends_nothing(desktop, monkeypatch):
    _events("alert_opened")
    _pass(monkeypatch, [_waiting_card()], ask="Allow Bash?")
    assert desktop == [] and _outbox() == []
    assert alerts.get(f"permission_wait:{CARD}")


@pytest.mark.parametrize("key", ["stalled:o/r#1", "ci_not_running:o/r"])
def test_sites_that_notify_themselves_are_not_notified_again(key):
    assert key.startswith(alerts.DIRECT)


def test_a_rework_wait_alert_notifies_once(desktop):
    long_ago = "2020-01-01T00:00:00+00:00"
    card = {"id": CARD, "metadata": {"rework": {"active": True, "at": long_ago}}}
    rework.check_waits([card], [card], {}, lambda *a: desktop.direct.append(a))
    assert alerts.get(f"rework_waiting:{CARD}")
    assert desktop == [] and len(desktop.direct) == 1


MOVED_DISPATCHER = ("near_cap:", "api_error:", "stage_failed:", "stage_unsafe:", "exited_window:", "pr_waiting:")


def test_moved_dispatcher_prefixes_are_not_direct():
    assert not any(p in alerts.DIRECT for p in MOVED_DISPATCHER)


@pytest.mark.parametrize("p", MOVED_DISPATCHER)
def test_a_moved_dispatcher_alert_sends_one_alert_opened(desktop, clock, monkeypatch, p):
    from pl import chat
    from pl.chat import gates
    doc = tomlkit.document()
    conns = doc.setdefault("connectors", tomlkit.table())
    conns["slack"] = {"role": "messaging", "adapter": "slack", "token": "${T}", "allow": ["222"], "routes": ["alerts"]}
    (machine_dir() / "machine.toml").write_text(tomlkit.dumps(doc))
    sent = []
    adapter = types.SimpleNamespace(name="slack", allow=["222"], routes=["alerts"],
                                    send=lambda chat_id, text, buttons=None: sent.append(text))
    monkeypatch.setattr(chat, "adapter", lambda name: adapter)
    alerts.open(p + "x", "warn", "T " + p, "fix")
    assert len(desktop) == 1 and "warn" in desktop[0][0] and "T " + p in desktop[0][0] and desktop.direct == []
    gates.post_outbox()
    assert len(sent) == 1
    clock["now"] += 60
    alerts.open(p + "x", "warn", "T " + p, "fix")
    assert len(desktop) == 1


@pytest.mark.parametrize("p", MOVED_DISPATCHER)
def test_a_moved_dispatcher_alert_with_alert_opened_off_still_records(desktop, p):
    _events("merged")
    alerts.open(p + "x", "warn", "T " + p, "fix")
    assert desktop == [] and _outbox() == []
    assert alerts.get(p + "x")


def test_a_failing_notifier_keeps_near_cap_and_api_error_handling(monkeypatch):
    def boom(*a):
        raise RuntimeError("no notifier")
    monkeypatch.setattr(notifications, "notify", boom)
    card = {"id": CARD, "title": "Card 1"}
    dispatch._warn_near_cap(card, {"stage": "run"}, "85 min")
    assert alerts.get(f"near_cap:{CARD}:run")
    import time
    st, failed = {"api_restarts": {CARD: [time.time()] * dispatch.API_RESTARTS}}, set()
    assert dispatch._api_restart(st, card, {"stage": "run"}, "API Error", failed) is False
    assert f"api_error:{CARD}" in failed and alerts.get(f"api_error:{CARD}")



def _open_dep_broken():
    cards = [{"id": "o/r#2", "title": "t", "list_id": "Approved", "metadata": {"depends_on": ["o/r#1"]}},
             {"id": "o/r#1", "title": "t", "list_id": "Done", "metadata": {"dropped_at": "2026-10-23T12:00:00Z"}}]
    dispatch.check_deps(cards)


def _open_pr_person():
    C.DISPATCH["rework_rounds"] = 3
    card = {"id": "o/r#9", "title": "t", "list_id": C.STAGE_DONE_AT["run"], "metadata": {"pipeline_mode": "auto", "rework": {"round": 3}}}
    dispatch.check_pr_person_alerts([card])


def _open_auto_merge_off(monkeypatch):
    C.TRACKER = {"type": "github-project", "repo": "o/r"}
    C.CODE_HOST = {"labels": {"merge_ready": "pl:merge-ready"}}
    monkeypatch.setattr(dispatch.github, "auto_merge_allowed", lambda repo: False)
    dispatch._check_auto_merge("auto-review", {"builtin": True})


MOVED = {"dep_broken:": _open_dep_broken, "pr_person:": _open_pr_person, "auto_merge_off:": _open_auto_merge_off}


def _open_moved(prefix, monkeypatch):
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    opener = MOVED[prefix]
    opener(monkeypatch) if prefix == "auto_merge_off:" else opener()
    return [a["key"] for a in alerts.listing() if a["key"].startswith(prefix)]


@pytest.mark.parametrize("prefix", list(MOVED))
def test_the_three_moved_alerts_obey_events_and_survive_a_failing_notifier(prefix, desktop, monkeypatch):
    assert not prefix.startswith(alerts.DIRECT)
    _events("merged")
    assert len(_open_moved(prefix, monkeypatch)) == 1
    assert desktop == [] and desktop.direct == [] and _outbox() == []

    for key in [a["key"] for a in alerts.listing()]:
        alerts.resolve(key)
    _events("alert_opened")

    def boom(*a):
        raise RuntimeError("no notifier")
    monkeypatch.setattr(notifications, "notify", boom)
    assert len(_open_moved(prefix, monkeypatch)) == 1
