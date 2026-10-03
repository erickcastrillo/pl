"""The Assistant: one live harness session in the profile's tmux window `assistant`. tmux, the session registry and
the board are fakes; HOME is a temporary folder."""
import argparse
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pl import accounts, agents, assistant, cli, dispatch, ideas
from pl import config as C

SID = "11111111-2222-3333-4444-555555555555"
GUIDE = Path(assistant.__file__).with_name("assistant.md")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "PL_ASSISTANT"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[accounts.acme]\nconfig_dir = "~/.claude-acme"\n'
                                   '[accounts.acme2]\nconfig_dir = "~/.claude-acme2"\n')
    C.load("t")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    monkeypatch.setattr(agents, "registry", lambda: {})
    assistant._INSTALLED.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class Tmux:
    """A fake tmux behind subprocess.run: sessions, windows by id, panes by id (pane -> (session, window id, name)).
    Records every argv."""

    def __init__(self, panes=None, screen="", cmd="claude"):
        self.calls, self.panes, self.screen, self.cmd, self.n = [], dict(panes or {}), screen, cmd, 9
        self.activity, self.clients = "0", "0"      # the window's last activity (epoch seconds), clients attached

    def __call__(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        out, rc = "", 0
        if argv[:1] != ["tmux"]:
            return subprocess.CompletedProcess(argv, 1, "", "not tmux")
        verb = argv[1]
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        if verb == "new-window":
            self.n += 1
            out = f"@{self.n}"
            self.panes[f"%{self.n}"] = (target.lstrip("=").rstrip(":"), out, argv[argv.index("-n") + 1])
        elif verb == "list-panes" and target.startswith("@"):
            out = " ".join(p for p, (_, w, _) in self.panes.items() if w == target)
        elif verb == "display-message":
            if target not in self.panes:
                rc = 1
            elif argv[-1] == "#{pane_current_command}":
                out = self.cmd
            elif argv[-1] == "#{session_name} #{window_id} #{window_name}":
                out = " ".join(self.panes[target])
            elif argv[-1] == "#{window_activity}":
                out = self.activity
            elif argv[-1] == "#{window_active_clients}":
                out = self.clients
            else:
                out = target
        elif verb == "capture-pane":
            out = self.screen
        elif verb == "kill-window":
            self.panes = {p: v for p, v in self.panes.items() if v[1] != target}
        return subprocess.CompletedProcess(argv, rc, out + "\n", "" if rc == 0 else "can't find pane")

    def verb(self, v):
        return [c for c in self.calls if c[1:2] == [v]]

    def typed(self):
        return [c for c in self.verb("send-keys")]


@pytest.fixture
def tmux(monkeypatch):
    t = Tmux()
    monkeypatch.setattr(subprocess, "run", t)
    return t


def _script(t):
    """The launch script whose path was typed into the new pane."""
    line = next(c[-1] for c in t.typed() if c[-1].startswith("sh "))
    return Path(line[3:].strip("'")).read_text()


def _events(kind=None):
    p = C.STATE_DIR / "events.jsonl"
    evs = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [e for e in evs if kind is None or e["kind"] == kind]


def _save_state(**kw):
    p = C.STATE_DIR / "assistant.json"
    p.write_text(json.dumps(kw))


# ---------- ensure: start, resume, reattach ----------

def test_a_first_ensure_starts_the_assistant_window_with_the_guide(tmux):
    status = assistant.ensure()
    nw = tmux.verb("new-window")
    assert len(nw) == 1
    argv = nw[0]
    assert argv[argv.index("-n") + 1] == "assistant" and argv[argv.index("-t") + 1] == "=pl-t:"
    assert f"PL_CONFIG_DIR={C.CONFIG_DIR}" in argv and "PL_ASSISTANT=1" in argv
    script = _script(tmux)
    assert "--session-id" in script and str(GUIDE) in script and "dangerously" not in script
    assert "exec claude --add-dir " in script and " --permission-mode manual " in script    # ask the person, whatever the account's default mode
    assert "CLAUDE_CONFIG_DIR=" in script and ".claude-acme" in script
    st = json.loads((C.STATE_DIR / "assistant.json").read_text())
    assert st["account"] == "acme" and st["window"] == "@10" and st["pane"] == "%10" and st["session_id"]
    assert status and "assistant" in status
    ev = _events("assistant_started")
    assert len(ev) == 1 and ev[0]["resumed"] is False and ev[0]["account"] == "acme"


@pytest.mark.parametrize("harness, unattended", [("claude", "auto"), ("codex", "never")])
def test_the_assistant_never_gets_the_unattended_flags(tmux, harness, unattended):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": harness}
    assistant.ensure()
    argv = _script(tmux).splitlines()[-1].split()
    assert unattended not in argv and "workspace-write" not in argv


def test_a_lost_window_resumes_the_saved_claude_conversation(tmux):
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    assistant.ensure()
    script = _script(tmux)
    assert "--resume" in script and SID in script and "--session-id" not in script
    assert json.loads((C.STATE_DIR / "assistant.json").read_text())["session_id"] == SID
    assert _events("assistant_started")[0]["resumed"] is True


def test_a_harness_without_resume_starts_fresh(tmux):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "codex"}
    _save_state(session_id=SID, account="acme", harness="codex", window="@3", pane="%3", started_at="x")
    assistant.ensure()
    script = _script(tmux)
    exec_line = next(x for x in script.splitlines() if x.startswith("exec "))
    assert exec_line.startswith("exec codex --ask-for-approval on-request --sandbox read-only ")
    assert "resume" not in exec_line and str(GUIDE) in exec_line


def test_a_harness_that_cannot_be_made_to_ask_is_refused(tmux, monkeypatch):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "antigravity"}
    with pytest.raises(SystemExit, match="ask"):
        assistant.ensure()
    from pl import harnesses
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "claude"}
    real = harnesses.get
    monkeypatch.setattr(harnesses, "get", lambda n: __import__("dataclasses").replace(
        real(n), interactive=["claude", "--permission-mode", "bypassPermissions", "{prompt}"]))
    with pytest.raises(SystemExit, match="ask"):
        assistant.ensure()
    assert tmux.verb("new-window") == []


def test_a_live_saved_pane_starts_nothing(tmux):
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    status = assistant.ensure()
    assert tmux.verb("new-window") == [] and tmux.typed() == []
    assert "running" in status


def test_a_pane_back_at_its_shell_says_how_to_start_over(tmux):
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    tmux.cmd = "zsh"
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    status = assistant.ensure()
    assert tmux.verb("new-window") == [] and "R starts a new conversation" in status


# ---------- send, screen ----------

def _live(tmux):
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")


def test_send_types_literal_text_then_enter_and_a_digit_alone(tmux):
    _live(tmux)
    assistant.send("hi\x03\n")
    assert tmux.typed() == [["tmux", "send-keys", "-t", "%3", "-l", "--", "hi "], ["tmux", "send-keys", "-t", "%3", "Enter"]]
    tmux.calls.clear()
    assistant.send("2")
    assert tmux.typed() == [["tmux", "send-keys", "-t", "%3", "-l", "--", "2"]]
    tmux.calls.clear()
    assistant.send("  ")
    assert tmux.typed() == []                       # never a bare Enter


def test_send_with_no_pane_refuses_without_tmux(tmux):
    with pytest.raises(SystemExit, match="not running"):
        assistant.send("hello")
    assert tmux.calls == []


def test_screen_masks_token_shaped_text(tmux):
    _live(tmux)
    tmux.screen = "ok\nkey ghp_" + "a" * 30 + "\n"
    lines = assistant.screen(10)
    assert lines[0] == "ok" and "ghp_" not in lines[1] and "***" in lines[1]


def test_a_pane_in_another_session_is_not_the_assistant(tmux):
    tmux.panes["%3"] = ("other", "@3", "assistant")      # a tmux restart reused the ids in someone else's session
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    with pytest.raises(SystemExit, match="not running"):
        assistant.send("hello")
    assert assistant.screen(10) == [] and tmux.verb("capture-pane") == []
    assistant.reset()
    assert tmux.verb("kill-window") == [] and "%3" in tmux.panes
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    assistant.ensure()
    assert len(tmux.verb("new-window")) == 1


# ---------- account ----------

def test_account_skips_a_parked_one_and_honours_the_setting(monkeypatch):
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})
    assert assistant.account() == "acme2"
    C.ASSISTANT = {"account": "acme"}
    assert assistant.account() == "acme"
    C.ASSISTANT = {"account": "nope"}
    assert assistant.account() == "acme2"


# ---------- reset ----------

def test_reset_kills_the_window_and_forgets_the_session(tmux):
    _live(tmux)
    assistant.reset()
    assert ["tmux", "kill-window", "-t", "@3"] in tmux.calls
    st = json.loads((C.STATE_DIR / "assistant.json").read_text())
    assert not st.get("session_id") and not st.get("pane")
    assert len(_events("assistant_reset")) == 1


# ---------- state file ----------

def test_the_state_file_is_private_and_holds_only_its_keys(tmux):
    assistant.ensure()
    p = C.STATE_DIR / "assistant.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert set(json.loads(p.read_text())) <= {"session_id", "account", "harness", "window", "pane", "started_at", "mode", "build"}


# ---------- CLI: assistant_action and pl assistant log ----------

def _cli(monkeypatch, *argv):
    monkeypatch.setenv("PL_CONFIG_DIR", str(C.CONFIG_DIR))
    monkeypatch.setattr(sys, "argv", ["pl", *argv])
    cli.main()


def test_a_pipeline_command_from_the_assistant_leaves_an_event(monkeypatch):
    monkeypatch.setattr(cli, "cmd_approve", lambda a: None)
    _cli(monkeypatch, "approve", "card0001aaaa")
    assert _events("assistant_action") == []
    monkeypatch.setenv("PL_ASSISTANT", "1")
    _cli(monkeypatch, "approve", "card0001aaaa")
    ev = _events("assistant_action")
    assert len(ev) == 1 and ev[0]["command"] == "approve" and ev[0]["card"] == "card0001aaaa"
    monkeypatch.setattr(cli, "cmd_pull", lambda a: None)
    monkeypatch.setattr(cli.alerts, "cmd_alerts", lambda a: None)
    _cli(monkeypatch, "pull")
    _cli(monkeypatch, "alerts")                          # a read: no event
    _cli(monkeypatch, "alerts", "--ack", "k1")
    assert [e["command"] for e in _events("assistant_action")] == ["approve", "pull"]


def test_a_loop_named_assistant_is_rejected():
    import tomlkit
    assert any("assistant" in e for e in C.validate(tomlkit.parse('[loops.assistant]\nprompt = "/loop 5m /x"\n')))


def test_pl_assistant_log_emits_a_masked_short_event(monkeypatch, capsys):
    _cli(monkeypatch, "assistant", "log", "installed a plugin, token ghp_" + "b" * 30 + " " + "x" * 400)
    ev = _events("assistant_log")
    assert len(ev) == 1 and "ghp_" not in ev[0]["message"] and len(ev[0]["message"]) <= 200
    assert ev[0]["message"].startswith("installed a plugin")


# ---------- the guide ----------

def test_every_subcommand_is_in_the_guide():
    src = Path(cli.__file__).read_text()
    names = set(re.findall(r'sub\.add_parser\("([\w-]+)"', src))
    guide = GUIDE.read_text()
    assert names and [n for n in names if f"pl {n}" not in guide] == []


# ---------- the assistant window is left alone by the sweepers ----------

def test_sweep_untracked_leaves_the_assistant_window_alone(tmux, monkeypatch):
    def run(argv, *a, **kw):
        tmux.calls.append([str(x) for x in argv])
        return subprocess.CompletedProcess(argv, 0, "@3 %3 0 assistant\n@4 %4 0 spec-old\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    dispatch.sweep_untracked([], {}, False)
    kills = [c for c in tmux.calls if c[1:2] == ["kill-window"]]
    assert kills == [["tmux", "kill-window", "-t", "@4"]]


# ---------- idea mode: same storage and approval path as the Ideas tab ----------

def test_set_mode_tells_the_harness_and_is_saved(tmux):
    _live(tmux)
    assistant.set_mode("idea")
    typed = [c[-1] for c in tmux.typed() if "-l" in c]
    assert len(typed) == 1 and typed[0].startswith("[pl mode idea]")
    assert json.loads((C.STATE_DIR / "assistant.json").read_text())["mode"] == "idea"
    assert _events("assistant_mode")[0]["mode"] == "idea"
    with pytest.raises(SystemExit):
        assistant.set_mode("rm")


def test_pl_assistant_idea_save_stores_a_draft_the_ideas_tab_lists(monkeypatch, capsys):
    brief = {"problem": "slow page", "who": "admins", "outcome": "fast page", "in_scope": ["cache"],
             "out_of_scope": [], "repos": ["web"], "open_questions": []}
    _cli(monkeypatch, "assistant", "idea", "save", "--title", "Faster page", "--brief", json.dumps(brief))
    out = capsys.readouterr().out
    listed = ideas.list_ideas()
    assert len(listed) == 1 and listed[0]["title"] == "Faster page" and listed[0]["brief"]["problem"] == "slow page"
    assert ideas.is_clear(listed[0]) and listed[0]["id"] in out and "## Problem" in out
    iid = listed[0]["id"]
    _cli(monkeypatch, "assistant", "idea", "save", "--id", iid, "--brief", json.dumps({"open_questions": ["which page?"]}))
    again = ideas.load(iid)
    assert again["title"] == "Faster page" and again["brief"]["problem"] == "slow page" and not ideas.is_clear(again)


def test_pl_assistant_idea_file_only_marks_the_draft_ready(monkeypatch, capsys):
    def no_board(*a, **k):
        raise AssertionError("the CLI wrote the board")
    monkeypatch.setattr(ideas, "approve", no_board)
    monkeypatch.setattr(ideas.trackers, "get", no_board)
    idea = ideas.save({**ideas.new_idea("x", title="T"), "asked": True})
    monkeypatch.setenv("PL_ASSISTANT", "1")
    _cli(monkeypatch, "assistant", "idea", "file", idea["id"])
    disk = ideas.load(idea["id"])
    assert disk["status"] == "interviewing" and disk["ready_to_file"] is True and "ctrl+f" in capsys.readouterr().out
    assert assistant.ready_idea()["id"] == idea["id"]
    _cli(monkeypatch, "assistant", "idea", "save", "--id", idea["id"], "--brief", "{}")   # changed: ask again
    assert not ideas.load(idea["id"]).get("ready_to_file") and assistant.ready_idea() is None


def test_save_never_reopens_a_closed_idea_or_overwrites_a_newer_draft():
    idea = ideas.save({**ideas.new_idea("x", title="T"), "asked": True})
    stale = dict(idea)
    ideas.save({**ideas.load(idea["id"]), "title": "newer"})
    with pytest.raises(SystemExit, match="newer"):
        ideas.save({**stale, "title": "older"})
    assert ideas.load(idea["id"])["title"] == "newer"
    for closed in ("approved", "discarded"):
        ideas.save({**ideas.load(idea["id"]), "status": closed})
        with pytest.raises(SystemExit, match=closed):
            ideas.save({**ideas.load(idea["id"]), "status": "interviewing"})
        assert ideas.load(idea["id"])["status"] == closed
        with pytest.raises(SystemExit, match=closed):
            _cli_ns(id=idea["id"])
    (ideas._dir() / f"{idea['id']}.json").write_text("[1]")
    with pytest.raises(SystemExit, match="not an object"):
        ideas.load(idea["id"])


def _cli_ns(**kw):
    ns = {"assistant_cmd": "idea", "idea_cmd": "save", "id": None, "title": None, "brief": "{}", **kw}
    return assistant.cmd_assistant(argparse.Namespace(**ns))


def test_cmd_assistant_idea_bad_brief_changes_nothing(monkeypatch):
    with pytest.raises(SystemExit, match="JSON"):
        assistant.cmd_assistant(argparse.Namespace(assistant_cmd="idea", idea_cmd="save", id=None, title="T", brief="{nope"))
    assert ideas.list_ideas() == []


# ---------- round 2: ask rules, allow-rule warning, template refusal, file locks ----------

def _exec_argv(t):
    import shlex
    return shlex.split(next(x for x in _script(t).splitlines() if x.startswith("exec "))[5:])


def _perms(t):
    argv = _exec_argv(t)
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert set(settings) == {"permissions"}
    return settings["permissions"]


def _hits(rules, cmd):
    """The Bash rules that match cmd, with Claude Code's `*` wildcard (any text, spaces included)."""
    return [r for r in rules if r.startswith("Bash(") and re.fullmatch(
        re.escape(r[5:-1]).replace(r"\*", ".*"), cmd)]


def test_claude_gets_ask_rules_that_beat_the_accounts_allow_rules(tmux):
    assistant.ensure()
    p = _perms(tmux)
    assert set(p) == {"allow", "ask", "deny"}
    for cmd in ("pl approve x", "pl --profile other approve x", "pl move-agent c a", "pl retry all", "pl idea hi",
                "pl assistant idea file 1", "pl manager stop", "pl manager --all stop", "pl manager restart t",
                "pl card x --delete", "gh pr merge 1", "gh api /user", "gh issue close 1", "gh pr create",
                "git push", "git push origin main", "git -C x push", "git diff --output=x", "git log --output x"):
        assert _hits(p["ask"], cmd), cmd
    assert _exec_argv(tmux)[0] == "claude" and "--permission-mode" in _exec_argv(tmux)
    assert _exec_argv(tmux)[_exec_argv(tmux).index("--permission-mode") + 1] == "manual"


def test_reads_and_read_only_commands_run_without_asking(tmux):
    assistant.ensure()
    p = _perms(tmux)
    assert {"Read", "Glob", "Grep", "LS", "NotebookRead"} <= set(p["allow"])
    for cmd in ("pl list --all", "pl card abc", "pl alerts --all", "pl usage --by card", "pl standup --since 24h",
                "pl manager status", "pl accounts", "pl whatsnew", "git status", "git log -5", "git diff main",
                "gh pr view 12", "gh pr list --state open"):
        assert _hits(p["allow"], cmd) and not _hits(p["ask"], cmd) and not _hits(p["deny"], cmd), cmd


def test_writes_behind_a_read_only_command_still_ask(tmux):
    assistant.ensure()
    p = _perms(tmux)
    for cmd in ("pl alerts --ack k1", "pl alerts --ack=k1", "pl accounts --reset all", "pl accounts --res acme"):
        assert _hits(p["ask"], cmd), cmd
    assert "Bash(pl alerts *--ac*)" in p["ask"] and "Bash(pl accounts *--r*)" in p["ask"]
    assert not [r for r in p["allow"] if "--ack" in r or "--reset" in r]


def test_credentials_are_denied(tmux):
    assistant.ensure()
    deny = _perms(tmux)["deny"]
    for rule in ("Read(**/.credentials*)", "Read(**/auth.json)", "Read(**/.env*)", "Read(**/*.pem)",
                 "Read(**/id_rsa*)", "Read(**/.netrc)", "Read(**/hosts.yml)", "Bash(security *)"):
        assert rule in deny, rule
    assert _hits(deny, "security find-generic-password -s x")


def test_every_pl_command_is_read_only_or_asks(tmux):
    src = Path(cli.__file__).read_text()
    names = set(re.findall(r'sub\.add_parser\("([\w-]+)"', src)) | {"setup", "whatsnew", "manager", "profiles"}
    assistant.ensure()
    p = _perms(tmux)
    free = [n for n in names if not any(_hits(p[k], c) for k in ("ask", "allow") for c in (f"pl {n}", f"pl {n} x", f"pl {n} -x"))]
    assert names and free == []


def _add_dirs(argv):
    i = argv.index("--add-dir")
    out = []
    for a in argv[i + 1:]:
        if a.startswith("-"):
            break
        out.append(a)
    return out


def test_the_assistant_starts_where_the_code_lives_and_reads_its_own_folders(tmux, fake_home):
    code = fake_home / "code"
    code.mkdir()
    C.WORK_DIR = code
    assistant.ensure()
    nw = tmux.verb("new-window")[0]
    assert nw[nw.index("-c") + 1] == str(code)
    dirs = _add_dirs(_exec_argv(tmux))
    assert dirs == [str(C.PROFILES["acme"]), str(C.CONFIG_DIR), str(GUIDE.parent)]
    assert str(fake_home) not in dirs and "/" not in dirs


def test_a_missing_work_dir_starts_in_the_profile_folder(tmux, fake_home):
    C.WORK_DIR = fake_home / "gone"
    assistant.ensure()
    nw = tmux.verb("new-window")[0]
    assert nw[nw.index("-c") + 1] == str(C.CONFIG_DIR)


def test_no_add_dir_points_at_home_or_above_it(tmux, fake_home, monkeypatch):
    monkeypatch.setitem(C.PROFILES, "acme", fake_home)
    assistant.ensure()
    dirs = _add_dirs(_exec_argv(tmux))
    assert dirs and str(fake_home) not in dirs and "/" not in dirs
    assert all(not fake_home.is_relative_to(Path(d)) for d in dirs)


def test_a_template_with_its_own_tool_or_settings_flags_is_refused(tmux, monkeypatch):
    from pl import harnesses
    real = harnesses.get
    for flag in ("--allowedTools", "--allowed-tools", "--settings"):
        monkeypatch.setattr(harnesses, "get", lambda n, f=flag: __import__("dataclasses").replace(
            real(n), interactive=["claude", f, "x", "{prompt}"]))
        with pytest.raises(SystemExit, match="ask"):
            assistant.ensure()
    assert tmux.verb("new-window") == []


def _write(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


def test_allow_rules_that_cover_pl_gh_or_git_push_give_one_warning_line(tmux, fake_home):
    assert assistant.ensure() and assistant.warning() is None          # no settings files: nothing to say
    _write(C.PROFILES["acme"] / "settings.json", {"permissions": {"allow": ["Bash(pl:*)", "Read"]},
                                                 "env": {"TOKEN": "s3cretvalue"}})
    _write(C.WORK_DIR / ".claude" / "settings.local.json",
           {"permissions": {"allow": ["Bash(gh pr *)", "Bash(npm test)"], "deny": ["Bash(rm *)"]}})
    _write(C.WORK_DIR / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(git push origin main)"]}})
    w = assistant.warning()
    assert w and "\n" not in w
    for rule in ("Bash(pl:*)", "Bash(gh pr *)", "Bash(git push origin main)"):
        assert rule in w
    assert "npm" not in w and "Read" not in w and "s3cret" not in w and "rm *" not in w


def test_codex_gets_a_warning_that_its_own_rules_may_approve(tmux):
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "codex"}
    assistant.ensure()
    assert "codex" in (assistant.warning() or "")


def _hold(path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def test_two_consoles_never_start_two_windows(tmux):
    import threading
    held = _hold(C.STATE_DIR / "assistant.lock")        # another console is inside ensure()
    th = threading.Thread(target=assistant.ensure)
    th.start()
    th.join(0.5)
    assert th.is_alive() and tmux.verb("new-window") == []
    held.close()
    th.join(5)
    assistant.ensure()                                  # the second console now sees the running window
    assert len(tmux.verb("new-window")) == 1


def test_an_idea_save_waits_for_the_ideas_lock():
    import threading
    idea = ideas.save({**ideas.new_idea("x", title="T"), "asked": True})
    held = _hold(C.STATE_DIR / "ideas.save.lock")
    th = threading.Thread(target=ideas.save, args=({**idea, "title": "later"},))
    th.start()
    th.join(0.5)
    assert th.is_alive() and ideas.load(idea["id"])["title"] == "T"
    held.close()
    th.join(5)
    assert ideas.load(idea["id"])["title"] == "later"


def test_bare_pl_assistant_prints_how_to_use_it(monkeypatch, capsys):
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x")
    _cli(monkeypatch, "assistant")
    out = capsys.readouterr().out
    assert SID not in out and "log" in out


def test_the_assistant_never_loads_the_skills_library_plugin(tmux, fake_home):
    lib = fake_home / ".local/share/pl/skills/alpha"
    lib.mkdir(parents=True)
    (lib / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\n")
    assistant.ensure()
    assert "--plugin-dir" not in _script(tmux)


# ---------- restart on a pl update ----------

def _updated(monkeypatch, tmux, status="idle", build="old"):
    from pl import update
    monkeypatch.setattr(update, "build_id", lambda fresh=False: "new")
    monkeypatch.setattr(agents, "registry", lambda: {SID: {"sessionId": SID, "status": status}})
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", started_at="x",
                mode="idea", build=build)


def test_a_new_build_restarts_an_idle_assistant_with_resume(tmux, monkeypatch):
    _updated(monkeypatch, tmux)
    assert assistant.restart_if_updated() is True
    assert tmux.verb("kill-window")[0][-1] == "@3" and len(tmux.verb("new-window")) == 1
    script = _script(tmux)
    assert f"--resume {SID}" in script and " --permission-mode manual " in script
    st = assistant.load()
    assert st["build"] == "new" and st["session_id"] == SID and st["mode"] == "idea" and st["updated_at"]
    tmux.calls.clear()
    assert assistant.restart_if_updated() is False and tmux.verb("new-window") == []   # once per build


@pytest.mark.parametrize("status, lines", [("busy", ""), ("idle", "Do you want to proceed?\n❯ 1. Yes\n  2. No")])
def test_no_restart_mid_turn_or_at_a_prompt(tmux, monkeypatch, status, lines):
    _updated(monkeypatch, tmux, status=status)
    tmux.screen = lines                                   # read again inside the lock, never passed in
    assert assistant.restart_if_updated() is False
    assert tmux.verb("kill-window") == [] and tmux.verb("new-window") == []


def test_a_console_on_an_older_build_than_installed_restarts_nothing(tmux, monkeypatch):
    from pl import update
    _updated(monkeypatch, tmux)
    monkeypatch.setattr(update, "build_id", lambda fresh=False: "newer" if fresh else "new")
    assert assistant.restart_if_updated() is False and tmux.verb("kill-window") == []


def test_a_started_assistant_remembers_its_build(tmux, monkeypatch):
    from pl import update
    monkeypatch.setattr(update, "build_id", lambda fresh=False: "b1")
    assistant.ensure()
    assert assistant.load()["build"] == "b1"


def test_menu_reads_a_numbered_menu_off_the_screen():
    m = assistant.menu(["│ Bash command                     │", "│   pl approve 4a000001          │",
                        "│ Do you want to proceed?        │", "│ ❯ 1. Yes                       │", "│   2. No                        │"])
    assert m["options"] == [("1", "Yes"), ("2", "No")]
    assert "pl approve 4a000001" in m["title"] and "Do you want to proceed?" in m["title"]
    assert assistant.menu(["1. just a list item in a reply"]) is None


def test_a_restart_never_starts_a_fresh_conversation(tmux, monkeypatch):
    _updated(monkeypatch, tmux)
    monkeypatch.setattr(assistant, "_idle", lambda st: True)                  # only the account and resume checks refuse
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})      # account() would now pick acme2
    assert assistant.restart_if_updated() is False
    C.ACCOUNTS["acme"] = {**C.ACCOUNTS["acme"], "harness": "codex"}           # codex: no resume template
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    _save_state(session_id=SID, account="acme", harness="codex", window="@3", pane="%3", started_at="x", build="old")
    assert assistant.restart_if_updated() is False
    assert tmux.verb("kill-window") == [] and tmux.verb("new-window") == []


def test_no_restart_when_the_pane_is_not_the_assistants(tmux, monkeypatch):
    _updated(monkeypatch, tmux)
    tmux.panes["%3"] = ("pl-t", "@3", "other")
    assert assistant.restart_if_updated() is False and tmux.verb("kill-window") == []


def test_no_restart_while_someone_watches_or_the_chat_moved_lately(tmux, monkeypatch, fake_home):
    import time
    _updated(monkeypatch, tmux)
    tmux.clients = "1"                                    # the person has the window open
    assert assistant.restart_if_updated() is False
    tmux.clients, tmux.activity = "0", str(int(time.time()) - 10)   # the screen moved 10 s ago
    assert assistant.restart_if_updated() is False
    tmux.activity = "0"
    t = fake_home / ".claude-acme" / "projects" / "-w" / f"{SID}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")                                 # the transcript was written just now
    assert assistant.restart_if_updated() is False
    tmux.activity = "x"                                   # tmux gave no time: never restart
    old = time.time() - 600
    __import__("os").utime(t, (old, old))
    assert assistant.restart_if_updated() is False
    assert tmux.verb("kill-window") == []
    tmux.activity = "0"
    assert assistant.restart_if_updated() is True        # quiet 300 s, nobody attached


def test_the_saved_build_is_compared_before_locking_and_the_installed_one_is_cached(tmux, monkeypatch):
    from pl import update
    calls = []
    monkeypatch.setattr(update, "build_id", lambda fresh=False: calls.append(fresh) or "new")
    monkeypatch.setattr(assistant, "LOCK_WAIT", 0.1)
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", build="new")
    held = _hold(C.STATE_DIR / "assistant.lock")
    try:
        assert assistant.restart_if_updated() is False   # the same build: no lock wait, no installed-build read
        assert calls.count(True) == 0
        _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", build="old")
        assert assistant.restart_if_updated() is False   # the lock is busy: give up, the next pass tries again
        assert assistant.restart_if_updated() is False
    finally:
        held.close()
    assert calls.count(True) == 1                         # the installed build is read once per ~10 s
    assert tmux.verb("kill-window") == []


def test_answer_types_the_digit_only_while_the_same_question_shows(tmux):
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3")
    tmux.screen = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
    opts = assistant.menu(assistant.screen(15))["options"]
    assert assistant.answer("2", opts) is True
    assert tmux.typed()[-1] == ["tmux", "send-keys", "-t", "%3", "-l", "--", "2"]
    tmux.calls.clear()
    tmux.screen = "Which board?\n❯ 1. Product\n  2. Engineering"     # a new question took its place
    assert assistant.answer("2", opts) is False and tmux.typed() == []
    tmux.screen = "all done"
    assert assistant.answer("2", opts) is False and tmux.typed() == []


def test_a_reply_ending_in_a_numbered_list_is_not_a_menu():
    box = ["1. Run pl list", "2. Approve the plan", "╭──────────────────╮", "│ >                │", "╰──────────────────╯"]
    assert assistant.menu(box) is None
    assert assistant.menu(["1. Run pl list", "2. Approve the plan"]) is None              # no selection marker
    assert assistant.menu(["Which?", "❯ 1. Product", "  2. Engineering", "│ >     │"]) is None   # input box below


def test_send_waits_for_the_lock_only_so_long(tmux, monkeypatch):
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3")
    monkeypatch.setattr(assistant, "LOCK_WAIT", 0.2)
    held = _hold(C.STATE_DIR / "assistant.lock")
    try:
        with pytest.raises(SystemExit, match="busy"):
            assistant.send("hello")
    finally:
        held.close()
    assert tmux.typed() == []


def test_a_tmux_call_that_hangs_times_out_and_is_not_a_dead_assistant(monkeypatch):
    seen = []

    def hang(argv, *a, **kw):
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 0)
    monkeypatch.setattr(subprocess, "run", hang)
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3")
    with pytest.raises(SystemExit, match="timed out"):
        assistant.pane()
    with pytest.raises(SystemExit, match="timed out"):
        assistant.ensure()                                # never a second window because tmux was slow
    assert seen and all(t and t <= 5 for t in seen)


def test_saves_keep_fields_this_build_does_not_know(tmux):
    _save_state(session_id=SID, account="acme", harness="claude", window="@3", pane="%3", future="kept")
    tmux.panes["%3"] = ("pl-t", "@3", "assistant")
    assistant.set_mode("idea")
    d = json.loads((C.STATE_DIR / "assistant.json").read_text())
    assert d["future"] == "kept" and d["mode"] == "idea"


def test_after_clear_the_live_session_is_the_one_the_pane_runs(monkeypatch):
    new = "99999999-8888-7777-6666-555555555555"
    assistant._save({"session_id": SID, "account": "acme", "harness": "claude", "window": "@7", "pane": "%7"})
    rec = {"sessionId": new, "status": "idle", "tmux": f"{C.TMUX_SESSION}:@7.%7"}
    monkeypatch.setattr(agents, "registry", lambda: {new: rec})     # /clear: the old id is gone from the registry
    assert assistant.live_session() == new and assistant._status(assistant.load()) == "idle"
    monkeypatch.setattr(assistant, "pane", lambda st=None: "%7")
    assert assistant.sync_session() is True and assistant.load()["session_id"] == new
    assert assistant.sync_session() is False                      # nothing left to sync
    monkeypatch.setattr(agents, "registry", lambda: {})
    assert assistant.live_session() == new                          # no registry entry: the saved id
