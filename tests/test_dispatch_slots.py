"""Prep slots count what really runs; one account is logged, stored and launched; the folder trust prompt is
reported and never answered."""
import subprocess
import time
import types

import pytest

from pl import accounts, agents, alerts, dispatch, harnesses
from pl import config as C

REAL_LIVE_WINDOW = dispatch.live_agent_window   # conftest fakes the module name for every other test
TRUST_SCREEN = ("Accessing workspace:\n\n /Users/me/Code/Proj\n\n Quick safety check: Is this a project you created or one you\n"
                " trust?\n\n Security guide\n\n ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude", "codex": tmp_path / ".codex", "agy": tmp_path / "agy"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}, "codex": {"harness": "codex"}, "agy": {"harness": "antigravity"}}
    C.STAGES = {"spec": {"account": "claude"}}
    C.PROMPTS = {**C.PROMPTS, "spec": "/pl-spec {id}"}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(n, **meta):
    return {"id": f"o/r#{n}", "title": f"Card {n}", "list_id": "Inbox", "updated_at": f"2026-10-0{n}",
            "metadata": {"pipeline_mode": "auto", **meta}}


def _pass(monkeypatch, cs, swept=0, parked=(), max_prep=2):
    """One dispatcher pass, board and tmux faked. Returns (started cards, board writes)."""
    started, writes = [], []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: writes.append((cid, f)))
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append(c))
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: swept)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set(parked))
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, max_prep=max_prep, pull=False)
    return started, writes


# ---------- Bug 1: slots come from what is running ----------

def test_untracked_live_prep_windows_hold_slots(monkeypatch):
    started, _ = _pass(monkeypatch, [_card(1), _card(2)], swept=2)
    assert started == []
    started, _ = _pass(monkeypatch, [_card(1), _card(2)], swept=1)
    assert [c["id"] for c in started] == ["o/r#1"]


def test_sweep_untracked_counts_live_untracked_prep_windows_it_leaves_open(monkeypatch):
    now = int(time.time())
    out = (f"@1 %1 {now} spec-busy node\n@2 %2 {now} spec-empty zsh\n@3 %3 {now - 5000} spec-old zsh\n"
           f"@4 %4 {now} run-x node\n@5 %5 {now} dispatch python3.12\n")
    calls = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(["tmux", *a]))
    assert dispatch.sweep_untracked([], {}, False) == 1                   # spec-busy only; spec-old is closed, spec-empty is a shell
    assert ["tmux", "kill-window", "-t", "@3"] in calls


def test_a_card_with_a_running_window_its_record_lacks_gets_no_second_agent(monkeypatch):
    seen = []
    monkeypatch.setattr(dispatch, "live_agent_window", lambda name, own=None: seen.append((name, own)) or True)
    started, _ = _pass(monkeypatch, [_card(1)])
    assert started == [] and seen == [("spec-card-1", None)]


def test_live_agent_window_ignores_shells_other_names_and_the_cards_own_window(monkeypatch):
    out = "@1 zsh spec-card-1\n@2 node spec-card-2\n@3 node spec-card-1\n"
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
    assert REAL_LIVE_WINDOW("spec-card-1") is True
    assert REAL_LIVE_WINDOW("spec-card-1", own="@3") is False
    assert REAL_LIVE_WINDOW("spec-card-9") is False


def test_a_failed_record_write_closes_the_new_window_and_starts_no_agent(monkeypatch):
    calls = []

    def tmux(*args, check=True):
        calls.append(args)
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    def boom(cid, **f):
        raise SystemExit("pl: GitHub rate-limited, retrying at 22:16")
    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(dispatch, "update", boom)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(SystemExit):
        dispatch.start_worker(_card(1, profile="claude"), "spec", 1, False)
    assert ("kill-window", "-t", "@7") in [a[:3] for a in calls]
    assert not [a for a in calls if a[0] == "send-keys"]


# ---------- Bug 2: one account everywhere ----------

def test_the_stage_account_is_logged_stored_and_launched(monkeypatch, capsys):
    monkeypatch.setattr(dispatch, "healthy_profile", lambda *a, **k: "codex")   # the old pick: ignored for a pinned stage
    c = _card(1)                                                                  # no profile yet
    started, writes = _pass(monkeypatch, [c])
    out = capsys.readouterr().out
    assert writes == [("o/r#1", {"metadata": {"profile": "claude"}})] and "None" not in out
    assert started[0]["metadata"]["profile"] == "claude"
    assert harnesses.harness_for("spec", started[0])[1] == "claude"


def test_a_parked_stage_account_waits_instead_of_launching_under_another(monkeypatch, capsys):
    started, writes = _pass(monkeypatch, [_card(1, profile="codex")], parked={"claude"})
    assert started == [] and writes == []
    assert "account claude is parked" in capsys.readouterr().out


def test_a_card_without_a_profile_is_never_reported_as_none(monkeypatch, capsys):
    C.STAGES = {}
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cards, **k: "agy")
    started, writes = _pass(monkeypatch, [_card(1)])
    out = capsys.readouterr().out
    assert "None" not in out and writes == [("o/r#1", {"metadata": {"profile": "agy"}})]
    assert started[0]["metadata"]["profile"] == "agy"


def test_a_parked_card_profile_is_reported_by_name(monkeypatch, capsys):
    C.STAGES = {}
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cards, **k: "agy")
    _pass(monkeypatch, [_card(1, profile="codex")])
    assert "profile codex is parked; using agy" in capsys.readouterr().out


# ---------- Bug 3: the folder trust prompt ----------

def _screen(monkeypatch, text, cmd="2.1.288"):
    def run(argv):
        out = text if "capture-pane" in argv else cmd if "#{pane_current_command}" in argv else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(harnesses, "_run", run)
    monkeypatch.setattr(agents, "pane_exists", lambda pane: True)


def test_trust_wait_names_the_folder_and_ignores_other_screens(monkeypatch):
    claude = harnesses.get("claude")
    _screen(monkeypatch, TRUST_SCREEN)
    assert agents.trust_wait(claude, "%1") == "/Users/me/Code/Proj"
    _screen(monkeypatch, "Do you want to proceed?\n 1. Yes\n")
    assert agents.trust_wait(claude, "%1") is None
    _screen(monkeypatch, TRUST_SCREEN)
    assert agents.trust_wait(harnesses.get("codex"), "%1") is None


def test_an_agent_held_at_the_trust_prompt_is_alive_not_dead(monkeypatch):
    w = {"stage": "spec", "pane": "%1", "window": "@1", "session_id": "s", "started_at": "2020-01-01T00:00:00+00:00"}
    monkeypatch.setattr(agents, "pane_exists", lambda pane: True)
    _screen(monkeypatch, TRUST_SCREEN)
    assert agents.worker_status(w, {})[0] == "alive"
    _screen(monkeypatch, TRUST_SCREEN, cmd="zsh")                  # the person chose "No, exit": a shell is left
    assert agents.worker_status(w, {})[0] == "dead"
    _screen(monkeypatch, "plain screen")                           # unregistered for 3 min and not at the prompt
    assert agents.worker_status(w, {})[0] == "dead"


def test_the_dispatcher_alerts_on_the_trust_prompt_and_types_nothing(monkeypatch):
    w = {"stage": "spec", "pane": "%1", "window": "@1", "session_id": "s", "profile": "claude", "harness": "claude",
         "started_at": "2020-01-01T00:00:00+00:00"}
    c = _card(1, profile="claude", worker=w)
    keys, notes = [], []
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: keys.append(a))
    monkeypatch.setattr(dispatch, "notify", lambda t, m: notes.append((t, m)))
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w["session_id"]))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: "/Users/me/Code/Proj")
    started, writes = _pass(monkeypatch, [c])
    a = alerts.get("permission_wait:o/r#1")
    assert a and "trust the folder /Users/me/Code/Proj once" in a["title"] and "open the window" in a["title"]
    assert started == [] and not [k for k in keys if k and k[0] == "send-keys"]
    assert writes[-1][1]["metadata"]["worker"]["trust_wait"] == "/Users/me/Code/Proj"
    monkeypatch.setattr(agents, "col_name", lambda lid: "Inbox")
    assert agents.worker_view({**c, "metadata": {**c["metadata"], "worker": writes[-1][1]["metadata"]["worker"]}}, {}) \
        == "spec agent waiting: trust the folder (claude)"
