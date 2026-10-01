"""The Assistant: one live, interactive harness session in the profile's tmux window `assistant` that runs pl for the
person. pl starts it, resumes it, types into it and reads its screen; the harness asks the person before each tool call.

<profile>/state/assistant.json (0600) holds only the KEYS below: never conversation text."""
import contextlib
import dataclasses
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from pl import accounts, agents, events, harnesses, ideas
from pl import config as C
from pl.util import mask, now_iso

WINDOW = "assistant"
GUIDE = Path(__file__).with_name("assistant.md")
KEYS = ("session_id", "account", "harness", "window", "pane", "started_at", "mode", "pending", "last_offer_at")
DIGIT_RE = re.compile(r"[1-9]")
PANE_RE, WINDOW_RE = re.compile(r"%\d+"), re.compile(r"@\d+")
LOG_CHARS = 200
# The harness's "ask the person" mode, forced whatever the account's own default is (strings of claude 2.1.286 and
# codex 0.150.1 --help). A harness missing here, or a template that already sets a permission flag, is refused.
# claude also gets permission rules through --settings (an inline JSON string, per its --help). Claude Code checks deny,
# then ask, then allow, so an ask rule beats every allow rule, the account's own included; a deny rule beats both.
# Reads and the read-only commands below run without a prompt; any other pl command, gh or git push asks.
READ_TOOLS = ["Read", "Glob", "Grep", "LS", "NotebookRead"]
READ_ONLY = ["Bash(pl list*)", "Bash(pl card *)", "Bash(pl alerts*)", "Bash(pl usage*)", "Bash(pl standup*)",
             "Bash(pl manager status*)", "Bash(pl accounts)", "Bash(pl whatsnew)", "Bash(git status*)",
             "Bash(git log*)", "Bash(git diff*)", "Bash(gh pr view*)", "Bash(gh pr list*)"]
PL_WRITES = ("idea", "review", "approve", "reject", "dispatch", "pull", "adopt", "done", "move", "board", "retry",
             "profiles", "watch", "pause", "resume", "intent", "assistant", "setup")   # move also covers move-agent
GH_TOP = ("agent-task", "alias", "api", "attestation", "auth", "browse", "cache", "co", "codespace", "completion",
          "config", "copilot", "discussion", "extension", "gist", "gpg-key", "issue", "label", "org", "preview",
          "project", "release", "repo", "ruleset", "run", "search", "secret", "skill", "ssh-key", "status", "variable",
          "workflow")                                                            # every gh command but pr (gh 2.x)
GH_PR_WRITES = ("checkout", "close", "comment", "create", "edit", "lock", "merge", "ready", "reopen", "revert",
                "review", "unlock", "update-branch")
# --ac, --r, --d: argparse also takes a unique prefix of a long flag (--ack, --reset, --delete)
ASK_RULES = ["Bash(pl -*)", *(f"Bash(pl {c}*)" for c in PL_WRITES), "Bash(pl alerts *--ac*)",
             "Bash(pl accounts *--r*)", "Bash(pl card *--d*)", "Bash(pl manager *start*)", "Bash(pl manager *stop*)",
             "Bash(pl manager *run*)", "Bash(pl manager -*)", "Bash(gh -*)", *(f"Bash(gh {c}*)" for c in GH_TOP), "Bash(gh pr -*)",
             *(f"Bash(gh pr {c}*)" for c in GH_PR_WRITES), "Bash(git push*)", "Bash(git push:*)", "Bash(git -*)",
             "Bash(git diff *--output*)", "Bash(git log *--output*)", "Bash(git difftool*)"]
DENY_RULES = [*(f"Read(**/{f})" for f in (".credentials*", "auth.json", ".env*", "*.pem", "id_rsa*", "id_ed25519*",
                                         ".netrc", "hosts.yml")), "Bash(security *)"]
CLAUDE_RULES = {"permissions": {"allow": READ_TOOLS + READ_ONLY, "ask": ASK_RULES, "deny": DENY_RULES}}
ASK_FIRST = {"claude": ["--permission-mode", "manual", "--settings", json.dumps(CLAUDE_RULES)],
             "codex": ["--ask-for-approval", "on-request", "--sandbox", "read-only"]}
PERMISSION_FLAG_RE = harnesses.PERMISSION_FLAG_RE
RISKY = ("pl", "pl approve x", "gh", "gh pr merge 1", "git push", "git push origin main")   # what the ask rules cover
OFFER_GAP = 60            # seconds between two alert offers
KEY_RE = re.compile(r"[^A-Za-z0-9:_-]")
MENU_RE = re.compile(r"^\s*(?:[❯›>]\s*)?[1-9][.)]\s+\S")
INPUT_RE = re.compile(r"^\s*[│┃|]?\s*[>❯›]\s?(.*?)\s*[│┃|]?\s*$")
WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _run(argv):
    """The one subprocess seam of this module; tests fake it."""
    return subprocess.run(argv, capture_output=True, text=True)


def _tmux(*args):
    r = _run(["tmux", *args])
    if r.returncode:
        raise SystemExit(f"pl: tmux {args[0]}: {(r.stderr or '').strip()}")
    return (r.stdout or "").strip()


# ---------- state ----------

def _path():
    return C.STATE_DIR / "assistant.json"


def load():
    try:
        d = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if k in KEYS} if isinstance(d, dict) else {}


def _save(st):
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_name(f".assistant.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({k: v for k, v in st.items() if k in KEYS}, f)
    os.replace(tmp, _path())


@contextlib.contextmanager
def _locked():
    """The profile's assistant lock (a file lock in its state folder): two consoles, or a console and the dispatcher,
    never start two windows or write back a stale state. Not reentrant: never nest it."""
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(C.STATE_DIR / "assistant.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def pane(st=None):
    """The saved pane when it is still the assistant's: a pane id in the saved window, named `assistant`, of this
    profile's tmux session (a tmux restart reuses ids). None otherwise. Every tmux write goes through this."""
    st = load() if st is None else st
    p, w = st.get("pane"), st.get("window")
    if not (isinstance(p, str) and PANE_RE.fullmatch(p) and isinstance(w, str) and WINDOW_RE.fullmatch(w)):
        return None
    r = _run(["tmux", "display-message", "-p", "-t", p, "#{session_name} #{window_id} #{window_name}"])
    return p if r.returncode == 0 and (r.stdout or "").strip() == f"{C.TMUX_SESSION} {w} {WINDOW}" else None


# ---------- account and first prompt ----------

def account():
    """[assistant] account when it names an account, else the first account not parked, else None."""
    want = C.ASSISTANT.get("account")
    if want in C.PROFILES:
        return want
    parked = accounts.exhausted_profiles()
    return next((a for a in C.PROFILES if a not in parked), None)


def first_prompt():
    return (f"You are the pl assistant for profile {C.PROFILE_NAME}. Read {GUIDE} and follow it, "
            "then say you are ready and wait.")


# ---------- start, resume, reattach ----------

def work_dir():
    """Where the assistant starts: the profile's work_dir (where the code lives), else the profile folder."""
    return C.WORK_DIR if C.WORK_DIR and Path(C.WORK_DIR).is_dir() else C.CONFIG_DIR


def add_dirs(acct):
    """The folders claude may read besides its start folder: the account's config folder (skills, plugins, settings),
    the profile folder and the folder of pl's guide. Never HOME, or a folder that holds HOME."""
    home = Path.home()
    dirs = [harnesses.config_dir(acct), C.CONFIG_DIR, GUIDE.parent]
    return list(dict.fromkeys(str(d) for d in dirs if d and not harnesses.inside(d, home)))


def ask_first(h, acct=None):
    """The harness with its "ask the person" flags after the program name, or SystemExit when pl cannot force that.
    claude's --add-dir takes several values, so it goes first and the next flag ends its list."""
    flags = ASK_FIRST.get(h.name)
    dirs = add_dirs(acct) if h.name == "claude" and flags else []
    flags = ["--add-dir", *dirs, *flags] if dirs else flags
    if not flags:
        raise SystemExit(f"pl: harness {h.name} cannot be started in a mode that asks you before each action, "
                         "so the assistant does not start on it (pick a claude or codex account: [assistant] account)")
    if any(PERMISSION_FLAG_RE.search(t) for t in (*h.interactive, *h.resume)):
        raise SystemExit(f"pl: the [harnesses.{h.name}] template sets its own permission flag; the assistant must ask "
                         "you before each action, so it does not start")
    return dataclasses.replace(h, interactive=[*h.interactive[:1], *flags, *h.interactive[1:]],
                               resume=[*h.resume[:1], *flags, *h.resume[1:]] if h.resume else [])


def ensure():
    """Show the running assistant, or start one: resume the saved conversation when the harness can. A status line."""
    with _locked():
        return _ensure()


def _ensure():
    st = load()
    pane_ = pane(st)
    if pane_:
        if harnesses.pane_command(pane_) in harnesses.SHELLS:
            return "the assistant exited: R starts a new conversation"
        return f"assistant running in {C.TMUX_SESSION}:{WINDOW} ({st.get('account')})"
    acct = account()
    if acct is None:
        raise SystemExit("pl: no harness account is free for the assistant (pl accounts)")
    h = harnesses.account_harness(acct)
    if not h.interactive:
        raise SystemExit(f"pl: harness {h.name} has no interactive template for the assistant")
    h = ask_first(h, acct)
    resume = bool(h.resume and st.get("session_id") and st.get("account") == acct and st.get("harness") == h.name)
    sid = st["session_id"] if resume else str(uuid.uuid4())
    if _run(["tmux", "has-session", "-t", f"={C.TMUX_SESSION}"]).returncode:
        _tmux("new-session", "-d", "-s", C.TMUX_SESSION, "-n", "dispatch", "-c", str(C.WORK_DIR))
    from pl import dispatch   # the launch helpers; imported here so the console's import stays light
    win = _tmux("new-window", "-d", "-t", f"={C.TMUX_SESSION}:", "-n", WINDOW, "-c", str(work_dir()),
                "-e", "DISABLE_AUTO_UPDATE=true", "-e", f"PL_CONFIG_DIR={C.CONFIG_DIR}", "-e", "PL_ASSISTANT=1",
                *dispatch._gh_env_args(), "-P", "-F", "#{window_id}")
    _tmux("set-option", "-w", "-t", win, "automatic-rename", "off")
    pane_ = _tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]
    dispatch._launch(pane_, WINDOW, harnesses.launch_script(h, acct, None if resume else first_prompt(), sid, WINDOW,
                                                           resume=resume))
    _save({"session_id": sid, "account": acct, "harness": h.name, "window": win, "pane": pane_,
           "started_at": now_iso(), "mode": st.get("mode") if resume else "chat"})
    events.emit("assistant_started", None, account=acct, harness=h.name, resumed=resume)
    return f"assistant {'resumed' if resume else 'started'} in {C.TMUX_SESSION}:{WINDOW} ({acct})"


def _covers(rule):
    """True when a permission allow rule would let a pl, gh or git push command run without asking."""
    if rule.strip() in ("Bash", "Bash(*)"):
        return True
    m = re.fullmatch(r"\s*Bash\((.*)\)\s*", rule)
    pat = m.group(1).replace(":*", "*") if m else None
    return bool(pat) and any(fnmatch.fnmatchcase(c, pat) for c in RISKY)


def _allow_rules(path):
    """The permissions.allow strings of one settings file, and nothing else from it."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        allow = d.get("permissions", {}).get("allow", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in allow if isinstance(r, str)] if isinstance(allow, list) else []


def warning():
    """One line when the saved harness may run pl, gh or git push without asking the person, else None. For claude:
    the allow rules that cover them in the account's settings.json and the work folder's .claude settings (pl's ask
    rules still ask for these). Codex: its own approval rules may approve some commands."""
    st = load()
    if st.get("harness") == "codex":
        return "warning: codex's own approval rules may still approve some commands without asking you"
    if st.get("harness") != "claude":
        return None
    files = [C.WORK_DIR / ".claude" / "settings.json", C.WORK_DIR / ".claude" / "settings.local.json"]
    d = harnesses.config_dir(st.get("account"))
    if d:
        files.insert(0, d / "settings.json")
    found = [(r, f) for f in files for r in _allow_rules(f) if _covers(r)]
    if not found:
        return None
    rules = ", ".join(dict.fromkeys(harnesses._typeable(r)[:60] for r, _ in found))
    where = ", ".join(dict.fromkeys(str(f) for _, f in found))
    return mask(f"warning: allow rules {rules} ({where}) would skip the prompt; pl's ask rules still ask for these")


def _type(pane, text, enter=True):
    if text:
        _tmux("send-keys", "-t", pane, "-l", "--", text)
    if enter:
        _tmux("send-keys", "-t", pane, "Enter")


NOT_RUNNING = "pl: the assistant is not running: open the Assistant tab to start it"


def send(text):
    """Type one message into the assistant. A single digit 1-9 goes without Enter: it answers a numbered menu.
    Blank text sends nothing: a bare Enter could accept a menu's default."""
    t = harnesses._typeable(text)
    if not t.strip():
        return
    p = pane()
    if not p:
        raise SystemExit(NOT_RUNNING)
    _type(p, t, enter=not DIGIT_RE.fullmatch(t))


def screen(n):
    """The last n lines of the assistant's screen, masked; [] when it is not running."""
    p = pane()
    return [mask(line) for line in agents.pane_tail(p, n)] if p else []


def reset():
    """End the conversation: close its window (only when it is still the assistant's) and forget the session."""
    st = load()
    if pane(st):
        _run(["tmux", "kill-window", "-t", st["window"]])
    _save({k: v for k, v in st.items() if k in ("account", "harness")} | {"mode": "chat"})
    events.emit("assistant_reset", None)


def _idle(st):
    """True when the saved session is a live harness the session registry marks idle (no registry: never idle)."""
    try:
        h = harnesses.get(st.get("harness") or "claude")
    except SystemExit:
        return False
    rec = agents.registry().get(st.get("session_id")) if h.session_registry else None
    return bool(rec) and rec.get("status") == "idle"


def _proactive():
    return C.ASSISTANT.get("proactive", True) is not False and C.ASSISTANT.get("enabled", True) is not False


def offer(key, title):
    """Proactive mode (on unless [assistant] proactive = false): queue an alert that opened or escalated. The dispatcher
    types the queue as one line with flush_offers(). Alert titles and keys hold ids only. No tmux call."""
    if not _proactive():
        return False
    key, title = KEY_RE.sub("", str(key))[:80], harnesses._typeable(title)[:120]
    with _locked():
        st = load()
        if not st.get("pane"):
            return False
        pending = [x for x in st.get("pending") or [] if isinstance(x, list) and x[:1] != [key]]
        _save({**st, "pending": (pending + [[key, title]])[-5:]})
    return True


def _clear_offers(sent, **extra):
    """Drop the sent offers from the queue, re-reading the state under the lock first (never a stale pane id)."""
    with _locked():
        cur = load()
        _save({**cur, "pending": [x for x in cur.get("pending") or [] if x not in sent], **extra})


def _held(lines):
    """True when typing now could answer a numbered menu or join text the person has not sent."""
    if sum(1 for x in lines if MENU_RE.match(x)) >= 2:
        return True
    typed = [m.group(1) for x in lines if (m := INPUT_RE.match(x))]
    return bool(typed and typed[-1])


def flush_offers(now=None):
    """Once per dispatcher pass: type the queued alerts as one question into an idle assistant, at most one line per
    OFFER_GAP seconds. A menu or unsent text on its screen keeps the queue for the next pass; no assistant drops it."""
    st = load()
    if not _proactive() or not st.get("pending"):
        return False
    now = time.time() if now is None else now
    if now - float(st.get("last_offer_at") or 0) < OFFER_GAP:
        return False
    p = pane(st)
    if not p:
        _clear_offers(st["pending"])      # nobody to tell; the alerts still show on Needs you
        return False
    if not _idle(st) or _held(agents.pane_tail(p, 15)):
        return False
    pending = [x for x in st["pending"] if isinstance(x, list) and len(x) == 2]
    items = [f"{t} ({k})" for k, t in pending]
    head = "" if len(items) == 1 else f"{WORDS[len(items)] if len(items) < len(WORDS) else 'several'} alerts open: "
    _type(p, harnesses._typeable(f"[pl alert] {head}{'; '.join(items)}. Want me to look?"))
    _clear_offers(st["pending"], last_offer_at=now)
    events.emit("assistant_offer", None, alerts=[k for k, _ in pending])
    return True


MODES = {"chat": "[pl mode chat] Back to plain chat: follow the guide's rules and commands.",
         "idea": "[pl mode idea] The person wants to shape an idea. Follow the Idea mode section of the guide."}


def set_mode(mode):
    """Switch this conversation between plain chat and idea mode: tell the harness in one line, and remember it."""
    if mode not in MODES:
        raise SystemExit(f"pl: mode must be one of {', '.join(MODES)}")
    st = load()
    p = pane(st)
    if not p:
        raise SystemExit(NOT_RUNNING)
    _type(p, MODES[mode])
    with _locked():
        _save({**load(), "mode": mode})
    events.emit("assistant_mode", None, mode=mode)


# ---------- pl assistant ----------

def _idea_save(a):
    """Draft or update an idea brief in the Ideas tab's own storage, and print exactly what filing would send."""
    try:
        raw = json.loads(a.brief)
    except ValueError as e:
        raise SystemExit(f"pl assistant idea save: --brief is not valid JSON ({e})")
    if not isinstance(raw, dict):
        raise SystemExit("pl assistant idea save: --brief must be a JSON object")
    if a.id:
        idea = ideas.load(a.id)
        if idea.get("status") != "interviewing":
            raise SystemExit(f"pl assistant idea save: idea {a.id} is {idea.get('status')}; save a new one instead")
        if a.title:
            idea["title"] = ideas.clean_title(a.title)
    else:
        idea = {**ideas.new_idea(title=a.title, account=load().get("account")), "source": "assistant"}
    idea["brief"] = ideas.clean_brief(raw, idea["brief"])
    idea.update(asked=True, question=None, ready_to_file=False)   # written in the chat; a change needs a new yes
    ideas.save(idea)
    events.emit("assistant_idea_saved", None, idea_id=idea["id"])
    print(f"idea {idea['id']}  {idea['title']}\n")
    print(ideas.markdown(idea["brief"]))
    print("clear: pl assistant idea file " + idea["id"] + " files it after the person's yes" if ideas.is_clear(idea)
          else "not clear yet: open questions remain")


def _idea_file(a):
    """Mark a clear draft "ready to file". It writes nothing to the board: the person files it in the pl console
    (the Assistant tab's ctrl+f, or the Ideas tab's A), after a confirm outside the harness."""
    idea = ideas.load(a.id)
    if idea.get("status") != "interviewing" or not ideas.is_clear(idea):
        raise SystemExit(f"pl assistant idea file: idea {a.id} is {idea.get('status')} and "
                         f"{'clear' if ideas.is_clear(idea) else 'not clear yet'}; only a clear draft can be filed")
    ideas.save({**idea, "ready_to_file": True})
    if os.environ.get("PL_ASSISTANT"):
        events.emit("assistant_action", None, command="assistant idea file", idea_id=idea["id"])
    print(f"idea {idea['id']} is ready to file: the person presses ctrl+f in the Assistant tab to file it")


def ready_idea():
    """The newest draft the assistant marked ready to file, or None."""
    return next((i for i in ideas.list_ideas() if i.get("status") == "interviewing" and i.get("ready_to_file")
                 and ideas.is_clear(i)), None)


def cmd_assistant(a):
    if getattr(a, "assistant_cmd", None) == "idea":
        return (_idea_save if a.idea_cmd == "save" else _idea_file)(a)
    if getattr(a, "assistant_cmd", None) == "log":
        events.emit("assistant_log", None, message=mask(a.text)[:LOG_CHARS])
        print("logged")
        return
    print('usage: pl assistant log "text" | idea save ... | idea file <idea-id> (the Assistant tab runs it)')

