"""Central read-only brain: ask questions across pipelines with checked sources."""
import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from pl import accounts, assistant, config as C, harnesses, manager, profiles, util
from pl.manager import machine_dir
from pl.util import mask, short_id

URL_RE = re.compile(r"https?://[^\s\"'>]+")
NO_SOURCE_ANSWER = "I found no source for that, so I won't guess."


def _run(argv: list[str], env: dict[str, str] | None = None, input: str | None = None, **kw: Any) -> subprocess.CompletedProcess:
    """The one subprocess seam of this module; tests fake it."""
    return subprocess.run(argv, env=env, input=input, capture_output=True, text=True, **kw)


def _sources_path(conv: str) -> Path:
    d = machine_dir() / "brain" / "conversations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{conv}.sources.jsonl"


def _cards_path(conv: str) -> Path:
    d = machine_dir() / "brain" / "conversations"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"{conv}.cards.jsonl"


def log_source(conv_or_source: Any, source_or_none: dict[str, Any] | None = None) -> None:
    if isinstance(conv_or_source, dict):
        conv, source = "default", conv_or_source
    else:
        conv, source = str(conv_or_source), source_or_none or {}
    p = _sources_path(conv)
    with open(p, "a") as f:
        f.write(json.dumps(source) + "\n")


def get_logged_sources(conv: str) -> list[dict[str, Any]]:
    p = _sources_path(conv)
    if not p.is_file():
        return []
    try:
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:
        return []


def log_card(conv_or_card: Any, card_or_none: dict[str, Any] | None = None) -> None:
    if isinstance(conv_or_card, dict):
        conv, card = "default", conv_or_card
    else:
        conv, card = str(conv_or_card), card_or_none or {}
    p = _cards_path(conv)
    with open(p, "a") as f:
        f.write(json.dumps(card) + "\n")


def get_logged_cards(conv: str) -> list[dict[str, Any]]:
    p = _cards_path(conv)
    if not p.is_file():
        return []
    try:
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception:
        return []


def record_sources(conv: str | None, result: Any) -> list[dict[str, Any]]:
    """Extract links and record ids from a tool result and append them to the conversation source log."""
    conv = conv or "default"
    found: list[dict[str, Any]] = []

    def _collect(val: Any) -> None:
        if val is None:
            return
        if hasattr(val, "content"):
            for item in getattr(val, "content", []):
                if text := getattr(item, "text", None):
                    _collect(text)
            return
        if isinstance(val, str):
            s = val.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    _collect(json.loads(s))
                    return
                except Exception:
                    pass
            for m in URL_RE.finditer(s):
                found.append({"url": m.group(0)})
            return
        if isinstance(val, list):
            for item in val:
                _collect(item)
            return
        if isinstance(val, dict):
            entry = {k: val[k] for k in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount") if k in val and val[k] is not None}
            for link in val.get("links") or []:
                if isinstance(link, str) and URL_RE.match(link):
                    found.append({"url": link})
            if "url" in entry or "link" in entry or "id" in entry:
                found.append(entry)
            for k, v in val.items():
                if k not in ("url", "link", "id", "title", "kind", "date", "pipeline", "amount") and isinstance(v, (dict, list)):
                    _collect(v)

    _collect(result)
    seen, logged = set(), []
    for item in found:
        key = (item.get("url"), item.get("link"), item.get("id"))
        if key not in seen:
            seen.add(key)
            log_source(conv, item)
            logged.append(item)
    return logged


def _is_parked(acc: str) -> bool:
    from pl import accounts
    info = accounts.parking(acc)
    until = info.get("until")
    return bool(until and util.parse_iso(until) > time.time())


def _pick_profile_and_account() -> tuple[str | None, str | None]:
    mt = manager._machine_toml()
    b = mt.get("brain", {}) if isinstance(mt.get("brain"), dict) else {}
    p_name = b.get("profile")
    a_name = b.get("account")

    if p_name:
        C.load(p_name)
        if a_name and a_name in C.PROFILES:
            return p_name, a_name
        for acc in C.PROFILES:
            if not _is_parked(acc):
                return p_name, acc
        return p_name, next(iter(C.PROFILES), None)

    rows = profiles.list_profiles(check_lock=False)
    for r in rows:
        C.load(r["name"])
        for acc in C.PROFILES:
            if not _is_parked(acc):
                return r["name"], acc
    if rows:
        C.load(rows[0]["name"])
        return rows[0]["name"], next(iter(C.PROFILES), None)
    return None, None


def ask(question: str, conv: str | None = None) -> dict[str, Any]:
    conv = conv or f"ask-{short_id(str(time.time()))}"

    profile, account = _pick_profile_and_account()
    if not account:
        raise SystemExit("pl: no account found for brain")

    h = harnesses.account_harness(account)
    if h.name not in ("claude", "codex"):
        sys.stderr.write(f"pl: {h.name} has no way to restrict tools\n")
        raise SystemExit(2)

    guide_path = Path(__file__).with_name("brain.md")
    guide = guide_path.read_text() if guide_path.is_file() else ""
    prompt = f"{guide}\n\nQuestion: {question}"

    argv, env = harnesses.headless_argv(h, account, prompt)

    mcp_config = {"mcpServers": {"pl-brain": {"command": "pl", "args": ["mcp", "serve", "--brain", "--stdio"]}}}
    mcp_json = json.dumps(mcp_config)

    if h.name == "claude":
        mcp_flags = ["--mcp-config", mcp_json, "--strict-mcp-config", "--allowedTools", "mcp__pl-brain__*"]
        argv = [argv[0], *mcp_flags, *[a for a in argv[1:] if a not in ("Bash", "Edit", "Write")]]
    elif h.name == "codex":
        codex_arg = f'mcp_servers.pl-brain={json.dumps(mcp_config["mcpServers"]["pl-brain"])}'
        argv = [argv[0], "-c", codex_arg, "--sandbox", "read-only", *argv[1:]]

    res = _run(argv, env=env, input=None)
    out = res.stdout if hasattr(res, "stdout") else str(res)

    try:
        data = json.loads(out)
    except Exception:
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}
        else:
            data = {}

    answer = data.get("answer") or ""
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    cards = data.get("cards") if isinstance(data.get("cards"), list) else []

    logged_sources = get_logged_sources(conv)
    logged_urls = {s.get("url") for s in logged_sources if s.get("url")}
    logged_urls |= {s.get("link") for s in logged_sources if s.get("link")}

    valid_sources = [
        s for s in sources
        if isinstance(s, dict) and (s.get("url") in logged_urls or s.get("link") in logged_urls)
    ]

    if not valid_sources:
        answer = NO_SOURCE_ANSWER
        valid_sources = []

    logged_cards = get_logged_cards(conv)
    valid_cards = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        curl = c.get("url")
        if any((cid and lc.get("id") == cid) or (curl and lc.get("url") == curl) for lc in logged_cards):
            valid_cards.append(c)

    return {
        "answer": answer,
        "sources": valid_sources,
        "cards": valid_cards,
        "conversation": conv,
    }


def cmd_ask(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="pl ask", description="ask the central read-only brain")
    ap.add_argument("question", help="the question to ask")
    ap.add_argument("--json", action="store_true", help="output result as JSON")
    ap.add_argument("--conversation", help="conversation id")
    a = ap.parse_args(argv)
    res = ask(a.question, conv=a.conversation)
    if a.json:
        print(json.dumps(res))
    else:
        print(res["answer"])
        if res.get("sources"):
            print()
            for i, s in enumerate(res["sources"], 1):
                t = s.get("title")
                u = s.get("url")
                if t and u and t != u:
                    print(f"{i}. {t}: {u}")
                else:
                    print(f"{i}. {u or t}")


TMUX_SESSION, WINDOW = "pl-brain", "brain"
KEYS = ("session", "session_id", "account", "harness", "window", "pane", "conversation")
CLAUDE_RULES = {"permissions": {"allow": ["mcp__pl-brain__*"], "deny": ["Bash", "Edit", "Write"]}}


def _where():
    return assistant.Where(manager.machine_dir(), "brain.json", "brain.lock", TMUX_SESSION, WINDOW, KEYS)


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
        raise SystemExit(f"pl: harness {h.name} has no tool restriction for the brain (pick a claude account)")
    flags = ["--mcp-config", str(mcp_config_path()), "--strict-mcp-config", "--permission-mode", "manual",
             "--settings", json.dumps(CLAUDE_RULES)]
    return dataclasses.replace(h, interactive=[*h.interactive[:1], *flags, *h.interactive[1:]],
                               resume=[*h.resume[:1], *flags, *h.resume[1:]] if h.resume else [])


def account():
    """[brain] account from machine.toml, else the first free account of the first profile."""
    from pl import profiles
    cfg = manager._machine_toml().get("brain", {})
    want_prof, want_acct = cfg.get("profile"), cfg.get("account")
    profs = profiles.list_profiles(check_lock=False)
    if want_prof:
        row = next((p for p in profs if p["name"] == want_prof), None)
        accts = (row.get("accounts") or {}) if row else (C.ACCOUNTS if want_prof == C.PROFILE_NAME else {})
    elif profs:
        row = profs[0]
        accts = (row.get("accounts") or {}) if row["name"] != C.PROFILE_NAME else C.ACCOUNTS
    else:
        accts = C.ACCOUNTS
    return want_acct if want_acct and want_acct in accts else next((a for a in accts if a not in accounts.exhausted_profiles()), None)


def first_prompt():
    return ("You are the central read-only brain for pl. Answer questions across pipelines and connectors. "
            "Every fact needs a source from a tool. If no source is found, say so. Say you are ready and wait.")


def ensure():
    with assistant._locked(wait=assistant.LOCK_WAIT, where=_where()):
        return _ensure()


def _ensure():
    st, pane_ = load(), pane()
    if pane_:
        return "the brain exited: R starts a new conversation" if harnesses.pane_command(pane_) in harnesses.SHELLS else f"brain running in {TMUX_SESSION}:{WINDOW} ({st.get('account')})"
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
    win = assistant._tmux("new-window", "-d", "-t", f"={TMUX_SESSION}:", "-n", WINDOW, "-c", str(manager.machine_dir()),
                          "-e", "DISABLE_AUTO_UPDATE=true", "-e", f"PL_CONFIG_DIR={C.CONFIG_DIR}", "-e", "PL_BRAIN=1",
                          *dispatch._gh_env_args(), "-P", "-F", "#{window_id}")
    assistant._tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    pane_ = assistant._tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]
    dispatch._launch(pane_, WINDOW, harnesses.launch_script(h, acct, None if resume else first_prompt(), sid, WINDOW,
                                                           resume=resume, plugin=False, worktrees=False))
    assistant._save({"session": sid, "session_id": sid, "account": acct, "harness": h.name,
                    "window": win, "pane": pane_, "conversation": conv_id}, where=_where())
    return f"brain {'resumed' if resume else 'started'} in {TMUX_SESSION}:{WINDOW} ({acct})"


def send(text):
    t = harnesses._typeable(text)
    if t.strip():
        with assistant._locked(wait=assistant.LOCK_WAIT, where=_where()):
            p = pane()
            if not p:
                raise SystemExit("pl: the brain is not running")
            assistant._type(p, t, enter=True, where=_where())


def screen(n):
    p = pane()
    return [mask(line) for line in assistant._tail(p, n)] if p else []


def reset():
    st = load()
    if pane(st):
        assistant._run(["tmux", "kill-window", "-t", st["window"]])
    assistant._save({"account": st.get("account"), "harness": st.get("harness"), "conversation": str(uuid.uuid4())}, where=_where())
