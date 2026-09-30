"""WP3: one dispatcher per profile, `pl profiles`, `pl accounts`, shared-resource warnings."""
import argparse
import multiprocessing
import os
import sys

import pytest

from pl import cli, commands, dispatch, profiles
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, name, text=""):
    d = home / f".pl-{name}"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(text)
    return d


def _hold(home, name, ready, release):
    """Child process: become the running dispatcher of profile `name` until told to stop."""
    os.environ["HOME"] = str(home)
    os.environ["PL_CONFIG_DIR"] = str(home / f".pl-{name}")
    from pl import config, dispatch as d
    config.load()
    lock = d.dispatch_lock()  # noqa: F841 - held open
    ready.set()
    release.wait(30)


def _row(name, running=True, accounts=None, board="b", tmux=None):
    return {"name": name, "dir": f"/x/.pl-{name}", "running": running, "holder": "pid 1", "tmux_session": tmux or f"pl-{name}",
            "tracker": f"mcp {board}", "board_id": board, "accounts": accounts or {}}


def test_second_dispatcher_for_same_profile_refused_other_profile_allowed(fake_home, capsys):
    _profile(fake_home, "a")
    _profile(fake_home, "b")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold, args=(fake_home, "a", ready, release))
    child.start()
    try:
        assert ready.wait(60), "child never took the lock"
        C.load("a")
        with pytest.raises(SystemExit) as e:
            dispatch.dispatch_lock()
        assert e.value.code == 0   # WP45: a restored or doubled window just ends, no error
        err = capsys.readouterr().err
        assert err.splitlines()[0] == f"a dispatcher is already running for this profile (pid {child.pid})"
        assert 'Profile "a" is already running. Starting it twice would run every agent twice and double the cost.' in err
        assert "dispatcher  pid " in err
        assert f"folder  {fake_home / '.pl-a'}" in err
        assert "open its console  pl --profile a watch" in err
        assert "stop it  Ctrl-C in tmux session pl-a, window dispatch" in err
        C.load("b")
        lock = dispatch.dispatch_lock()
        assert lock is not None
        lock.close()
    finally:
        release.set()
        child.join(30)


def test_list_profiles_running_vs_stopped_and_creates_no_lock(fake_home):
    _profile(fake_home, "busy", '[tracker]\nboard_id = "b-1"\n[accounts.x]\nconfig_dir = "~/hx"\n')
    _profile(fake_home, "idle")
    (fake_home / ".pl-nocfg").mkdir()          # no config.toml: not a profile
    C.load("busy")
    lock = dispatch.dispatch_lock()
    rows = {r["name"]: r for r in profiles.list_profiles()}
    assert set(rows) == {"busy", "idle"}
    assert rows["busy"]["running"] is True and rows["idle"]["running"] is False
    assert rows["busy"]["holder"].startswith("pid ") and rows["idle"]["holder"] == ""
    assert rows["busy"]["dir"] == str(fake_home / ".pl-busy")
    assert rows["busy"]["tmux_session"] == "pl-busy" and rows["busy"]["tracker"] == "? b-1"
    assert rows["busy"]["accounts"] == {"x": str(fake_home / "hx")}
    assert not (fake_home / ".pl-idle" / "state").exists()
    assert not (fake_home / ".claude-attention").exists()
    lock.close()
    assert {r["name"]: r["running"] for r in profiles.list_profiles()}["busy"] is False


def test_list_profiles_has_no_row_without_a_profile_folder(fake_home):
    assert profiles.list_profiles() == []
    C.load()
    lock = dispatch.dispatch_lock()
    assert profiles.list_profiles() == []
    lock.close()


def test_shared_warnings_shared_account_dir_only_when_both_running():
    a = _row("work", accounts={"x": "/h/.claude-acme2"}, board="b1")
    b = _row("personal", accounts={"y": "/h/.claude-acme2"}, board="b2")
    w = profiles.shared_warnings([a, b])
    assert len(w) == 1 and "work and personal both use /h/.claude-acme2" in w[0] and "usage limit" in w[0]
    assert profiles.shared_warnings([a, {**b, "running": False}]) == []
    assert profiles.shared_warnings([a, _row("other", accounts={"z": "/h/.claude-other"}, board="b3")]) == []


def test_shared_warnings_board_and_current_profile():
    a = _row("work", board="same")
    cur = _row("personal", board="same")
    w = profiles.shared_warnings([a], cur)
    assert len(w) == 1 and "work and personal" in w[0] and "same" in w[0]
    assert profiles.shared_warnings([a, {**cur, "running": False}]) == []          # the current row is judged as running
    assert len(profiles.shared_warnings([a, cur], cur)) == 1                        # not counted twice


def test_shared_warnings_tmux_session_clash():
    a = _row("work", tmux="pl-x", board="b1")
    b = _row("personal", tmux="pl-x", board="b2")
    w = profiles.shared_warnings([a, b])
    assert len(w) == 1 and "tmux session pl-x" in w[0]


def test_new_profile_refuses_existing_and_round_trips(fake_home, capsys):
    C.load()
    src = {k: getattr(C, k) for k in ("BOARD", "PROFILES", "SERVICES", "TRACKER", "INTAKE", "USER_EMAIL", "WORK_DIR")}
    profiles.new_profile("work")
    d = fake_home / ".pl-work"
    assert (d / "config.toml").is_file()
    assert "alias pl-work='PL_CONFIG_DIR=~/.pl-work pl'" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="exists"):
        profiles.new_profile("work")
    C.load("work")
    assert C.GATES["spec"] is True
    for k, v in src.items():
        assert getattr(C, k) == v, k
    assert C.TMUX_SESSION == "pl-work"
    with pytest.raises(SystemExit, match="bad profile name"):
        profiles.new_profile("Bad Name")
    assert not (fake_home / ".pl-Bad Name").exists()


def test_new_profile_leaves_plans_dir_unset_so_each_profile_gets_its_own(fake_home):
    profiles.new_profile("one")
    profiles.new_profile("two")
    assert "plans_dir" not in (fake_home / ".pl-one" / "config.toml").read_text()
    C.load("two")
    assert C.PLANS == fake_home / ".pl-two" / "plans"


def test_profiles_new_github_project_create_records_the_repo(fake_home, monkeypatch):
    from pl.trackers.github import GitHubProject
    monkeypatch.setattr(GitHubProject, "create_project", staticmethod(lambda o, t, c: {"number": "7", "url": "u", "status_field": "pl stage"}))
    profiles.cmd_new(["gp", "--github-project", "create", "--owner", "acme", "--repo", "acme/app"])
    C.load("gp")
    assert C.TRACKER["repo"] == "acme/app" and C.TRACKER["owner"] == "acme"


def test_new_profile_from_current_copies_the_loaded_profile(fake_home):
    _profile(fake_home, "base", '[tracker]\nboard_id = "b-base"\n[accounts.a]\nconfig_dir = "~/ha"\n')
    profiles.cmd_new(["copy", "--from-current"], "base")
    C.load("copy")
    assert C.BOARD == "b-base" and C.PROFILES == {"a": fake_home / "ha"} and C.GATES["spec"] is True


def test_new_profile_from_current_keeps_loops_turned_off(fake_home):
    _profile(fake_home, "base", '[tracker]\ntype = "github-project"\nowner = "acme"\nnumber = 3\nrepo = "acme/app"\n'
             '[accounts.a]\nconfig_dir = "~/ha"\n[code_host.labels]\nreview = "r"\nready = "ok"\n'
             '[loops.auto-review]\nenabled = false\n[loops.keep]\nprompt = "p"\n')
    C.load("base")
    profiles.cmd_new(["copy", "--from-current"], "base")
    C.load("copy")
    assert set(C.SERVICES) == {"keep"}


def test_profiles_new_runs_before_the_profile_exists(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pl", "profiles", "new", "fresh"])
    cli.main()
    assert (fake_home / ".pl-fresh" / "config.toml").is_file()
    assert "alias pl-fresh=" in capsys.readouterr().out
    assert not (fake_home / ".pl-fresh" / "state").exists()


def test_profiles_command_prints_table(fake_home, monkeypatch, capsys):
    _profile(fake_home, "idle", '[accounts.a]\nconfig_dir = "~/ha"\n')
    monkeypatch.setattr(sys, "argv", ["pl", "profiles"])
    cli.main()
    out = capsys.readouterr().out
    assert "idle" in out and "stopped" in out and "pl-idle" in out
    (fake_home / ".pl-idle" / "config.toml").unlink()
    cli.main()
    assert "pl profiles new" in capsys.readouterr().out


def test_accounts_is_the_old_profiles_and_reset_flag_moved(fake_home, monkeypatch, capsys):
    _profile(fake_home, "work", '[accounts.a]\nconfig_dir = "~/ha"\n')
    monkeypatch.setenv("PL_CONFIG_DIR", str(fake_home / ".pl-work"))
    C.load()
    commands.cmd_profiles(argparse.Namespace(reset=None))
    old = capsys.readouterr().out
    assert old
    monkeypatch.setattr(sys, "argv", ["pl", "accounts"])
    cli.main()
    assert capsys.readouterr().out == old
    monkeypatch.setattr(sys, "argv", ["pl", "accounts", "--reset", "all"])
    cli.main()
    assert "un-parked all" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["pl", "profiles", "--reset", "all"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2


def test_dispatch_prints_shared_warnings_and_carries_on(fake_home, monkeypatch, capsys):
    rows = [_row("work", accounts={"x": "/h/.claude-acme2"}), _row("personal", accounts={"y": "/h/.claude-acme2"})]
    monkeypatch.setattr(profiles, "list_profiles", lambda: rows)
    passes = []
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **k: passes.append(a))
    dispatch.cmd_dispatch(argparse.Namespace(dry_run=True, once=True, max_runs=1, max_prep=1, no_pull=True, interval=1))
    assert "share one subscription" in capsys.readouterr().out
    assert len(passes) == 1


FAKE_LEGACY = '''#!/usr/bin/env python3
"""a legacy pl"""
import os
from pathlib import Path

HOME = Path.home()
BOARD = os.environ.get("PL_BOARD_ID", "board-pipe-1")
PLANS = HOME / ".acme" / "plans"
ATTN = HOME / ".acme-attn"
PROFILES = {"main": HOME / ".claude-main", "spare": HOME / ".claude-spare"}
USER_EMAIL = os.environ.get("PL_USER_EMAIL", "dev@acme.test")
WORK_DIR = HOME / "acme"
ATTENTION = HOME / ".local" / "bin" / "acme-attention"
PRODUCT_BOARD = os.environ.get("PL_PRODUCT_BOARD_ID", "board-intake-2")
PRODUCT_INTAKE_COLUMNS = [c.strip() for c in os.environ.get("PL_PRODUCT_COLUMNS", "Triage,Backlog").split(",") if c.strip()]
PRODUCT_SKIP_TAGS = {"question", "hold"}
USER_UUID = os.environ.get("PL_USER_UUID", "user-uuid-3")
USER_NAME = os.environ.get("PL_USER_NAME", "Dev Person")
PROMPTS = {"spec": "/spec-skill {id}", "design": "/design-skill {id}",
           "plan": "/plan-skill {id}", "run": "/loop 5m /run-skill {id}"}


def card_url(item_id):
    return f"https://boards.acme.test/boards/{BOARD}?cardId={item_id}"


def curl_conf():
    cfg = json.loads(mcp.read_text())["mcpServers"]["brd"]["env"]


def pr_counts():
    r = subprocess.run(["gh", "search", "prs", "--owner", "AcmeOrg", "--state", "open", "--label", "ready-label",
                        "--assignee", "@me"])
    f = subprocess.run(["gh", "search", "prs", "--owner", "AcmeOrg", "--state", "open", "--label", "failed-label",
                        "--assignee", "@me"])
    pr["state"] = "rework" if "rework-label" in ls else ("merge" if "merge-label" in ls else "gate")
    head = ["(decide, then add the review-label label back)"]


def product_url(item_id):
    return f"https://boards.acme.test/boards/{PRODUCT_BOARD}?cardId={item_id}"


def pull_one(pc):
    repos = [t for t in tags if t in ("api", "web")]


SERVICES = {
    "triage": {"prompt": "/loop 30m /triage-skill", "profile": "spare"},
    "gate": {"prompt": "/loop 30m /gate-skill", "profile": "main"},
}

if __name__ == "__main__":
    raise SystemExit("never run")
'''
SECRET = "sentinel-api-key-51c2"


def _legacy_setup(home):
    snap = home / "legacy-pl"
    snap.write_text(FAKE_LEGACY)
    (home / "acme").mkdir()
    (home / "acme" / ".mcp.json").write_text(__import__("json").dumps({"mcpServers": {
        "other": {"command": "x"},
        "brd": {"type": "stdio", "command": "node", "args": ["/srv/mcp/dist/index.js"],
                "env": {"BRD_API_KEY": SECRET, "BRD_BASE_URL": "https://api.acme.test"}}}}))
    return snap


def test_from_legacy_writes_a_complete_mcp_profile_without_the_secret(fake_home, capsys):
    snap = _legacy_setup(fake_home)
    profiles.cmd_new(["work", "--from-legacy", "--legacy-script", str(snap)])
    text = (fake_home / ".pl-work" / "config.toml").read_text()
    assert SECRET not in text
    assert (os.stat(fake_home / ".pl-work" / "config.toml").st_mode & 0o777) == 0o600
    for gone in ("BRD_API_KEY", "BRD_BASE_URL", "api.acme.test", "/srv/mcp/dist/index.js", '"node"', "[tracker.server"):
        assert gone not in text, gone                            # pl names the server; the harness file holds it
    out = capsys.readouterr().out
    assert SECRET not in out and "export" not in out
    C.load("work")
    mcp_json = str(fake_home / "acme" / ".mcp.json")
    assert C.TRACKER["type"] == "mcp" and C.TRACKER["board_id"] == "board-pipe-1"
    assert (C.TRACKER["mcp_config"], C.TRACKER["server"]) == (mcp_json, "brd")
    assert not {"command", "args", "env", "url", "headers"} & (set(C.TRACKER) | set(C.INTAKE))
    assert C.TRACKER["card_url"] == "https://boards.acme.test/boards/{board_id}?cardId={item_id}"
    assert C.INTAKE["type"] == "mcp" and C.INTAKE["board_id"] == "board-intake-2"
    assert (C.INTAKE["mcp_config"], C.INTAKE["server"]) == (mcp_json, "brd")
    assert C.INTAKE["columns"] == ["Triage", "Backlog"] and sorted(C.INTAKE["skip_tags"]) == ["hold", "question"]
    assert C.INTAKE["repo_tags"] == ["api", "web"]
    tools = C.TRACKER["tools"]
    assert {k: v["tool"] for k, v in tools.items()} == {k: "manage_boards" for k in
                                                         ("columns", "cards", "card", "create", "update", "move", "delete", "ensure_column")}
    assert [tools[k]["args"]["action"] for k in ("columns", "cards", "card", "create", "update", "move", "delete", "ensure_column")] == [
        "list_list", "item_list", "item_get", "item_create", "item_update", "item_move", "item_delete", "list_create"]
    assert C.TRACKER["tools"] == C.INTAKE["tools"]
    assert (C.USER_NAME, C.USER_EMAIL, C.USER_UUID) == ("Dev Person", "dev@acme.test", "user-uuid-3")
    assert C.WORK_DIR == fake_home / "acme" and C.PLANS == fake_home / ".acme" / "plans"
    assert C.ATTENTION == fake_home / ".local" / "bin" / "acme-attention"
    assert C.PROFILES == {"main": fake_home / ".claude-main", "spare": fake_home / ".claude-spare"}
    assert C.CODE_HOST == {"owner": "AcmeOrg", "labels": {"review": "review-label", "ready": "ready-label",
                                                         "merge_ready": "merge-label", "rework": "rework-label",
                                                         "failed": "failed-label"}}
    assert C.PROMPTS == {"spec": "/spec-skill {id}", "design": "/design-skill {id}", "plan": "/plan-skill {id}",
                         "run": "/loop 5m /run-skill {id}"}
    assert C.SERVICES == {"triage": {"prompt": "/loop 30m /triage-skill", "profile": "spare"},
                          "gate": {"prompt": "/loop 30m /gate-skill", "profile": "main"}}
    assert C.GATES["spec"] is True and C.TMUX_SESSION == "pl-work"


def test_from_legacy_refuses_a_script_without_the_constants(fake_home):
    bad = fake_home / "wrapper"
    bad.write_text("#!/bin/sh\nexec uv run pl \"$@\"\n")
    with pytest.raises(SystemExit, match="BOARD"):
        profiles.cmd_new(["work", "--from-legacy", "--legacy-script", str(bad)])
    assert not (fake_home / ".pl-work").exists()


def test_shared_warnings_same_basename_in_two_folders_still_warns():
    a = {**_row("work", board="b1"), "dir": "/h/.pl-work"}
    cur = {**_row("work", board="b2"), "dir": "/h/elsewhere/.pl-work"}
    w = profiles.shared_warnings([a], cur)
    assert len(w) == 1 and "tmux session pl-work" in w[0]
    assert "/h/.pl-work" in w[0] and "/h/elsewhere/.pl-work" in w[0]
    assert profiles.shared_warnings([a, cur], cur) == w                             # the current folder is not counted twice


def _dispatch_args(**k):
    return argparse.Namespace(**{"dry_run": False, "once": True, "max_runs": 1, "max_prep": 1, "no_pull": True,
                                 "interval": 1, **k})


def test_a_second_dispatcher_in_this_process_exits_0_and_runs_no_pass(fake_home, monkeypatch, capsys):
    """WP45: whatever starts it (console, by hand, a restored tmux window), a second dispatcher starts nothing."""
    import fcntl
    _profile(fake_home, "a")
    C.load("a")
    C.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    held = open(C.LOCK_FILE, "a+")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    held.write("pid 4242 on h since now")
    held.flush()
    passes = []
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **k: passes.append(a))
    try:
        with pytest.raises(SystemExit) as e:
            dispatch.cmd_dispatch(_dispatch_args())
        assert e.value.code == 0 and passes == []
        assert "a dispatcher is already running for this profile (pid 4242)" in capsys.readouterr().err
    finally:
        held.close()


def test_the_dispatcher_says_it_started_with_its_pid(fake_home, monkeypatch, capsys):
    import json
    _profile(fake_home, "a")
    C.load("a")
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **k: None)
    dispatch.cmd_dispatch(_dispatch_args())
    assert f"dispatcher started for a (pid {os.getpid()})" in capsys.readouterr().out
    ev = [json.loads(x) for x in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["pid"]) for e in ev] == [("dispatcher_started", os.getpid())]
    assert dispatch.holder_pid() == os.getpid()
