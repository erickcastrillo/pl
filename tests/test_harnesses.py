"""WP7: agents run under Claude Code, Codex or Antigravity; every command is built from quoted template tokens."""
import re
import shlex
import subprocess
import time
import types
from datetime import datetime

import pytest

from pl import accounts, agents, dispatch, harnesses
from pl import config as C
from pl.util import now_iso

SID = "11111111-2222-3333-4444-555555555555"


def _accounts(home):
    """The two-account setup these tests were written against (no profile carries defaults since WP14)."""
    C.PROFILES = {"acme": home / ".claude-acme", "acme2": home / ".claude-acme2"}
    C.ACCOUNTS = {n: {"harness": "claude", "config_dir": str(d)} for n, d in C.PROFILES.items()}
    C.PROMPTS = {"spec": "/spec-writer {id}", "design": "/ui-designer {id}",
                 "plan": "/plan-writer {id}", "run": "/loop 5m /run-plan {id}"}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    _accounts(tmp_path)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


typed = []   # the raw text every send-keys -l typed into a pane
REAL_RUN = subprocess.run


def _script_cmd(payload):
    """The launch script behind a typed `sh <path>`, folded back into the one-line command it runs."""
    parts = shlex.split(payload)
    assert parts[0] == "sh" and len(parts) == 2, payload
    lines = open(parts[1]).read().splitlines()
    env = [line[len("export "):] for line in lines if line.startswith("export ")]
    run = [line[len("exec "):] for line in lines if line.startswith("exec ")]
    assert len(run) == 1, lines
    return " ".join(env + run)


@pytest.fixture
def fake_tmux(monkeypatch):
    """Record every tmux call and card update start_worker/ensure_services make; nothing real runs."""
    sent, updates = [], []   # sent: the command each launch script runs, as one "VAR=val argv..." line
    typed.clear()

    def tmux(*args, check=True):
        if args[0] == "send-keys" and "-l" in args:
            typed.append(args[-1])
            sent.append(_script_cmd(args[-1]))
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    def run(argv, **kw):
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: updates.append((cid, f)))
    monkeypatch.setattr(dispatch.uuid, "uuid4", lambda: SID)
    return sent, updates


def _profile(home, text):
    d = home / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text(text)
    C.load(config_dir=str(d))


def test_send_keys_string_returns_the_exact_hostile_tokens(fake_home, fake_tmux):
    sent, _ = fake_tmux
    C.PROFILES = {"acme": fake_home / "cfg dir"}
    c = {"id": "x'; rm -rf ~ #", "title": "t", "metadata": {"profile": "acme", "spec_slug": "$(id)"}}
    dispatch.start_worker(c, "spec", 1, False)
    assert len(sent) == 1
    assert shlex.split(sent[0]) == [f"CLAUDE_CONFIG_DIR={fake_home / 'cfg dir'}", "claude", "--permission-mode", "auto",
                                    "--session-id", SID,
                                    "--name", "spec:$(id)", "/spec-writer x'; rm -rf ~ #"]


def test_harness_for_honours_stage_over_card_account_default_claude(fake_home):
    _profile(fake_home, '[accounts.a]\nconfig_dir = "~/ca"\n[accounts.b]\nharness = "codex"\nconfig_dir = "~/cb"\n'
                        '[stages.run]\naccount = "b"\n[harnesses.codex]\nbin = "codex-beta"\n')
    card = {"metadata": {"profile": "a"}}
    h, acct = harnesses.harness_for("run", card)
    assert (h.name, acct, h.bin) == ("codex", "b", "codex-beta")
    h, acct = harnesses.harness_for("spec", card)
    assert (h.name, acct) == ("claude", "a")
    h, acct = harnesses.harness_for("spec", {"metadata": {}})
    assert (h.name, acct) == ("claude", "a")        # no card account: the first one listed
    C.load()
    with pytest.raises(SystemExit, match=r"\[accounts.<name>\]"):
        harnesses.harness_for("spec", {"metadata": {}})


def test_codex_liveness_by_pane_command(fake_home, monkeypatch):
    cmd = {"now": "node"}
    monkeypatch.setattr(agents, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=cmd["now"] + "\n"))
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 200))
    w = {"harness": "codex", "pane": "%3", "session_id": "s", "started_at": old}
    assert agents.worker_status(w, {}) == ("alive", "s")
    cmd["now"] = "zsh"
    assert agents.worker_status(w, {})[0] == "dead"
    assert agents.worker_status({**w, "started_at": now_iso()}, {})[0] == "starting"
    # the Claude session registry is read only from accounts whose harness keeps one
    cx = fake_home / "codexhome" / "sessions"
    cx.mkdir(parents=True)
    (cx / "x.json").write_text('{"sessionId": "leak", "pid": 1}')
    C.PROFILES, C.ACCOUNTS = {"cx": cx.parent}, {"cx": {"harness": "codex"}}
    assert agents.registry() == {}


def test_limit_detection_uses_the_cards_own_harness_patterns(fake_home, monkeypatch):
    claude_screen = "Claude usage limit reached · resets 3pm"
    codex_screen = "■ You've reached your usage limit. Increase your limits to continue using codex."
    screen = {"now": claude_screen}
    monkeypatch.setattr(accounts, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=screen["now"]))
    claude, codex = harnesses.get("claude"), harnesses.get("codex")
    assert accounts.screen_hit_limit("%1", claude) == claude_screen
    assert accounts.screen_hit_limit("%1", codex) is None
    screen["now"] = codex_screen
    assert accounts.screen_hit_limit("%1", codex) == codex_screen
    assert accounts.screen_hit_limit("%1", claude) is None
    assert accounts.mark_exhausted("cx", codex_screen)  # a screen the Claude regex does not match still parks
    screen["now"] = "HTTP 429: rate limit exceeded, try again in 5 seconds"   # ordinary output, not Codex's limit
    assert accounts.screen_hit_limit("%1", codex) is None


def test_claude_builtin_reproduces_the_legacy_commands(fake_home, fake_tmux):
    sent, updates = fake_tmux
    acme = fake_home / ".claude-acme"
    c = {"id": "abc", "title": "Fix Thing", "metadata": {"profile": "acme", "pipeline_mode": "auto"}}
    dispatch.start_worker(c, "spec", 1, False)
    legacy = f"CLAUDE_CONFIG_DIR={acme} claude --permission-mode auto --session-id {SID} --name 'spec:fix-thing' '/spec-writer abc'"
    assert shlex.split(sent[0]) == shlex.split(legacy)
    assert updates[0][1]["metadata"]["worker"]["harness"] == "claude"
    # service loops: same command as the snapshot's ensure_services
    C.SERVICES = {"merge-check": {"prompt": "/loop 30m /merge-check", "profile": "acme"}}
    sent.clear()
    dispatch.ensure_services({}, [], {}, False, None)
    legacy = f"CLAUDE_CONFIG_DIR={acme} claude --permission-mode auto --name 'merge-check' '/loop 30m /merge-check'"
    assert [shlex.split(s) for s in sent] == [shlex.split(legacy)]


@pytest.mark.parametrize("ctl", ["\x03", "\x15", "\r", "\n", "\x1b", "\x7f"])
def test_control_characters_never_reach_send_keys(fake_home, fake_tmux, ctl):
    sent, _ = fake_tmux
    c = {"id": f"i{ctl}d", "title": "t", "metadata": {"profile": "acme", "spec_slug": f"a{ctl}touch CTRLC{ctl}"}}
    dispatch.start_worker(c, "spec", 1, False)
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in sent[0]), repr(sent[0])
    sp = " " if ctl in "\r\n" else ""
    assert f"spec:a{sp}touch CTRLC{sp}".strip() in [t.strip() for t in shlex.split(sent[0])]


def test_control_strip_keeps_word_breaks(fake_home, fake_tmux):
    sent, _ = fake_tmux
    c = {"id": "i", "title": "t", "metadata": {"profile": "acme", "spec_slug": "line one\nline two\tend"}}
    dispatch.start_worker(c, "spec", 1, False)
    assert "spec:line one line two end" in shlex.split(sent[0])


def test_unknown_worker_harness_never_raises_in_status_or_view(fake_home, monkeypatch):
    monkeypatch.setattr(agents, "pane_exists", lambda p: True)
    w = {"stage": "run", "harness": "ghost", "pane": "%1", "session_id": "s"}
    assert agents.worker_status(w, {}) == ("dead", "s")
    monkeypatch.setattr(agents, "col_name", lambda lid: "Inbox")
    assert "unknown harness" in agents.worker_view({"list_id": "L", "metadata": {"pipeline_mode": "auto", "worker": w}}, {})


def test_bad_harness_config_is_refused_with_a_clear_error(fake_home, monkeypatch):
    _profile(fake_home, '[harnesses.codex]\nenv_var = "X=1; id"\n')
    with pytest.raises(SystemExit, match="env_var"):
        harnesses.get("codex")
    (fake_home / ".pl-t" / "config.toml").write_text('[harnesses.codex]\ninteractive = "codex {prompt}"\n')
    C.load(config_dir=str(fake_home / ".pl-t"))
    harnesses._CACHE.clear()
    with pytest.raises(SystemExit, match="list"):
        harnesses.get("codex")
    (fake_home / ".pl-t" / "config.toml").write_text('[harnesses.codex\n')
    harnesses._CACHE.clear()
    with pytest.raises(SystemExit, match="config.toml"):
        harnesses.get("codex")


def test_harness_overrides_are_parsed_once(fake_home, monkeypatch):
    _profile(fake_home, '[harnesses.codex]\nbin = "codex-beta"\n')
    calls = []
    real = harnesses.tomllib.loads
    monkeypatch.setattr(harnesses.tomllib, "loads", lambda t: calls.append(1) or real(t))
    assert harnesses.get("codex").bin == harnesses.get("codex").bin == "codex-beta"
    assert len(calls) == 1


def test_stage_account_must_be_a_known_account(fake_home):
    _profile(fake_home, '[accounts.a]\nconfig_dir = "~/ca"\n[stages.run]\naccount = "nope"\n')
    with pytest.raises(SystemExit, match="nope"):
        harnesses.harness_for("run", {"metadata": {}})


def test_unknown_worker_harness_skips_the_card_and_the_loop_continues(fake_home, monkeypatch, capsys):
    bad = {"id": "bad00000", "title": "b", "list_id": "L", "metadata": {"pipeline_mode": "auto",
           "worker": {"stage": "run", "harness": "ghost", "pane": "%1"}}}
    good = {"id": "good0000", "title": "g", "list_id": "L", "metadata": {"pipeline_mode": "auto"}}
    seen = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [bad, good])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Inbox")
    monkeypatch.setattr(dispatch, "stage_for", lambda c, col: seen.append(c["id"]) or None)
    monkeypatch.setattr(dispatch, "load_state", lambda: {"notified": {}})
    monkeypatch.setattr(dispatch, "save_state", lambda st: None, raising=False)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    try:
        dispatch.dispatch_once(1, True, pull=False)
    except Exception:
        pass   # later steps of the pass are not under test here
    assert "good0000" in seen
    assert "ghost" in capsys.readouterr().err


def _new_window_calls(monkeypatch, fake_home):
    calls = []
    real = dispatch.tmux

    def tmux(*args, check=True):
        if args[0] == "new-window":
            calls.append(list(args))
        return real(*args, check=check)
    monkeypatch.setattr(dispatch, "tmux", tmux)
    dispatch.start_worker({"id": "abc", "title": "T", "metadata": {"profile": "acme"}}, "spec", 1, False)
    C.SERVICES = {"merge-check": {"prompt": "/loop 30m /merge-check", "profile": "acme"}}
    dispatch.ensure_services({}, [], {}, False, None)
    assert len(calls) == 2
    return calls


def test_agent_windows_get_the_profiles_gh_config_dir(fake_home, fake_tmux, monkeypatch):
    monkeypatch.setattr(C, "GH_CONFIG_DIR", fake_home / "gh work", raising=False)
    for argv in _new_window_calls(monkeypatch, fake_home):
        assert ["-e", f"GH_CONFIG_DIR={fake_home / 'gh work'}"] in [argv[i:i + 2] for i in range(len(argv))], argv
    argv, env = harnesses.headless_argv(harnesses.get("claude"), "acme", "hi")
    assert env["GH_CONFIG_DIR"] == str(fake_home / "gh work")


def test_agent_windows_add_nothing_when_gh_config_dir_is_unset(fake_home, fake_tmux, monkeypatch):
    import os
    monkeypatch.setattr(C, "GH_CONFIG_DIR", None, raising=False)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    for argv in _new_window_calls(monkeypatch, fake_home):
        assert not any("GH_CONFIG_DIR" in a for a in argv)
        assert argv.count("-e") == 1                             # only DISABLE_AUTO_UPDATE, as before
    _, env = harnesses.headless_argv(harnesses.get("claude"), "acme", "hi")
    assert "GH_CONFIG_DIR" not in env and "GH_CONFIG_DIR" not in os.environ


def test_a_loop_pinned_to_a_parked_account_runs_on_another_of_its_harness(fake_home, fake_tmux, monkeypatch, capsys):
    sent, _ = fake_tmux
    asked, seen = [], []
    monkeypatch.setattr(dispatch, "healthy_profile",
                        lambda pref, cards, harness=None, skip=None: asked.append((pref, harness, skip)) or "acme2")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})
    monkeypatch.setattr(dispatch.events, "emit", lambda kind, card=None, **f: seen.append((kind, f)))
    C.SERVICES = {"call-ingest": {"prompt": "/loop 15m /call-ingest", "profile": "acme"}}
    st = {"services": {}}
    dispatch.ensure_services(st, [], {}, False, None)
    assert asked == [(None, "claude", "acme")]
    assert [shlex.split(s)[0] for s in sent] == [f"CLAUDE_CONFIG_DIR={fake_home / '.claude-acme2'}"]
    assert "call-ingest loop: its account acme is parked; running under acme2" in capsys.readouterr().out
    assert st["services"]["call-ingest"]["profile"] == "acme2" and st["services"]["call-ingest"]["fallback_from"] == "acme"
    assert ("loop_fallback", {"loop": "call-ingest", "account": "acme2", "parked": "acme"}) in seen


def test_a_loop_on_a_fallback_account_goes_back_at_its_next_start(fake_home, fake_tmux, monkeypatch):
    sent, _ = fake_tmux
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())          # acme is back
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)
    C.SERVICES = {"call-ingest": {"prompt": "/loop 15m /call-ingest", "profile": "acme"}}
    st = {"services": {"call-ingest": {"profile": "acme2", "fallback_from": "acme"}}}
    out = {"text": "@7 %9 claude call-ingest\n"}                               # a working loop: left alone
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=out["text"], stderr=""))
    dispatch.ensure_services(st, [], {}, False, None)
    assert sent == [] and st["services"]["call-ingest"]["profile"] == "acme2"
    out["text"] = ""                                                            # it exited: the next start is on acme
    dispatch.ensure_services(st, [], {}, False, None)
    assert [shlex.split(s)[0] for s in sent] == [f"CLAUDE_CONFIG_DIR={fake_home / '.claude-acme'}"]
    assert st["services"]["call-ingest"]["profile"] == "acme" and "fallback_from" not in st["services"]["call-ingest"]


def test_a_loop_with_fallback_false_waits_for_its_account(fake_home, fake_tmux, monkeypatch, capsys):
    sent, _ = fake_tmux
    monkeypatch.setattr(dispatch, "healthy_profile", lambda *a, **k: "acme2")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})
    _profile(fake_home, '[accounts.acme]\nconfig_dir = "~/.claude-acme"\n[accounts.acme2]\nconfig_dir = "~/.claude-acme2"\n'
                        '[loops.call-ingest]\nprompt = "/loop 15m /call-ingest"\naccount = "acme"\nfallback = false\n')
    st = {"services": {"call-ingest": {"profile": "acme2"}}}   # a stale fallover record must not win either
    dispatch.ensure_services(st, [], {}, False, None)
    assert sent == []
    assert "call-ingest loop waits: its account acme is parked" in capsys.readouterr().out
    assert st["services"]["call-ingest"]["waits"].startswith("its account acme is parked")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    dispatch.ensure_services(st, [], {}, False, None)
    assert [shlex.split(s)[0] for s in sent] == [f"CLAUDE_CONFIG_DIR={fake_home / '.claude-acme'}"]
    assert st["services"]["call-ingest"]["profile"] == "acme" and "waits" not in st["services"]["call-ingest"]
    import tomlkit
    assert any("fallback" in e for e in C.validate(tomlkit.parse('[loops.x]\nprompt = "p"\nfallback = "no"\n')))


def test_a_pinned_loop_waits_when_no_other_account_of_its_harness_is_free(fake_home, fake_tmux, monkeypatch, capsys):
    sent, _ = fake_tmux
    monkeypatch.setattr(dispatch, "healthy_profile", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"acme"})
    C.SERVICES = {"call-ingest": {"prompt": "/loop 15m /call-ingest", "profile": "acme"}}
    dispatch.ensure_services({}, [], {}, False, None)
    assert sent == [] and "call-ingest loop waits: its account acme is parked" in capsys.readouterr().out


# ---------- WP42: long launch commands, pl retry, never-started agents ----------

def test_a_5000_byte_prompt_types_only_a_short_sh_line(fake_home, fake_tmux):
    sent, _ = fake_tmux
    C.PROMPTS = {**C.PROMPTS, "spec": "/spec-writer {id} " + "x" * 5000}
    dispatch.start_worker({"id": "abc", "title": "T", "metadata": {"profile": "acme"}}, "spec", 1, False)
    C.SERVICES = {"merge-check": {"prompt": "/loop 30m " + "y" * 5000, "profile": "acme"}}
    dispatch.ensure_services({}, [], {}, False, None)
    assert len(typed) == 2
    assert all(len(t.encode()) < 200 for t in typed), typed
    assert shlex.split(sent[0])[-1] == "/spec-writer abc " + "x" * 5000
    assert shlex.split(sent[1])[-1] == "/loop 30m " + "y" * 5000


def test_the_launch_script_is_private_and_named_for_the_window(fake_home, fake_tmux):
    c = {"id": "abc", "title": "T", "metadata": {"profile": "acme", "spec_slug": "../../evil name"}}
    dispatch.start_worker(c, "spec", 1, False)
    path = shlex.split(typed[0])[1]
    assert path.startswith(str(C.STATE_DIR / "launch") + "/")
    assert "/" not in path[len(str(C.STATE_DIR / "launch")) + 1:]
    import os
    assert os.stat(path).st_mode & 0o777 == 0o700
    assert os.stat(C.STATE_DIR / "launch").st_mode & 0o777 == 0o700


def test_the_launch_script_round_trips_hostile_argv_through_sh(fake_home, fake_tmux, tmp_path):
    hostile = "it's $HOME `id` $(id) \\ \"q\" ; & | > < * ? ! # ~"
    C.PROMPTS = {**C.PROMPTS, "spec": hostile + " {id}"}
    C.PROFILES = {"acme": tmp_path / "cfg dir $X"}
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "argv.txt"
    fake = bindir / "claude"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CONFIG_DIR" "$@" > {shlex.quote(str(out))}\n')
    fake.chmod(0o755)
    dispatch.start_worker({"id": "abc", "title": "T", "metadata": {"profile": "acme"}}, "spec", 1, False)
    script = shlex.split(typed[0])[1]
    import os
    REAL_RUN(["sh", script], env={"PATH": f"{bindir}:/usr/bin:/bin"}, check=True)
    assert out.read_text().splitlines() == [str(tmp_path / "cfg dir $X"), "--permission-mode", "auto", "--session-id",
                                            SID, "--name", "spec:t",
                                            hostile + " abc"]
    assert not os.path.exists(script)   # it removes itself once it runs: a script left behind means it never ran


def test_gh_config_dir_stays_a_new_window_env_not_the_script(fake_home, fake_tmux, monkeypatch):
    monkeypatch.setattr(C, "GH_CONFIG_DIR", fake_home / "gh", raising=False)
    _new_window_calls(monkeypatch, fake_home)
    for t in typed:
        assert "GH_CONFIG_DIR" not in open(shlex.split(t)[1]).read()


def _funnel(monkeypatch, cs, status="dead"):
    """cards() and worker liveness faked; the real update() is replaced by a metadata merge on the dicts."""
    from pl import commands
    writes = []

    def update(cid, **f):
        writes.append((cid, f))
        c = next(x for x in cs if x["id"] == cid)
        c["metadata"] = {**c["metadata"], **f.get("metadata", {})}
        c["metadata"] = {k: v for k, v in c["metadata"].items() if v is not None}
    for mod in (commands, dispatch):
        monkeypatch.setattr(mod, "cards", lambda: cs, raising=False)
        monkeypatch.setattr(mod, "update", update, raising=False)
    monkeypatch.setattr(commands, "registry", lambda: {}, raising=False)
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: (status, w.get("session_id")), raising=False)
    return writes


def _dead(cid, attempts, stage="spec"):
    return {"id": cid, "title": f"card {cid}", "list_id": "L", "updated_at": "2026-09-29",
            "metadata": {"pipeline_mode": "auto", "profile": "acme", "worker": {"stage": stage, "attempts": attempts, "pane": "%1",
                                                              "window": "@1", "session_id": "s", "started_at": "2020-01-01T00:00:00+00:00"}}}


def test_retry_clears_the_worker_and_the_failed_mark_and_the_dispatcher_starts_it(fake_home, monkeypatch, capsys):
    from pl import commands
    from pl.util import load_state, save_state
    c = _dead("card0001aaaa", 3)
    writes = _funnel(monkeypatch, [c])
    save_state({"notified": {"card0001aaaa:spec_failed": 1, "other:spec_failed": 2}})
    commands.cmd_retry(types.SimpleNamespace(id="card0001", stage=None))
    assert writes == [("card0001aaaa", {"metadata": {"worker": None}})]
    assert load_state()["notified"] == {"card0001aaaa:spec_failed": 1, "other:spec_failed": 2}   # retry writes no state
    out = capsys.readouterr().out
    assert "card0001" in out and "spec" in out and "3" in out
    got = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Inbox")
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: got.append((c["id"], stage, attempts)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)
    assert got == [("card0001aaaa", "spec", 1)]
    assert load_state()["notified"] == {"other:spec_failed": 2}   # the dispatcher clears the mark on a fresh start


def test_retry_all_touches_only_cards_at_the_attempt_limit(fake_home, monkeypatch):
    from pl import commands
    cs = [_dead("aaaa0000aaaa", 3), _dead("bbbb0000bbbb", 1), _dead("cccc0000cccc", 3, stage="plan"),
          {**_dead("dddd0000dddd", 3), "metadata": {**_dead("x", 3)["metadata"], "pipeline_mode": "manual"}}]
    writes = _funnel(monkeypatch, cs)
    commands.cmd_retry(types.SimpleNamespace(id="all", stage=None))
    assert [w[0] for w in writes] == ["aaaa0000aaaa", "cccc0000cccc"]
    writes.clear()
    cs[0]["metadata"]["worker"] = _dead("x", 3)["metadata"]["worker"]
    commands.cmd_retry(types.SimpleNamespace(id="all", stage="spec"))
    assert [w[0] for w in writes] == ["aaaa0000aaaa"]


def test_retry_leaves_a_running_agent_alone_and_takes_an_issue_number(fake_home, monkeypatch, capsys):
    from pl import commands
    cs = [{**_dead("o/r#12", 3), "id": "o/r#12"}]
    writes = _funnel(monkeypatch, cs, status="alive")
    commands.cmd_retry(types.SimpleNamespace(id="#12", stage=None))
    assert writes == [] and "running" in capsys.readouterr().out
    writes = _funnel(monkeypatch, cs)
    commands.cmd_retry(types.SimpleNamespace(id="#12", stage=None))
    assert writes == [("o/r#12", {"metadata": {"worker": None}})]


def test_an_agent_whose_launch_never_ran_logs_a_clear_event(fake_home, fake_tmux, monkeypatch):
    import json
    c = {"id": "card0001", "title": "T", "list_id": "L", "metadata": {"pipeline_mode": "auto", "profile": "acme"}}
    dispatch.start_worker(c, "spec", 1, False)   # writes the script; the fake shell never runs it
    c["metadata"]["worker"] = {**fake_tmux[1][-1][1]["metadata"]["worker"], "started_at": "2020-01-01T00:00:00+00:00"}
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [c])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Inbox")
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("dead", w.get("session_id")))
    monkeypatch.setattr(dispatch, "start_worker", lambda *a: None)
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)
    dispatch.dispatch_once(1, False, pull=False)
    ev = [json.loads(line) for line in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]
    never = [e for e in ev if "never started" in (e.get("message") or "")]
    assert len(never) == 1   # once per failed launch, not once per pass
    assert never[0]["message"] == "agent never started in @7: the launch command did not run"
    assert never[0]["card"] == "card0001"


def test_two_cards_whose_slugs_share_28_chars_get_their_own_scripts(fake_home, fake_tmux):
    _, updates = fake_tmux
    C.PROMPTS = {**C.PROMPTS, "spec": "/spec-writer {id}"}
    title = "A very long shared card title prefix that goes on"
    dispatch.start_worker({"id": "aaaa0001", "title": title + " A", "metadata": {"profile": "acme"}}, "spec", 1, False)
    dispatch.start_worker({"id": "bbbb0002", "title": title + " B", "metadata": {"profile": "acme"}}, "spec", 1, False)
    paths = [shlex.split(t)[1] for t in typed]
    assert paths[0] != paths[1]
    assert "aaaa0001" in open(paths[0]).read() and "bbbb0002" not in open(paths[0]).read()
    assert "bbbb0002" in open(paths[1]).read()
    stored = [u[1]["metadata"]["worker"]["launch"] for u in updates if "worker" in u[1].get("metadata", {})]
    assert stored == paths   # the never-started check reads the path the card's own launch wrote


def test_retry_reads_the_board_fresh_first(fake_home, monkeypatch):
    from pl import commands
    order = []
    cs = [_dead("card0001aaaa", 3)]
    _funnel(monkeypatch, cs)
    monkeypatch.setattr(commands, "fresh_next", lambda: order.append("fresh"))
    monkeypatch.setattr(commands, "cards", lambda: order.append("cards") or cs)
    commands.retry("card0001")
    assert order[:2] == ["fresh", "cards"]


def test_retry_refuses_a_short_id_prefix(fake_home, monkeypatch):
    from pl import commands
    writes = _funnel(monkeypatch, [_dead("card0001aaaa", 3)])
    with pytest.raises(SystemExit):
        commands.retry("c")
    with pytest.raises(SystemExit):
        commands.retry("card000")
    assert writes == []
    commands.retry("card0001aaaa")   # the full id still works
    assert len(writes) == 1


# ---------- WP43: idle run agents free their slot; split and parked cards get no agents ----------

def _screen(monkeypatch, text, activity):
    """Fake the agent pane: its captured screen and its tmux window_activity (epoch seconds)."""
    monkeypatch.setattr(agents, "pane_exists", lambda p: True)

    def run(argv, **kw):
        out = str(int(activity)) if "display-message" in argv else text
        return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(harnesses, "_run", run)


RUN_W = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1"}


@pytest.mark.parametrize("text, idle, status, want", [
    ("⏺ GATE: nothing-runnable. Api PR #1556 is still open", 60, "idle", "waiting (nothing-runnable)"),
    ("GATE: not-approved — card is in Spec ready", 30, "idle", "waiting (not-approved)"),
    ("The loop: I cancelled it (job 52763bf6).", 700, "idle", "waiting (idle 11 min)"),
    ("working on WP2", 60, "idle", None),                               # between two loop fires, no marker: in flight
    ("⏺ GATE: nothing-runnable.", 700, "busy", None),                   # a busy session is working, whatever the screen says
])
def test_run_waiting_reads_gate_markers_and_idle_time(fake_home, monkeypatch, text, idle, status, want):
    _screen(monkeypatch, text, time.time() - idle)
    assert agents.run_waiting(RUN_W, {"s1": {"status": status}}) == want


def _pass(monkeypatch, cs, waiting, max_runs=1, ask=None):
    started = []
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: ask)   # the permission-prompt screen check
    monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "idle"}})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg: waiting)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: None)   # no real tmux
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append((c["id"], stage)))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product", "screen_hit_limit", "notify"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(max_runs, False, pull=False)
    return started


def _auto(cid, col, tags=(), **meta):
    return {"id": cid, "title": f"card {cid}", "list_id": col, "tags": list(tags), "updated_at": "2026-09-30",
            "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


def test_a_waiting_run_agent_frees_its_slot_and_is_never_started_twice(fake_home, monkeypatch):
    idle = _auto("aaaa0001", "In progress", worker=dict(RUN_W))
    new = _auto("bbbb0002", "Approved")
    assert _pass(monkeypatch, [idle, new], "waiting (nothing-runnable)") == [("bbbb0002", "run")]
    assert _pass(monkeypatch, [idle, new], None) == []   # a working run agent still holds the only slot


@pytest.mark.parametrize("tag, label", [("split", "split — see child cards"), ("parked", "held")])
def test_split_and_parked_cards_get_no_agent_and_say_why(fake_home, monkeypatch, tag, label):
    held = _auto("cccc0003", "Spec ready", tags=("api", tag), spec_approved_at="2026-09-29T10:00:00+00:00")
    run = _auto("dddd0004", "Approved", tags=(tag,))
    assert _pass(monkeypatch, [held, run], None) == []
    assert dispatch.approved_label(held, "Spec ready", {}) == label
    assert agents.worker_view(run, {}) == label


@pytest.mark.parametrize("n_waiting, want", [(3, 3), (5, 1)])
def test_live_run_agents_are_capped_at_twice_max_runs(fake_home, monkeypatch, capsys, n_waiting, want):
    idle = [_auto(f"w{i:07d}", "In progress", worker=dict(RUN_W)) for i in range(n_waiting)]
    new = [_auto(f"n{i:07d}", "Approved") for i in range(6)]
    assert len(_pass(monkeypatch, idle + new, "waiting (nothing-runnable)", max_runs=3)) == want
    assert f"live, cap {2 * 3})" in capsys.readouterr().out


# ---------- weekly limit screen ----------

WEEKLY = """You've hit your weekly limit, resets Oct 4 at 9pm
   ❯ 1. Stop and wait for limit to reset
     2. Wait here, then continue automatically at Oct 4 at 9pm
     3. Ask your admin for more usage
   Enter to confirm · Esc to cancel"""


@pytest.mark.parametrize("screen", [WEEKLY, "You've hit your session limit", "You’ve hit your 5-hour limit",
                                    "You've hit your usage limit", "5-hour limit reached ∙ resets 3pm",
                                    "❯ 1. Stop and wait for limit to reset", "You've hit your limit"])
def test_the_claude_limit_screens_are_detected(fake_home, monkeypatch, screen):
    monkeypatch.setattr(accounts, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=screen))
    assert accounts.screen_hit_limit("%1", harnesses.get("claude")) == screen
    assert C.LIMIT_RE.search(screen)


def test_a_card_stuck_on_a_limit_says_so_in_pl_list(fake_home, monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: "Inbox")
    w = {"stage": "spec", "session_id": "s", "pane": "%1", "profile": "acme",
         "limit_hit": {"account": "acme", "until": "2026-10-05T04:01:00+00:00"}}
    c = {"id": "card0001", "list_id": "L", "metadata": {"pipeline_mode": "auto", "worker": w}}
    t = datetime.fromisoformat("2026-10-05T04:01:00+00:00").astimezone()
    assert agents.worker_view(c, {"s": {"status": "waiting"}}) == f"limit hit — acme, resets {t:%b} {t.day} {t:%H:%M}"


# ---------- unattended permissions: pipeline agents and loops work without stopping at a prompt ----------

UNATTENDED_WANT = {"claude": ["--permission-mode", "auto"],
                   "codex": ["--ask-for-approval", "never", "--sandbox", "workspace-write"],
                   "antigravity": ["--mode", "accept-edits"]}


def _launched(fake_home, fake_tmux, harness, toml=""):
    """The argv of a spec agent and of a loop started under one account of this harness."""
    sent, _ = fake_tmux
    _profile(fake_home, f'[accounts.a]\nharness = "{harness}"\nconfig_dir = "~"\n{toml}')
    C.PROMPTS = {"spec": "/spec {id}"}
    dispatch.start_worker({"id": "abc", "title": "t", "metadata": {"profile": "a"}}, "spec", 1, False)
    C.SERVICES = {"rev": {"prompt": "/loop 30m /rev", "profile": "a"}}
    dispatch.ensure_services({}, [], {}, False, None)
    return [[t for t in shlex.split(s) if "=" not in t or t.startswith("-")] for s in sent]


@pytest.mark.parametrize("harness", sorted(UNATTENDED_WANT))
def test_every_pipeline_agent_and_loop_carries_its_harness_unattended_flags(fake_home, fake_tmux, harness):
    want = UNATTENDED_WANT[harness]
    for argv in _launched(fake_home, fake_tmux, harness):
        assert argv[1:1 + len(want)] == want, argv


@pytest.mark.parametrize("toml", ["[permissions]\nunattended = false\n",
                                  '[harnesses.claude]\nunattended = false\n'])
def test_the_off_switch_works_per_profile_and_per_harness(fake_home, fake_tmux, toml):
    for argv in _launched(fake_home, fake_tmux, "claude", toml):
        assert "--permission-mode" not in argv and "auto" not in argv, argv


def test_turning_one_harness_off_leaves_the_others_on(fake_home, fake_tmux):
    argv = _launched(fake_home, fake_tmux, "codex", "[harnesses.claude]\nunattended = false\n")
    assert all("never" in a for a in argv)


def test_a_template_that_sets_its_own_permission_flag_is_left_alone(fake_home, fake_tmux):
    toml = '[harnesses.claude]\ninteractive = ["claude", "--permission-mode", "acceptEdits", "{prompt}"]\n'
    for argv in _launched(fake_home, fake_tmux, "claude", toml):
        assert argv == ["claude", "--permission-mode", "acceptEdits", argv[-1]], argv


def test_a_moved_agent_resumes_with_the_unattended_flags(fake_home, fake_tmux):
    sent, _ = fake_tmux
    from pl import move_agent
    w = {"stage": "run", "session_id": SID, "pane": "%1", "window": "@1"}
    move_agent._relaunch({"id": "abc", "title": "t", "metadata": {}}, w, "acme", "go on")
    argv = shlex.split(sent[0])
    assert argv[1:4] == ["claude", "--permission-mode", "auto"] and "--resume" in argv


UNSAFE = re.compile(r"yolo|bypass|skip|dangerous|danger-full-access|full-auto|dontask", re.I)


def test_no_harness_default_ever_uses_a_yolo_bypass_or_skip_mode():
    assert set(harnesses.UNATTENDED) >= {*harnesses.BUILTINS, "gemini"}
    for name, flags in harnesses.UNATTENDED.items():
        assert flags and not any(UNSAFE.search(f) for f in flags), (name, flags)
    for h in harnesses.BUILTINS.values():
        assert not any(UNSAFE.search(t) for t in (*h.interactive, *h.headless, *h.resume)), h.name


def test_harnesses_that_still_ask_say_so_with_the_setting_to_change(fake_home):
    for name in ("antigravity", "gemini"):
        warn = harnesses.still_asks(name)
        assert warn and "may wait" in warn and "pl never" in warn
    assert harnesses.still_asks("claude") is None and harnesses.still_asks("codex") is None


# ---------- an agent stuck at a permission prompt: an alert and a card label ----------

CLAUDE_ASK = """⏺ Pushing the branch now.

╭──────────────────────────────────────────╮
│ Bash command                             │
│                                          │
│   git push origin feat/x                 │
│   Push the branch                        │
│                                          │
│ Do you want to proceed?                  │
│ ❯ 1. Yes                                 │
│   2. Yes, and don't ask again for git push│
│   3. No, and tell Claude what to do       │
╰──────────────────────────────────────────╯"""
CODEX_ASK = """Would you like to run the following command?
  $ npm test --token=ghp_abcdefghijklmnopqrstuvwxyz0123456789
› 1. Yes, proceed
  2. No, and tell Codex what to do differently"""


@pytest.mark.parametrize("harness, text, idle, want", [
    ("claude", CLAUDE_ASK, 130, "git push origin feat/x"),
    ("codex", CODEX_ASK, 300, "npm test"),
    ("claude", CLAUDE_ASK, 60, None),                       # at the prompt for under 2 minutes: maybe a person is on it
    ("claude", "", 600, None),                              # an empty screen
    ("claude", "⏺ Running the tests\n  ⎿  42 passed", 600, None),   # a normal, idle screen
    ("codex", CLAUDE_ASK, 600, None),                       # another harness's prompt is not this one's
])
def test_permission_wait_needs_a_prompt_on_screen_and_two_idle_minutes(fake_home, monkeypatch, harness, text, idle, want):
    _screen(monkeypatch, text, time.time() - idle)
    got = agents.permission_wait(harnesses.get(harness), "%1")
    assert (got is None) if want is None else (want in got)
    if got:
        assert "ghp_" not in got and len(got) <= 120


def test_a_stuck_agent_opens_an_alert_and_its_card_says_waiting_for_permission(fake_home, monkeypatch):
    from pl import alerts
    updates, opened = [], []
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: updates.append(f))
    real_open = alerts.open
    monkeypatch.setattr(alerts, "open", lambda *a: opened.append(a) or real_open(*a))
    c = _auto("eeee0005", "In progress", spec_slug="fix-x",
              worker={**RUN_W, "profile": "acme", "started_at": now_iso()})
    _pass(monkeypatch, [c], None, ask="Bash command · git push origin feat/x")
    key, sev, title = opened[0][:3]
    assert key == "permission_wait:eeee0005" and sev == "warn"
    assert title == "agent waiting for permission in run-fix-x: Bash command · git push origin feat/x"
    w = updates[-1]["metadata"]["worker"]
    assert w["permission_wait"]
    c["metadata"]["worker"] = w
    monkeypatch.setattr(agents, "col_name", lambda lid: "In progress")
    assert agents.worker_view(c, {}) == "run agent waiting for permission (acme)"
    # the prompt is answered: the label and the alert clear
    _pass(monkeypatch, [c], None)
    assert "permission_wait" not in updates[-1]["metadata"]["worker"]
    assert alerts.get("permission_wait:eeee0005")["resolved_at"]


def test_a_normal_screen_opens_no_alert_and_changes_no_card(fake_home, monkeypatch):
    from pl import alerts
    updates = []
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: updates.append(f))
    _pass(monkeypatch, [_auto("ffff0006", "In progress", worker=dict(RUN_W))], None)
    assert updates == [] and alerts.get("permission_wait:ffff0006") is None


def test_a_ctrl_c_during_the_record_write_starts_no_agent(fake_home, fake_tmux, monkeypatch):
    def update(cid, **f):
        raise KeyboardInterrupt   # Ctrl-C while the board write runs
    monkeypatch.setattr(dispatch, "update", update)
    c = {"id": "abc", "title": "Fix Thing", "metadata": {"profile": "acme"}}
    with pytest.raises(KeyboardInterrupt):
        dispatch.start_worker(c, "spec", 1, False)
    assert typed == [] and not list((C.STATE_DIR / "launch").glob("*"))   # no launch line typed, no script written


def test_the_worker_record_is_written_before_the_agent_starts_so_a_ctrl_c_never_doubles_it(fake_home, fake_tmux,
                                                                                            monkeypatch):
    _, updates = fake_tmux
    real, at_keys = dispatch.tmux, []

    def tmux(*args, check=True):
        if args[:1] == ("send-keys",):
            at_keys.append(len(updates))
            if args[-1] == "Enter":
                raise KeyboardInterrupt   # Ctrl-C right after the launch line was typed
        return real(*args, check=check)
    monkeypatch.setattr(dispatch, "tmux", tmux)
    c = {"id": "abc", "title": "Fix Thing", "metadata": {"profile": "acme"}}
    with pytest.raises(KeyboardInterrupt):
        dispatch.start_worker(c, "spec", 1, False)
    assert at_keys == [1, 1] and len(updates) == 1   # one board write, before any key
    w = updates[0][1]["metadata"]["worker"]
    assert (w["window"], w["pane"], w["stage"]) == ("@7", "%9", "spec") and dispatch._own_launch(w)
    monkeypatch.setattr(agents, "pane_exists", lambda pane: pane == "%9")
    assert agents.worker_status(w, {})[0] == "starting"   # the next pass counts it: no second agent


def test_retry_takes_repo_and_number_and_refuses_an_ambiguous_number(fake_home, monkeypatch):
    from pl import commands
    cs = [{**_dead("x", 3), "id": "o/r#12"}, {**_dead("x", 3), "id": "o/s#12"}]
    writes = _funnel(monkeypatch, cs)
    with pytest.raises(SystemExit) as e:
        commands.retry("#12")
    assert "o/r#12" in str(e.value) and "o/s#12" in str(e.value) and writes == []
    commands.retry("s#12")
    assert writes == [("o/s#12", {"metadata": {"worker": None}})]
