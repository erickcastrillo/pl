"""Move a live Claude agent to another account in place: stop it with Ctrl-C in its own pane, copy its transcript,
relaunch `claude --resume` under the other account's folder. tmux, processes and files are all fakes."""
import json
import os
import shlex
import stat
import time
import types

import pytest

from pl import accounts, dispatch, harnesses, move_agent
from pl import config as C

SID = "11111111-2222-3333-4444-555555555555"
SLUG = "-work-repo"
BANNER = "Claude usage limit reached · resets 5pm"
NOTES = []


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    C.PROFILES = {"acme": tmp_path / ".claude-acme", "acme2": tmp_path / ".claude-acme2"}
    C.ACCOUNTS = {n: {"harness": "claude", "config_dir": str(d)} for n, d in C.PROFILES.items()}
    for d in C.PROFILES.values():
        d.mkdir()
    monkeypatch.setattr(move_agent, "_sleep", lambda s: None)
    monkeypatch.setattr(move_agent, "alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(move_agent, "notify", lambda t, m: NOTES.append((t, m)))
    NOTES.clear()

    def no_signal(pid, sig):
        raise AssertionError(f"a test tried to signal pid {pid}")
    monkeypatch.setattr(os, "kill", no_signal)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _transcript(home, acct="acme", sub=True):
    d = C.PROFILES[acct] / "projects" / SLUG
    d.mkdir(parents=True)
    (d / f"{SID}.jsonl").write_text('{"type":"user"}\n')
    if sub:
        (d / SID / "subagents").mkdir(parents=True)
        (d / SID / "subagents" / "agent-a1.jsonl").write_text('{"type":"assistant"}\n')
    return d / f"{SID}.jsonl"


class Pane:
    """One fake tmux pane plus the session registry. Ctrl-C number `exits_after` drops it to the shell."""

    def __init__(self, exits_after=2, holder=False, screen=""):
        self.cmd, self.ccs, self.exits_after, self.holder = "claude", 0, exits_after, holder
        self.calls, self.typed, self.screen, self.order = [], [], screen, []
        self.where = None   # the pane's "<session> <window> <window name>"; None = the pl session, window @5 run-card-title
        self.on_cc = None   # called at each Ctrl-C (a test hook)

    def run(self, argv, **kw):
        self.calls.append(argv)
        out = ""
        if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "C-c":
            self.ccs += 1
            self.order.append("C-c")
            if self.on_cc:
                self.on_cc()
            if self.exits_after and self.ccs >= self.exits_after:
                self.cmd = "zsh"
        elif argv[:2] == ["tmux", "display-message"] and argv[-1] == "#{pane_current_command}":
            out = self.cmd + "\n"
        elif argv[:2] == ["tmux", "display-message"] and argv[-1].startswith("#{session_name} #{window_id}"):
            where = (self.where or f"{C.TMUX_SESSION} @5 run-card-title").split()
            out = " ".join(where[:len(argv[-1].split())]) + "\n"
        elif argv[:2] == ["tmux", "capture-pane"]:
            out = self.screen
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    def tmux(self, *args, check=True):
        self.calls.append(["tmux", *args])
        if args[0] == "send-keys" and "-l" in args:
            self.typed.append(args[-1])
            self.order.append("launch")
        return ""

    def registry(self):
        alive = self.cmd != "zsh" or self.holder
        return {SID: {"sessionId": SID, "pid": 4242, "tmux": "pl:%5"}} if alive else {}


@pytest.fixture
def pane(monkeypatch):
    p = Pane()
    monkeypatch.setattr(move_agent, "_run", p.run)
    monkeypatch.setattr(dispatch, "tmux", p.tmux)
    monkeypatch.setattr(move_agent, "registry", p.registry)
    return p


def _card(age=3600, harness="claude", resume=None):
    started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - age))
    w = {"stage": "run", "session_id": SID, "pane": "%5", "window": "@5", "tmux_session": "pl", "profile": "acme",
         "harness": harness, "started_at": started, "attempts": 1}
    if resume:
        w["resume"] = resume
    return {"id": "card0001aaaa", "title": "Card title", "list_id": "L", "updated_at": "2026-09-29",
            "metadata": {"pipeline_mode": "auto", "profile": "acme", "worker": w}}


def _updates(monkeypatch, c):
    writes = []

    def update(cid, **f):
        writes.append((cid, f))
        c["metadata"] = {k: v for k, v in {**c["metadata"], **f.get("metadata", {})}.items() if v is not None}
    for mod in (move_agent, dispatch):
        monkeypatch.setattr(mod, "update", update)
    monkeypatch.setattr(move_agent, "card", lambda cid: json.loads(json.dumps(c)))   # the fresh read
    monkeypatch.setattr(move_agent, "fresh_next", lambda: None)
    return writes


def _script(typed):
    parts = shlex.split(typed)
    assert parts[0] == "sh" and len(parts) == 2
    return open(parts[1]).read()


def _events():
    try:
        return [json.loads(x) for x in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]
    except OSError:
        return []


# ---------- the move itself ----------

def test_move_stops_copies_and_resumes_under_the_target(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert ok, msg
    assert pane.order == ["C-c", "C-c", "launch"]          # stopped and verified before the relaunch
    script = _script(pane.typed[0])
    assert f"export CLAUDE_CONFIG_DIR={C.PROFILES['acme2']}" in script
    assert f"exec claude --permission-mode auto --resume {SID}" in script
    w = c["metadata"]["worker"]
    assert (w["profile"], w["session_id"], w["pane"], w["window"]) == ("acme2", SID, "%5", "@5")
    assert c["metadata"]["profile"] == "acme2"
    sw = c["metadata"]["profile_switches"][-1]
    assert (sw["from"], sw["to"], sw["reason"], sw["stage"]) == ("acme", "acme2", "usage limit", "run")
    assert w["resume"]["at"] > time.time() - 5 and w["resume"].get("skip") is None
    assert w["started_at"] == _card()["metadata"]["worker"]["started_at"] and w["moved_at"]   # the card's own start time
    ev = [e for e in _events() if e["kind"] == "agent_moved"]
    assert len(ev) == 1 and ev[0]["card"] == "card0001aaaa" and ev[0]["to"] == "acme2"


def test_transcript_and_subagents_copied_to_the_target_projects_folder_0600(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    dst = C.PROFILES["acme2"] / "projects" / SLUG
    f = dst / f"{SID}.jsonl"
    assert f.read_text() == '{"type":"user"}\n'
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    sub = dst / SID / "subagents" / "agent-a1.jsonl"
    assert sub.read_text() == '{"type":"assistant"}\n' and stat.S_IMODE(sub.stat().st_mode) == 0o600
    assert not (C.PROFILES["acme2"] / ".credentials.json").exists()


def test_credentials_and_links_are_never_copied(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    (C.PROFILES["acme"] / ".credentials.json").write_text("secret")
    d = C.PROFILES["acme"] / "projects" / SLUG / SID
    os.symlink(C.PROFILES["acme"] / ".credentials.json", d / "link.jsonl")
    os.link(C.PROFILES["acme"] / ".credentials.json", d / "hard.jsonl")
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    dst = C.PROFILES["acme2"] / "projects" / SLUG / SID
    assert not (dst / "link.jsonl").exists() and not (dst / "hard.jsonl").exists()
    assert "secret" not in "".join(p.read_text() for p in C.PROFILES["acme2"].rglob("*") if p.is_file())


def test_no_relaunch_while_the_old_process_is_alive(fake_home, monkeypatch, pane):
    _transcript(fake_home)
    pane.exits_after = 0                                   # Ctrl-C never brings the shell back
    c = _card()
    writes = _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "still running" in msg
    assert "launch" not in pane.order and pane.ccs >= 2
    assert all("profile_switches" not in f.get("metadata", {}) for _, f in writes)


def test_no_relaunch_while_a_background_holder_keeps_the_session(fake_home, monkeypatch, pane):
    _transcript(fake_home)
    pane.holder = True                                     # the pane is a shell but the session is still registered
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "still running" in msg and "launch" not in pane.order


def test_nothing_outside_the_pane_is_signalled(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    keys = [a for a in pane.calls if a[:2] == ["tmux", "send-keys"]]
    assert keys and all(a[a.index("-t") + 1] == "%5" for a in keys)
    assert not any(a[1] in ("kill-window", "kill-pane", "kill-session", "kill-server", "respawn-pane") for a in pane.calls)


def test_a_shell_pane_gets_no_ctrl_c(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    pane.cmd = "zsh"                                       # nothing to stop: no key is sent to the shell
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    assert pane.ccs == 0 and pane.order == ["launch"]


# ---------- refusals ----------

def test_refuses_without_a_transcript(fake_home, pane):
    assert "transcript" in move_agent.refusal(_card()["metadata"]["worker"], "acme2")


def test_refuses_the_same_folder_through_a_link(fake_home, pane):
    _transcript(fake_home)
    link = fake_home / "link-to-acme"
    os.symlink(C.PROFILES["acme"], link)
    C.PROFILES["acme3"] = link
    C.ACCOUNTS["acme3"] = {"harness": "claude", "config_dir": str(link)}
    assert "same" in move_agent.refusal(_card()["metadata"]["worker"], "acme3")
    assert "same" in move_agent.refusal(_card()["metadata"]["worker"], "acme")


def test_refuses_codex(fake_home, pane):
    _transcript(fake_home)
    assert "Claude" in move_agent.refusal(_card(harness="codex")["metadata"]["worker"], "acme2")
    C.ACCOUNTS["acme2"] = {"harness": "codex", "config_dir": str(C.PROFILES["acme2"])}
    assert "Claude" in move_agent.refusal(_card()["metadata"]["worker"], "acme2")


def test_refuses_an_unknown_account(fake_home, pane):
    _transcript(fake_home)
    assert "account" in move_agent.refusal(_card()["metadata"]["worker"], "nope")


# ---------- the dispatcher ----------

def _dispatch(monkeypatch, c, screen, others=(), started=None, pick=lambda harness: "acme2"):
    started = [] if started is None else started
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: [c, *others])
    monkeypatch.setattr(dispatch, "col_name", lambda lid: "Approved")
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", w.get("session_id")))
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cs, harness=None, pool=None: pick(harness))
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg, st=None: None)
    monkeypatch.setattr(dispatch, "permission_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda h, pane: None)
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda pane, h=None: screen)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a: started.append(a[0]["id"]))
    for name in ("sweep_untracked", "ensure_services", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)


def test_dispatcher_moves_an_old_agent_that_hit_the_limit(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card(age=3 * 3600)                               # far past the 10-minute fresh-restart window
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER + "\ncontinuing automatically at 5pm")
    assert pane.order == ["C-c", "C-c", "launch"]
    assert c["metadata"]["worker"]["profile"] == "acme2" and c["metadata"]["worker"]["session_id"] == SID
    assert "acme" in accounts.exhausted_profiles()        # the old account is still parked
    assert not any(a[1] == "kill-window" for a in pane.calls)


@pytest.mark.parametrize("why", ["no transcript", "same folder", "codex"])
def test_dispatcher_falls_back_when_the_move_is_refused(fake_home, pane, monkeypatch, why):
    c = _card(age=3 * 3600, harness="codex" if why == "codex" else "claude")
    if why != "no transcript":
        _transcript(fake_home)
    if why == "same folder":
        os.rmdir(C.PROFILES["acme2"])
        os.symlink(C.PROFILES["acme"], C.PROFILES["acme2"])
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER)                     # not auto-resuming: today's fresh restart elsewhere
    assert pane.ccs == 0 and pane.typed == []
    assert any(a[1:3] == ["kill-window", "-t"] for a in pane.calls)
    assert c["metadata"].get("worker") is None and c["metadata"]["profile"] == "acme2"
    assert [e["kind"] for e in _events() if e["kind"].startswith("agent_move")] == ["agent_move_refused"]


def test_dispatcher_falls_back_to_waiting_for_an_old_auto_resuming_agent(fake_home, pane, monkeypatch):
    c = _card(age=3 * 3600)                               # no transcript: refused
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER + "\ncontinuing automatically at 5pm")
    assert pane.calls == [] or not any(a[1] in ("kill-window", "send-keys") for a in pane.calls)
    assert c["metadata"]["worker"]["profile"] == "acme"


def test_dispatcher_does_not_restart_a_card_whose_move_failed_to_stop(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    pane.exits_after = 0
    c = _card(age=3 * 3600)
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER + "\ncontinuing automatically at 5pm")
    assert "launch" not in pane.order and c["metadata"]["worker"]["profile"] == "acme"


# ---------- the replayed banner ----------

def _entry(ts, text, kind="assistant", error=True):
    e = {"type": kind, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
         "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if error:
        e["isApiErrorMessage"] = True
    return json.dumps(e) + "\n"


def _resumed(fake_home, at):
    """A card already moved to acme2 at `at`, its transcript there holding the old limit from before the move."""
    d = C.PROFILES["acme2"] / "projects" / SLUG
    d.mkdir(parents=True)
    f = d / f"{SID}.jsonl"
    f.write_text('{"type":"user"}\n' + _entry(at - 30, BANNER))
    c = _card(resume={"at": at})
    c["metadata"]["worker"]["profile"] = c["metadata"]["profile"] = "acme2"
    return c, f


def test_a_replayed_banner_after_the_resume_does_not_park(fake_home, pane, monkeypatch):
    c, f = _resumed(fake_home, time.time() - 5)
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER)                      # the replay shows the old banner on screen
    assert "acme2" not in accounts.exhausted_profiles()
    assert pane.ccs == 0 and c["metadata"]["worker"]["profile"] == "acme2"


def test_a_new_limit_in_the_transcript_after_the_resume_parks(fake_home, pane, monkeypatch):
    c, f = _resumed(fake_home, time.time() - 120)
    with f.open("a") as out:
        out.write(_entry(time.time() - 60, "working", error=False) + _entry(time.time() - 5, BANNER))
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, "")                          # nothing on screen: the transcript alone counts
    assert "acme2" in accounts.exhausted_profiles()


def test_a_limit_word_in_a_user_entry_does_not_park(fake_home, pane, monkeypatch):
    c, f = _resumed(fake_home, time.time() - 120)
    with f.open("a") as out:
        out.write(_entry(time.time() - 5, BANNER, kind="user", error=False))
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, "")
    assert "acme2" not in accounts.exhausted_profiles()


# ---------- the continue prompt, the lock, the pane check, the copies ----------

def test_the_resume_carries_a_continue_prompt(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    line = next(x for x in _script(pane.typed[0]).splitlines() if x.startswith("exec "))
    assert shlex.split(line)[-1] == move_agent.CONTINUE_PROMPT and "usage limit" in move_agent.CONTINUE_PROMPT


def test_the_move_holds_a_lock_file_until_the_relaunch(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    writes = _updates(monkeypatch, c)
    lock = C.STATE_DIR / "moving" / "card0001aaaa.lock"
    seen = []
    pane.on_cc = lambda: seen.append(lock.read_text().split()[0])
    assert move_agent.move(c, "acme2", "usage limit")[0]
    assert seen and seen[0] == str(os.getpid()) and stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
    assert not lock.exists()
    assert all("moving" not in (f.get("metadata", {}).get("worker") or {}) for _, f in writes)


def _hold(pid, at=None):
    d = C.STATE_DIR / "moving"
    d.mkdir(parents=True, exist_ok=True)
    (d / "card0001aaaa.lock").write_text(f"{pid} {at or time.time()}\n")


def test_a_held_lock_refuses_a_second_move(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    _hold(os.getpid())
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "another move" in msg and pane.ccs == 0 and pane.typed == []


@pytest.mark.parametrize("stale", ["dead pid", "old"])
def test_a_stale_lock_is_removed(fake_home, pane, monkeypatch, stale):
    _transcript(fake_home)
    _hold(999999 if stale == "dead pid" else os.getpid(), time.time() - 11 * 60 if stale == "old" else None)
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]


def test_the_dispatcher_leaves_a_locked_card_alone_and_counts_its_slot(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    _hold(os.getpid())
    c = _card(age=3 * 3600)
    waiting = {"id": "card0002bbbb", "title": "Other", "list_id": "L", "updated_at": "2026-09-29",
               "metadata": {"pipeline_mode": "auto", "profile": "acme"}}
    _updates(monkeypatch, c)
    started = []
    _dispatch(monkeypatch, c, BANNER, others=[waiting], started=started)
    assert pane.calls == [] and c["metadata"]["worker"]["profile"] == "acme"
    assert started == []                                   # max_runs 1: the locked run still holds its slot


@pytest.mark.parametrize("where", ["bad id", "other session", "other window"])
def test_refuses_a_pane_outside_the_agent_window(fake_home, pane, monkeypatch, where):
    _transcript(fake_home)
    c = _card()
    if where == "bad id":
        c["metadata"]["worker"]["pane"] = "%5;kill"
    else:
        pane.where = "someone-else @5" if where == "other session" else f"{C.TMUX_SESSION} @9"
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "pane" in msg
    assert not any(a[:2] == ["tmux", "send-keys"] for a in pane.calls) and pane.typed == []


def test_a_failed_first_copy_refuses_without_stopping(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    monkeypatch.setattr(move_agent, "copy_transcript", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "copied" in msg and pane.ccs == 0 and pane.typed == []


def test_a_failed_second_copy_resumes_under_the_target_from_the_first_copy(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    real, n = move_agent.copy_transcript, []

    def copy(*a):
        n.append(1)
        if len(n) > 1:
            raise OSError("disk full")
        return real(*a)
    monkeypatch.setattr(move_agent, "copy_transcript", copy)
    started = c["metadata"]["worker"]["started_at"]
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert ok and len(n) == 2 and pane.order == ["C-c", "C-c", "launch"]
    script = _script(pane.typed[0])
    assert f"export CLAUDE_CONFIG_DIR={C.PROFILES['acme2']}" in script and f"--resume {SID}" in script
    w = c["metadata"]["worker"]
    assert w["profile"] == "acme2" and w["resume"]["at"] > time.time() - 5
    assert w["started_at"] == started and w["moved_at"]
    assert c["metadata"]["profile_switches"][-1]["to"] == "acme2"


def test_the_second_copy_catches_the_final_lines(fake_home, pane, monkeypatch):
    src = _transcript(fake_home)
    pane.on_cc = lambda: src.write_text(src.read_text() + '{"type":"assistant","n":2}\n')
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    assert '"n":2' in (C.PROFILES["acme2"] / "projects" / SLUG / f"{SID}.jsonl").read_text()


def test_find_transcript_refuses_a_linked_project_folder(fake_home):
    elsewhere = fake_home / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{SID}.jsonl").write_text("{}\n")
    (C.PROFILES["acme"] / "projects").mkdir()
    os.symlink(elsewhere, C.PROFILES["acme"] / "projects" / SLUG)
    assert move_agent.find_transcript("acme", SID) is None


@pytest.mark.parametrize("kind", ["hard link", "symlink"])
def test_an_existing_target_is_replaced_never_written_through(fake_home, pane, monkeypatch, kind):
    _transcript(fake_home)
    other = fake_home / "other.txt"
    other.write_text("keep me")
    dst = C.PROFILES["acme2"] / "projects" / SLUG
    dst.mkdir(parents=True)
    (os.link if kind == "hard link" else os.symlink)(other, dst / f"{SID}.jsonl")
    if kind == "hard link":
        os.utime(other, (0, 0))                            # older and smaller than the agent's own: it may be replaced
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    f = dst / f"{SID}.jsonl"
    assert other.read_text() == "keep me"
    assert not f.is_symlink() and f.read_text() == '{"type":"user"}\n' and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert [p.name for p in dst.iterdir() if p.name.startswith(".")] == []   # no temp file left behind


def test_a_successful_move_notifies_moved_and_resumed(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    assert move_agent.move(c, "acme2", "usage limit")[0]
    assert any("moved card0001 to acme2 and resumed it" in m for _, m in NOTES)


# ---------- pl move-agent ----------

def _manual(monkeypatch, c, answer=None):
    asked = []
    monkeypatch.setattr(move_agent, "find_card", lambda ref: c if "card0001aaaa".startswith(ref) else None)
    monkeypatch.setattr(move_agent, "cards", lambda: [c])
    monkeypatch.setattr(move_agent, "worker_status", lambda w, reg: ("alive", SID))

    def ask(prompt):
        asked.append(prompt)
        if answer is None:
            raise AssertionError("no confirm expected")
        return answer
    monkeypatch.setattr("builtins.input", ask)
    return asked


def test_manual_move_asks_first_and_no_leaves_it_alone(fake_home, pane, monkeypatch, capsys):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    asked = _manual(monkeypatch, c, answer="n")
    move_agent.cmd_move_agent(types.SimpleNamespace(card="card0001", account="acme2", yes=False))
    assert len(asked) == 1 and "acme2" in asked[0]
    assert pane.calls == [] and c["metadata"]["worker"]["profile"] == "acme"


@pytest.mark.parametrize("yes", [False, True])
def test_manual_move_yes_moves(fake_home, pane, monkeypatch, capsys, yes):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    asked = _manual(monkeypatch, c, answer=None if yes else "y")   # --yes: no confirm is asked
    move_agent.cmd_move_agent(types.SimpleNamespace(card="card0001", account="acme2", yes=yes))
    assert c["metadata"]["worker"]["profile"] == "acme2" and "moved" in capsys.readouterr().out
    assert c["metadata"]["profile_switches"][-1]["reason"] == "by hand" and len(asked) == (0 if yes else 1)


@pytest.mark.parametrize("why, match", [("no transcript", "transcript"), ("parked", "parked"), ("dead", "no live agent")])
def test_manual_move_refuses_with_the_same_rules(fake_home, pane, monkeypatch, why, match):
    if why != "no transcript":
        _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    _manual(monkeypatch, c, answer=None)
    if why == "parked":
        monkeypatch.setattr(move_agent, "exhausted_profiles", lambda: {"acme2"})
    if why == "dead":
        monkeypatch.setattr(move_agent, "worker_status", lambda w, reg: ("dead", SID))
    with pytest.raises(SystemExit, match=match):
        move_agent.cmd_move_agent(types.SimpleNamespace(card="card0001", account="acme2", yes=True))
    assert pane.calls == []


def test_manual_move_without_a_target_picks_another_healthy_claude_account(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    _manual(monkeypatch, c, answer=None)
    got = []
    monkeypatch.setattr(move_agent, "healthy_profile",
                        lambda want, cs, harness=None, skip=None: got.append((harness, skip)) or "acme2", raising=False)
    assert "acme2" in move_agent.move_card("card0001", None)
    assert got == [("claude", "acme")] and c["metadata"]["worker"]["profile"] == "acme2"


def test_move_agent_is_a_cli_command():
    from pl import cli
    assert "pl move-agent <card> <account>" in cli.__doc__


def test_the_dispatcher_keeps_an_agent_whose_second_copy_failed_on_the_target(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card(age=60)                                      # young: without the move it would restart fresh
    _updates(monkeypatch, c)
    real, n = move_agent.copy_transcript, []

    def copy(*a):
        n.append(1)
        if len(n) > 1:
            raise OSError("disk full")
        return real(*a)
    monkeypatch.setattr(move_agent, "copy_transcript", copy)
    _dispatch(monkeypatch, c, BANNER)
    assert not any(a[1] == "kill-window" for a in pane.calls)
    assert c["metadata"]["worker"]["profile"] == "acme2" and c["metadata"]["worker"]["session_id"] == SID


# ---------- round 2: the dispatcher's lock, fresh card data, target choice, window names, relaunch failure ----------

def _prep_card():
    c = _card(age=60)
    c["metadata"]["worker"]["stage"] = "spec"
    return c


def test_the_dispatcher_holds_the_card_lock_through_a_prep_restart(fake_home, pane, monkeypatch):
    c = _prep_card()
    _updates(monkeypatch, c)
    lock = C.STATE_DIR / "moving" / "card0001aaaa.lock"
    seen = []
    real = pane.tmux

    def tmux(*args, check=True):
        if args[0] == "kill-window":
            seen.append(lock.exists() and lock.read_text().split()[0])
        return real(*args, check=check)
    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a: None)
    _dispatch(monkeypatch, c, BANNER)
    assert seen == [str(os.getpid())] and not lock.exists()


@pytest.mark.parametrize("stage", ["run", "spec"])
def test_a_lock_taken_by_a_hand_move_mid_pass_skips_the_card_and_keeps_its_slot(fake_home, pane, monkeypatch, stage):
    _transcript(fake_home)
    c = _card(age=3 * 3600)
    c["metadata"]["worker"]["stage"] = stage
    _updates(monkeypatch, c)
    monkeypatch.setattr(move_agent, "locked", lambda cid: False)   # free at the first look, taken just after
    _hold(os.getpid())
    waiting = {"id": "card0002bbbb", "title": "Other", "list_id": "L", "updated_at": "2026-09-29",
               "metadata": {"pipeline_mode": "auto", "profile": "acme"}}
    started = []
    _dispatch(monkeypatch, c, BANNER, others=[waiting], started=started)
    assert not any(a[1] in ("kill-window", "send-keys") for a in pane.calls)
    assert c["metadata"]["worker"]["profile"] == "acme" and "limit_hit" not in c["metadata"]["worker"]
    if stage == "run":
        assert started == []


@pytest.mark.parametrize("field, value", [("pane", "%6"), ("session_id", "99999999-2222-3333-4444-555555555555"),
                                          ("profile", "acme2"), ("started_at", "2026-01-01T00:00:00+00:00")])
def test_a_move_on_stale_card_data_is_refused(fake_home, pane, monkeypatch, field, value):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)
    fresh = json.loads(json.dumps(c))
    fresh["metadata"]["worker"][field] = value
    monkeypatch.setattr(move_agent, "card", lambda cid: fresh)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "changed" in msg and pane.ccs == 0 and pane.typed == []


@pytest.mark.parametrize("how", ["newer", "larger"])
def test_a_newer_or_larger_target_transcript_is_never_replaced(fake_home, pane, monkeypatch, how):
    src = _transcript(fake_home)
    dst = C.PROFILES["acme2"] / "projects" / SLUG
    dst.mkdir(parents=True)
    f = dst / f"{SID}.jsonl"
    body = "x" * (len(src.read_text()) if how == "newer" else 500)
    f.write_text(body)
    os.utime(src, (time.time() - 100, time.time() - 100))
    if how == "larger":
        os.utime(f, (time.time() - 200, time.time() - 200))
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and pane.ccs == 0 and pane.typed == [] and f.read_text() == body


def test_the_dispatcher_moves_a_run_agent_to_a_claude_account(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    C.PROFILES["acme3"] = fake_home / ".codex-acme3"
    C.PROFILES["acme3"].mkdir()
    C.ACCOUNTS["acme3"] = {"harness": "codex", "config_dir": str(C.PROFILES["acme3"])}
    c = _card(age=3 * 3600)
    _updates(monkeypatch, c)
    _dispatch(monkeypatch, c, BANNER, pick=lambda harness: "acme2" if harness == "claude" else "acme3")
    assert c["metadata"]["worker"]["profile"] == "acme2" and pane.order == ["C-c", "C-c", "launch"]


def test_a_reused_pane_id_in_another_window_name_is_refused(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    pane.where = f"{C.TMUX_SESSION} @5 run-some-other-card"   # a tmux restart handed the ids to another window
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "pane" in msg and pane.ccs == 0


def test_worker_status_matches_the_pane_exactly(monkeypatch):
    from pl import agents
    monkeypatch.setattr(agents, "pane_exists", lambda p: True)
    monkeypatch.setattr(harnesses, "pane_command", lambda p: "")
    w = {"session_id": "mine", "pane": "%5", "window": "@5", "harness": "claude", "started_at": "2026-01-01T00:00:00+00:00"}
    assert agents.worker_status(w, {"theirs": {"tmux": "other-session:@5.%5"}}) == ("dead", "mine")
    assert agents.worker_status(w, {"theirs": {"tmux": f"{C.TMUX_SESSION}:@5.%5"}}) == ("alive", "theirs")


def test_a_young_unreadable_lock_counts_as_held_and_an_old_one_is_broken(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    d = C.STATE_DIR / "moving"
    d.mkdir(parents=True)
    lock = d / "card0001aaaa.lock"
    lock.write_text("")
    c = _card()
    _updates(monkeypatch, c)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "another move" in msg and lock.exists()
    os.utime(lock, (time.time() - 30, time.time() - 30))
    assert move_agent.move(c, "acme2", "usage limit")[0]


def test_breaking_a_lock_puts_back_a_fresh_one_taken_meanwhile(fake_home):
    _hold(os.getpid())
    lock = C.STATE_DIR / "moving" / "card0001aaaa.lock"
    before = lock.read_text()
    move_agent._break(lock)
    assert lock.read_text() == before
    assert [p.name for p in lock.parent.iterdir()] == ["card0001aaaa.lock"]


def test_a_failed_relaunch_after_the_stop_clears_the_worker_and_alerts(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card()
    _updates(monkeypatch, c)

    def tmux(*args, check=True):
        if args[0] == "send-keys":
            raise SystemExit("pl: tmux send-keys: no such pane")
        return ""
    monkeypatch.setattr(dispatch, "tmux", tmux)
    ok, msg, _ = move_agent.move(c, "acme2", "usage limit")
    assert not ok and "relaunch failed" in msg and c["metadata"].get("worker") is None
    assert any(e["kind"] == "agent_move_failed" for e in _events())
    assert any("relaunch failed for card0001" in m for _, m in NOTES)
    from pl import alerts
    assert any("relaunch" in str(a.get("title")) for a in alerts._load().values())


def test_the_parked_alert_after_a_move_says_moved_and_resumed(fake_home, pane, monkeypatch):
    _transcript(fake_home)
    c = _card(age=3 * 3600)
    _updates(monkeypatch, c)
    said = []
    monkeypatch.setattr(dispatch, "notify", lambda t, m: said.append(m))
    _dispatch(monkeypatch, c, BANNER)
    assert any("moved and resumed" in m for m in said) and not any("young agents" in m for m in said)


# ---------- the test guard ----------

@pytest.mark.parametrize("argv", [["env", "PATH=/nonexistent", "tmux", "ls"], ["sh", "-c", "PATH=/nonexistent tmux ls"],
                                  ["bash", "-c", "PATH=/nonexistent; tmux ls"]])
def test_the_guard_catches_tmux_behind_env_or_a_shell(argv):
    import subprocess
    with pytest.raises(AssertionError, match="real tmux"):
        subprocess.run(argv, capture_output=True)


@pytest.mark.parametrize("fn", ["system", "popen"])
def test_the_guard_catches_tmux_through_os_system_and_popen(fn):
    with pytest.raises(AssertionError, match="real tmux"):
        getattr(os, fn)("PATH=/nonexistent tmux ls >/dev/null 2>&1")
