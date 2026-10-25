"""Prep slots count what really runs; one account is logged, stored and launched; the folder trust prompt is
reported and never answered."""
import json
import subprocess
import time
import types
from pathlib import Path

import pytest

from pl import accounts, agents, alerts, dispatch, harnesses
from pl import config as C

REAL_LIVE_WINDOW = dispatch.live_agent_window   # conftest fakes the module name for every other test
REAL_GATE = dispatch.untrusted_folder   # conftest fakes it for every other test
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


def _pass(monkeypatch, cs, swept=0, parked=(), max_prep=2, dry=False):
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
    dispatch.dispatch_once(1, dry, max_prep=max_prep, pull=False)
    return started, writes


# ---------- Bug 1: slots come from what is running ----------

def test_untracked_live_prep_windows_hold_slots(monkeypatch):
    started, _ = _pass(monkeypatch, [_card(1), _card(2)], swept=2)
    assert started == []
    started, _ = _pass(monkeypatch, [_card(1), _card(2)], swept=1)
    assert [c["id"] for c in started] == ["o/r#1"]


def test_sweep_untracked_counts_live_untracked_prep_windows_it_leaves_open(monkeypatch):
    now = int(time.time())
    out = (f"@1 %1 {now} pl 0 spec-busy node\n@2 %2 {now} pl 0 spec-empty zsh\n@3 %3 {now - 5000} pl 0 spec-old zsh\n"
           f"@4 %4 {now} pl 0 run-x node\n@5 %5 {now} - 0 dispatch python3.12\n")
    calls = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(["tmux", *a]))
    assert dispatch.sweep_untracked([], {}, False) == 1                   # spec-busy only; spec-old is closed, spec-empty is a shell
    assert ["tmux", "kill-window", "-t", "@3"] in calls


def _sweep_runs(monkeypatch, out, screen="", reg=None, cs=()):
    """sweep_untracked with tmux faked: list-panes prints out, capture-pane prints screen. Returns (live, killed)."""
    killed = []

    def run(argv, *a, **kw):
        return subprocess.CompletedProcess(argv, 0, screen if "capture-pane" in argv else out, "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: killed.append(a[2]) if a[0] == "kill-window" else None)
    return dispatch.sweep_untracked(list(cs), reg or {}, False), killed


def test_an_untracked_idle_run_window_is_closed_so_its_card_can_start_a_new_run(monkeypatch):
    old = int(time.time()) - 5000
    live, killed = _sweep_runs(monkeypatch, f"@4 %4 {old} pl 0 run-fix-orphaned node\n", screen="PR #69 opened.\n❯ ")
    assert killed == ["@4"] and live == 0


def test_an_untracked_run_window_idle_in_its_registered_session_is_closed(monkeypatch):
    now = int(time.time())
    reg = {"s1": {"tmux": "pl-t:%4", "status": "idle", "statusUpdatedAt": (now - 900) * 1000}}
    _, killed = _sweep_runs(monkeypatch, f"@4 %4 {now} pl 0 run-x node\n", reg=reg)
    assert killed == ["@4"]


def test_an_untracked_run_window_waiting_at_a_human_gate_stays(monkeypatch):
    old = int(time.time()) - 5000
    _, killed = _sweep_runs(monkeypatch, f"@4 %4 {old} pl 0 run-x node\n", screen="GATE: before-push\n❯ ")
    assert killed == []


def test_busy_or_tracked_run_windows_stay_and_hold_no_prep_slot(monkeypatch):
    now, old = int(time.time()), int(time.time()) - 5000
    reg = {"s1": {"tmux": "pl-t:%4", "status": "busy", "statusUpdatedAt": now * 1000}}
    cs = [_card(1, worker={"window": "@5", "stage": "run"})]
    live, killed = _sweep_runs(monkeypatch, f"@4 %4 {now} run-busy node\n@5 %5 {old} run-mine node\n", reg=reg, cs=cs)
    assert killed == [] and live == 0


def test_a_card_with_a_running_window_its_record_lacks_gets_no_second_agent(monkeypatch):
    seen = []
    monkeypatch.setattr(dispatch, "live_agent_window", lambda name, own=None: seen.append((name, own)) or True)
    started, _ = _pass(monkeypatch, [_card(1)])
    assert started == [] and seen == [("spec-card-1", None)]


def test_live_agent_window_ignores_shells_other_names_and_the_cards_own_window(monkeypatch):
    out = "@1 pl zsh spec-card-1\n@2 pl node spec-card-2\n@3 pl node spec-card-1\n@4 - 2.1.296 spec-card-9\n"
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
    assert REAL_LIVE_WINDOW("spec-card-1") is True
    assert REAL_LIVE_WINDOW("spec-card-1", own="@3") is False
    assert REAL_LIVE_WINDOW("spec-card-9") is False


def _sweep_tmux(monkeypatch, out):
    """tmux list-panes answers out; every other tmux call is logged."""
    calls = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, out if "list-panes" in argv else "", "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: calls.append(["tmux", *a]))
    return calls


def test_untracked_stage_windows_holding_only_a_shell_are_closed_and_never_counted(monkeypatch):
    """Live after a reboot: tmux-resurrect restored run-/plan- windows with only zsh in them."""
    now = int(time.time())
    w = {"stage": "run", "window": "@5", "pane": "%5", "started_at": "2026-10-11T00:00:00+00:00"}
    out = (f"@1 %1 {now - 5000} - 0 run-restored zsh\n@2 %2 {now - 5000} - 0 plan-restored bash\n"
           f"@3 %3 {now} - 0 run-fresh zsh\n@5 %5 {now - 5000} pl 0 run-tracked zsh\n")
    calls = _sweep_tmux(monkeypatch, out)
    assert dispatch.sweep_untracked([_card(1, worker=w)], {}, False, {}) == 0
    kills = [c for c in calls if c[:2] == ["tmux", "kill-window"]]
    assert kills == [["tmux", "kill-window", "-t", "@1"], ["tmux", "kill-window", "-t", "@2"]]   # never a tracked one


def test_an_untracked_window_running_what_pl_did_not_start_is_not_counted_closed_or_reported_twice(monkeypatch):
    """A restored `claude --resume` picker in a run- or plan- window: maybe yours, so pl leaves it and tells you once."""
    old = int(time.time()) - 5000
    out = f"@1 %1 {old} - 0 run-restored 2.1.296\n@2 %2 {old} - 0 plan-restored 2.1.296\n@3 %3 {int(time.time())} pl 0 run-mine agy\n"
    calls = _sweep_tmux(monkeypatch, out)
    st = {}
    assert dispatch.sweep_untracked([], {}, False, st) == 0
    assert dispatch.sweep_untracked([], {}, False, st) == 0
    assert not [c for c in calls if c[:2] == ["tmux", "kill-window"]]
    open_ = [k for k, a in alerts._load().items() if k.startswith("foreign_window:") and not a.get("resolved_at")]
    assert sorted(open_) == ["foreign_window:@1", "foreign_window:@2"]
    assert all(a["count"] == 1 for a in alerts._load().values())             # told once, no reminders
    calls[:] = []
    _sweep_tmux(monkeypatch, out.split("\n", 1)[1])                        # you closed @1
    dispatch.sweep_untracked([], {}, False, st)
    assert alerts._load()["foreign_window:@1"].get("resolved_at")


def test_a_tracked_agent_window_gets_pls_mark_and_the_session_is_marked(monkeypatch):
    """Agents started before marks existed: the card's own window and pane, started in this tmux server."""
    start = int(time.time()) - 3600
    w = {"stage": "run", "window": "@5", "pane": "%5", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}
    old = {"stage": "plan", "window": "@6", "pane": "%6", "started_at": "2020-01-01T00:00:00+00:00"}   # before the server
    now = int(time.time())
    out = f"@5 %5 {now} - {start} run-card-1 agy\n@6 %6 {now} - {start} plan-card-2 2.1.296\n"
    calls = _sweep_tmux(monkeypatch, out)
    dispatch.sweep_untracked([_card(1, worker=w), _card(2, worker=old)], {}, False, {})
    sets = [c for c in calls if c[:2] == ["tmux", "set-option"]]
    assert sets == [["tmux", "set-option", "-w", "-t", "@5", "@pl_agent", "1"],
                    ["tmux", "set-option", "-t", "=pl-t", "@pl_marks", "1"]]


def test_start_worker_marks_its_window_as_pls(monkeypatch):
    calls = []

    def tmux(*args, check=True):
        calls.append(args)
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")
    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    dispatch.start_worker(_card(1, profile="claude"), "spec", 1, False)
    assert ("set-option", "-w", "-t", "@7", "@pl_agent", "1") in calls


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


def test_trust_command_names_the_account_config_folder(monkeypatch):
    monkeypatch.setattr(C, "HOME", Path("/home/me"))
    monkeypatch.setattr(C, "PROFILES", {"claude": Path("/home/me/.claude")})
    assert harnesses.trust_command(harnesses.get("claude"), "claude", "/home/me/code/pl") \
        == "cd ~/code/pl && CLAUDE_CONFIG_DIR=~/.claude claude"


def test_trust_command_without_a_config_folder_has_no_env_part(monkeypatch):
    monkeypatch.setattr(C, "HOME", Path("/home/me"))
    monkeypatch.setattr(C, "PROFILES", {})
    cmd = harnesses.trust_command(harnesses.get("claude"), "other", "/home/me/code/pl")
    assert cmd == "cd ~/code/pl && claude" and "CLAUDE_CONFIG_DIR" not in cmd


def test_trust_command_quotes_odd_folders_and_falls_back_to_the_work_dir(monkeypatch):
    monkeypatch.setattr(C, "HOME", Path("/home/me"))
    monkeypatch.setattr(C, "PROFILES", {})
    monkeypatch.setattr(C, "WORK_DIR", Path("/home/me/work"))
    claude = harnesses.get("claude")
    assert harnesses.trust_command(claude, "x", "/srv/a b;c") == "cd '/srv/a b;c' && claude"
    assert harnesses.trust_command(claude, "x", "/home/me/a b") == "cd ~/'a b' && claude"
    assert harnesses.trust_command(claude, "x", "~/code/pl") == "cd ~/code/pl && claude"
    assert harnesses.trust_command(claude, "x", "this folder") == "cd ~/work && claude"


def _trust_file(path, **folders):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": {f: {"hasTrustDialogAccepted": v} for f, v in folders.items()}}))


def test_claude_trusts_reads_the_account_config_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HOME", tmp_path)
    monkeypatch.setattr(C, "WORK_DIR", tmp_path / "code/pl")
    monkeypatch.setattr(C, "PROFILES", {"a": tmp_path / ".claude-a"})
    pl, x = str(tmp_path / "code/pl"), str(tmp_path / "code/x")
    _trust_file(tmp_path / ".claude-a/.claude.json", **{pl: True, x: False})
    assert harnesses.claude_trusts("a", pl) is True
    assert harnesses.claude_trusts("a", pl + "/") is True
    assert harnesses.claude_trusts("a", x) is False
    assert harnesses.claude_trusts("a", str(tmp_path / "code")) is False
    assert harnesses.claude_trusts("a", "this folder") is True
    assert harnesses.claude_trusts("a", "~/code/pl") is True
    _trust_file(tmp_path / ".claude.json", **{pl: True})
    assert harnesses.claude_trusts("nobody", pl) is True


def test_claude_trusts_says_no_on_a_missing_empty_or_broken_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(C, "HOME", tmp_path)
    monkeypatch.setattr(C, "PROFILES", {"a": tmp_path / ".claude-a"})
    f = tmp_path / ".claude-a/.claude.json"
    assert harnesses.claude_trusts("a", "/x") is False
    f.parent.mkdir()
    for text in ("", "{not json", "[]", '{"projects": []}', '{"projects": {"/y": {"hasTrustDialogAccepted": true}}}',
                 '{"projects": {"/x": "yes"}}', '{"projects": {"/x": {"hasTrustDialogAccepted": "true"}}}'):
        f.write_text(text)
        assert harnesses.claude_trusts("a", "/x") is False
    assert capsys.readouterr() == ("", "")


def test_claude_trusts_never_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PROFILES", {"a": tmp_path})
    f = tmp_path / ".claude.json"
    _trust_file(f, **{"/x": True})
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    assert harnesses.claude_trusts("a", "/x") is True
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before


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
    monkeypatch.setitem(C.PROFILES, "claude", C.HOME / ".claude")
    started, writes = _pass(monkeypatch, [c])
    a = alerts.get("permission_wait:o/r#1")
    fix = harnesses.trust_command(harnesses.get("claude"), "claude", "/Users/me/Code/Proj")
    assert a and "trust the folder /Users/me/Code/Proj" in a["title"] and fix in a["title"]
    assert "CLAUDE_CONFIG_DIR=" in a["title"] and "Yes, I trust this folder" in a["title"]
    assert "run claude in it" not in a["title"]
    assert started == [] and not [k for k in keys if k and k[0] == "send-keys"]
    assert writes[-1][1]["metadata"]["worker"]["trust_wait"] == "/Users/me/Code/Proj"
    monkeypatch.setattr(agents, "col_name", lambda lid: "Inbox")
    assert agents.worker_view({**c, "metadata": {**c["metadata"], "worker": writes[-1][1]["metadata"]["worker"]}}, {}) \
        == "spec agent waiting: trust the folder (claude)"


def _trusted_agent(monkeypatch, trusts, **meta):
    """A Claude spec agent at the trust prompt for /Users/me/Code/Proj; returns (keys sent, events emitted, stops)."""
    w = {"stage": "spec", "pane": "%1", "window": "@1", "session_id": "s", "profile": "claude", "harness": "claude",
         "started_at": "2020-01-01T00:00:00+00:00"}
    keys, evs, stops = [], [], []
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: keys.append(a))
    monkeypatch.setattr(dispatch, "notify", lambda t, m: None)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w["session_id"]))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: "/Users/me/Code/Proj")
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "pane_exists", lambda pane: False)
    monkeypatch.setattr(harnesses, "claude_trusts", lambda account, folder: trusts)
    monkeypatch.setattr(dispatch.move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(dispatch.move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(dispatch.move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(dispatch.move_agent, "stop", lambda pane, sid: stops.append(pane) or True)
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: evs.append((a, k)))
    return _card(1, profile="claude", worker=w, **meta), keys, evs, stops


def test_the_dispatcher_keeps_an_untrusted_agent_at_the_prompt(monkeypatch):
    c, keys, evs, stops = _trusted_agent(monkeypatch, False)
    started, writes = _pass(monkeypatch, [c])
    assert evs == [] and stops == [] and started == []
    assert not [k for k in keys if k and k[0] == "send-keys"]
    assert alerts.get("permission_wait:o/r#1") and not alerts.get("permission_wait:o/r#1").get("resolved_at")
    assert writes[-1][1]["metadata"]["worker"]["trust_wait"] == "/Users/me/Code/Proj"


def test_the_dispatcher_restarts_a_trusted_agent_once(monkeypatch, capsys):
    c, keys, evs, stops = _trusted_agent(monkeypatch, True)
    alerts.open("permission_wait:o/r#1", "warn", "agent waiting: trust the folder", "an earlier pass opened it")
    started, writes = _pass(monkeypatch, [c])
    assert evs == [(("agent_restarted", "o/r#1"), {"stage": "spec", "reason": "folder trusted"})]
    assert stops == ["%1"] and not [k for k in keys if k and k[0] == "send-keys"]
    assert writes[0][1]["metadata"]["worker"] is None and writes[0][1]["metadata"]["trust_restarted"] == ["spec"]
    assert started == [c]
    assert "now trusted" in capsys.readouterr().out
    assert not alerts.get("permission_wait:o/r#1").get("resolved_at")


def test_a_second_trust_prompt_after_the_restart_is_not_restarted(monkeypatch):
    c, keys, evs, stops = _trusted_agent(monkeypatch, True, trust_restarted=["spec"])
    started, _ = _pass(monkeypatch, [c])
    assert evs == [] and stops == [] and started == []
    assert not alerts.get("permission_wait:o/r#1").get("resolved_at")


def test_the_trust_alert_closes_once_the_new_agent_is_past_the_prompt(monkeypatch):
    c, keys, evs, stops = _trusted_agent(monkeypatch, False)
    _pass(monkeypatch, [c])
    assert alerts.get("permission_wait:o/r#1")
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: None)
    c = {**c, "metadata": {**c["metadata"], "worker": {**c["metadata"]["worker"], "trust_wait": "/Users/me/Code/Proj"}}}
    _, writes = _pass(monkeypatch, [c])
    assert alerts.get("permission_wait:o/r#1")["resolved_at"]
    assert "trust_wait" not in writes[-1][1]["metadata"]["worker"]


def test_a_dry_run_only_says_it_would_restart_the_trusted_agent(monkeypatch, capsys):
    c, keys, evs, stops = _trusted_agent(monkeypatch, True)
    started, writes = _pass(monkeypatch, [c], dry=True)
    assert "would stop it" in capsys.readouterr().out
    assert stops == [] and evs == [] and writes == [] and started == []


# ---------- profiles do not starve each other: the manager's share ----------

def _status(rooms, hold=None):
    """A fresh pl manager status: each profile's own room, the machine already at its cap."""
    from pl import manager
    return {"at": manager._clock(), "live_agents": 15, "max_live_agents": 15, "room": 0, "hold": hold,
            "profiles": [{"name": n, "agents": {"share": 5, "used": 5 - r, "stuck": 0, "room": r}} for n, r in rooms.items()]}


def test_a_profile_with_room_in_its_share_starts_although_the_machine_is_full(monkeypatch):
    from pl import manager
    monkeypatch.setattr(manager, "read_status", lambda: _status({C.PROFILE_NAME: 2, "busy": 0}))
    started, _ = _pass(monkeypatch, [_card(1), _card(2), _card(3)], max_prep=5)
    assert [c["id"] for c in started] == ["o/r#1", "o/r#2"]   # its own room, not the machine's 0


def test_a_profile_at_its_share_waits_and_says_why(monkeypatch, capsys):
    from pl import manager
    monkeypatch.setattr(manager, "read_status", lambda: _status({C.PROFILE_NAME: 0}))
    started, _ = _pass(monkeypatch, [_card(1)])
    assert started == [] and "share of the machine's agents is in use" in capsys.readouterr().out


def test_agents_waiting_at_a_prompt_are_reported_to_the_manager(monkeypatch):
    import json
    w = {"stage": "spec", "pane": "%1", "window": "@7", "session_id": "s", "profile": "claude", "harness": "claude",
         "started_at": "2020-01-01T00:00:00+00:00"}
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "notify", lambda t, m: None)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w["session_id"]))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: "/Users/me/Code/Proj")
    _pass(monkeypatch, [_card(1, profile="claude", worker=w), _card(2)])
    got = json.loads((C.STATE_DIR / "agent-waits.json").read_text())
    assert got["windows"] == ["@7"] and got["at"] > 0


# ---------- in-progress cards get run slots before approved ones ----------

def test_an_in_progress_card_needing_a_restart_gets_the_run_slot_before_older_approved_cards(monkeypatch, capsys):
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}"}
    approved = [{**_card(n, profile="claude"), "list_id": "Approved"} for n in (1, 2)]   # older: updated first
    restart = {**_card(9, profile="claude"), "list_id": "In progress"}                    # its agent was stopped
    started, _ = _pass(monkeypatch, [*approved, restart])
    assert [c["id"] for c in started] == ["o/r#9"]
    assert "r#1  run waits (1/1 in flight)" in capsys.readouterr().out


def test_in_progress_cards_go_oldest_first_then_approved_ones(monkeypatch):
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}"}
    cs = [{**_card(n, profile="claude"), "list_id": col} for n, col in
          ((1, "Approved"), (5, "In progress"), (3, "In progress"))]
    monkeypatch.setattr(dispatch, "LIVE_RUN_CAP", 9)
    started = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append(c["id"]))
    for name in ("mirror_to_product", "ensure_services", "sweep_untracked"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(3, False, pull=False)
    assert started == ["o/r#3", "o/r#5", "o/r#1"]


def test_an_unreadable_card_is_skipped_and_alerted_once(monkeypatch):
    c = {**_card(1), "unreadable": "<!-- pl:metadata -->\n{broken\n"}
    started, _ = _pass(monkeypatch, [c])
    assert started == []
    a = alerts.get(f"card_unreadable:{c['id']}")
    assert a is not None
    assert a["severity"] == "warn"
    assert "broken pl metadata block" in a["title"]
    assert "fix the block on the board; pl card <id> shows it" in a["fix"]
    started, _ = _pass(monkeypatch, [c])
    assert started == []
    assert len([al for al in alerts.listing(all=True) if al["key"] == f"card_unreadable:{c['id']}"]) == 1
    assert alerts.get(f"card_unreadable:{c['id']}").get("resolved_at") is None



# ---------- Plan 26 slice 4: trust before start ----------

@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "untrusted_folder", REAL_GATE)
    monkeypatch.setattr(C, "WORK_DIR", tmp_path / "code" / "pl")
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    return tmp_path


def _claude_json(tmp, text):
    f = tmp / ".claude" / ".claude.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def _trusting(folder):
    return json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": True}}})


def _trust_alerts(all=False):
    return [a for a in alerts.listing(all=all) if a["key"].startswith("trust:")]


def test_an_untrusted_claude_account_starts_none_of_its_cards_and_opens_one_trust_alert(gate, monkeypatch, capsys):
    _claude_json(gate, json.dumps({"projects": {}}))
    cs = [_card(n, profile="claude") for n in (1, 2, 3)]
    started, writes = _pass(monkeypatch, cs)
    out = capsys.readouterr().out
    assert started == [] and writes == []
    lines = [ln for ln in out.splitlines() if "waits: account claude" in ln]
    assert len(lines) == 3
    assert all(str(C.WORK_DIR) in ln and f"o/r#{i}"[-3:] in ln for i, ln in zip((1, 2, 3), lines))
    (a,) = _trust_alerts()
    assert a["key"] == f"trust:claude:{C.WORK_DIR}"
    assert harnesses.trust_command(harnesses.get("claude"), "claude", str(C.WORK_DIR)) in a["title"]
    assert "Yes, I trust this folder" in a["title"]


@pytest.mark.parametrize("text", [None, "", "{not json"])
def test_a_missing_empty_or_broken_claude_json_counts_as_untrusted(gate, monkeypatch, text):
    if text is not None:
        _claude_json(gate, text)
    started, _ = _pass(monkeypatch, [_card(1, profile="claude")])
    assert started == []
    assert len(_trust_alerts()) == 1


def test_trusting_the_folder_starts_the_waiting_stage_and_closes_the_alert(gate, monkeypatch):
    f = _claude_json(gate, "{}")
    cs = [_card(1, profile="claude")]
    started, _ = _pass(monkeypatch, cs)
    assert started == []
    f.write_text(_trusting(C.WORK_DIR))
    started, _ = _pass(monkeypatch, cs)
    assert [c["id"] for c in started] == ["o/r#1"]
    assert alerts.get(f"trust:claude:{C.WORK_DIR}")["resolved_at"]


def test_a_trusted_parent_folder_lets_the_stage_start(gate, monkeypatch):
    _claude_json(gate, _trusting(C.WORK_DIR.parent))
    started, _ = _pass(monkeypatch, [_card(1, profile="claude")])
    assert [c["id"] for c in started] == ["o/r#1"]
    assert _trust_alerts() == []


def test_a_trusted_account_starts_as_today_with_no_trust_alert(gate, monkeypatch):
    _claude_json(gate, _trusting(C.WORK_DIR))
    started, _ = _pass(monkeypatch, [_card(1, profile="claude")])
    assert [c["id"] for c in started] == ["o/r#1"]
    assert _trust_alerts(all=True) == []


def test_other_harnesses_never_run_the_trust_check(gate, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the trust check ran")
    monkeypatch.setattr(harnesses, "claude_trusts", boom)
    monkeypatch.setattr(C, "STAGES", {"spec": {"account": "codex"}})
    started, _ = _pass(monkeypatch, [_card(1)])
    assert [c["id"] for c in started] == ["o/r#1"]


def test_dry_run_says_it_would_wait_and_opens_nothing(gate, monkeypatch, capsys):
    _claude_json(gate, "{}")
    started, _ = _pass(monkeypatch, [_card(1, profile="claude")], dry=True)
    assert started == []
    assert "would wait for trust" in capsys.readouterr().out
    assert alerts.listing(all=True) == []


def test_the_gate_never_writes_or_repeats_claude_json(gate, monkeypatch, capsys):
    f = _claude_json(gate, json.dumps({"projects": {}, "oauthAccount": "marker-218"}))
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    for _ in range(2):
        _pass(monkeypatch, [_card(1, profile="claude")])
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before
    assert "marker-218" not in capsys.readouterr().out
    assert "marker-218" not in json.dumps(alerts.listing(all=True))
