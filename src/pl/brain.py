"""The Brain: one machine-wide read-only harness session in tmux pl-brain:brain.

<machine-dir>/brain.json (0600) holds only session/pane IDs: never conversation text."""
import dataclasses
import json
import os
import uuid
from pathlib import Path

from pl import accounts, assistant, harnesses, manager
from pl import config as C
from pl.util import mask

TMUX_SESSION = "pl-brain"
WINDOW = "brain"
KEYS = ("session", "session_id", "account", "harness", "window", "pane", "conversation")
CLAUDE_RULES = {"permissions": {"allow": ["mcp__pl-brain__*"], "deny": ["Bash", "Edit", "Write"]}}


def _where():
    return assistant.Where(
        state_dir=manager.machine_dir(),
        file_name="brain.json",
        lock_name="brain.lock",
        tmux_session=TMUX_SESSION,
        window=WINDOW,
        keys=KEYS,
    )


def load():
    return assistant.load(where=_where())


def pane(st=None):
    return assistant.pane(st=st, where=_where())


def mcp_config_path() -> Path:
    p = manager.machine_dir() / "brain-mcp.json"
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": {"pl-brain": {"command": "pl", "args": ["mcp", "serve", "--brain"]}}}, f)
    return p


def ask_first(h, acct=None):
    """Refuse harnesses without tool restrictions. Claude gets strict pl-brain server only."""
    if h.name != "claude":
        raise SystemExit(
            f"pl: harness {h.name} has no tool restriction for the brain (pick a claude account)"
        )
    flags = [
        "--mcp-config",
        str(mcp_config_path()),
        "--strict-mcp-config",
        "--permission-mode",
        "manual",
        "--settings",
        json.dumps(CLAUDE_RULES),
    ]
    return dataclasses.replace(
        h,
        interactive=[*h.interactive[:1], *flags, *h.interactive[1:]],
        resume=[*h.resume[:1], *flags, *h.resume[1:]] if h.resume else [],
    )


def account():
    """[brain] account from machine.toml, else the first free account of the first profile."""
    from pl import profiles

    cfg = manager._machine_toml().get("brain", {})
    want_prof = cfg.get("profile")
    want_acct = cfg.get("account")
    parked = accounts.exhausted_profiles()
    profs = profiles.list_profiles(check_lock=False)

    if want_prof:
        row = next((p for p in profs if p["name"] == want_prof), None)
        accts = (row.get("accounts") or {}) if row else (C.ACCOUNTS if want_prof == C.PROFILE_NAME else {})
    elif profs:
        row = profs[0]
        accts = (row.get("accounts") or {}) if row["name"] != C.PROFILE_NAME else C.ACCOUNTS
    else:
        accts = C.ACCOUNTS

    if want_acct and want_acct in accts:
        return want_acct
    return next((a for a in accts if a not in parked), None)


def first_prompt():
    return (
        "You are the central read-only brain for pl. Answer questions across pipelines and connectors. "
        "Every fact needs a source from a tool. If no source is found, say so. Say you are ready and wait."
    )


def ensure():
    """Show the running brain session, or start one: resume the saved conversation when possible."""
    with assistant._locked(wait=assistant.LOCK_WAIT, where=_where()):
        return _ensure()


def _ensure():
    st = load()
    pane_ = pane(st)
    if pane_:
        if harnesses.pane_command(pane_) in harnesses.SHELLS:
            return "the brain exited: R starts a new conversation"
        return f"brain running in {TMUX_SESSION}:{WINDOW} ({st.get('account')})"

    acct = account()
    if acct is None:
        raise SystemExit("pl: no harness account is free for the brain")
    h = ask_first(harnesses.account_harness(acct), acct)
    if not h.interactive:
        raise SystemExit(f"pl: harness {h.name} has no interactive template for the brain")

    resume = bool(h.resume and st.get("session_id") and st.get("account") == acct and st.get("harness") == h.name)
    sid = st["session_id"] if resume else str(uuid.uuid4())
    conv_id = st.get("conversation") if resume else str(uuid.uuid4())

    if assistant._run(["tmux", "has-session", "-t", f"={TMUX_SESSION}"]).returncode:
        assistant._tmux("new-session", "-d", "-s", TMUX_SESSION, "-n", "dispatch", "-c", str(manager.machine_dir()))
    from pl import dispatch

    win = assistant._tmux(
        "new-window",
        "-d",
        "-t",
        f"={TMUX_SESSION}:",
        "-n",
        WINDOW,
        "-c",
        str(manager.machine_dir()),
        "-e",
        "DISABLE_AUTO_UPDATE=true",
        "-e",
        f"PL_CONFIG_DIR={C.CONFIG_DIR}",
        "-e",
        "PL_BRAIN=1",
        *dispatch._gh_env_args(),
        "-P",
        "-F",
        "#{window_id}",
    )
    assistant._tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    pane_ = assistant._tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]
    dispatch._launch(
        pane_,
        WINDOW,
        harnesses.launch_script(
            h, acct, None if resume else first_prompt(), sid, WINDOW, resume=resume, plugin=False, worktrees=False
        ),
    )
    assistant._save(
        {
            "session": sid,
            "session_id": sid,
            "account": acct,
            "harness": h.name,
            "window": win,
            "pane": pane_,
            "conversation": conv_id,
        },
        where=_where(),
    )
    return f"brain {'resumed' if resume else 'started'} in {TMUX_SESSION}:{WINDOW} ({acct})"


def send(text):
    """Type one message into the brain pane."""
    t = harnesses._typeable(text)
    if not t.strip():
        return
    with assistant._locked(wait=assistant.LOCK_WAIT, where=_where()):
        p = pane()
        if not p:
            raise SystemExit("pl: the brain is not running")
        assistant._type(p, t, enter=True, where=_where())


def screen(n):
    """The last n lines of the brain's screen, masked; [] when not running."""
    p = pane()
    return [mask(line) for line in assistant._tail(p, n)] if p else []


def reset():
    """End the conversation: close its window and reset state."""
    st = load()
    if pane(st):
        assistant._run(["tmux", "kill-window", "-t", st["window"]])
    conv_id = str(uuid.uuid4())
    assistant._save(
        {"account": st.get("account"), "harness": st.get("harness"), "conversation": conv_id},
        where=_where(),
    )
