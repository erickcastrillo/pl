"""What's new: one entry per feature, oldest first. The console shows the entries newer than the last one it showed
(kept in <profile>/state/whatsnew.json) once after an upgrade; ? or the palette shows them all; `pl whatsnew` prints them.
A new feature adds an entry here with a key greater than every other one."""
import json
import os

from pl import config as C


def _e(key, title, where, try_):
    return {"key": key, "title": title, "where": where, "try": try_}


ENTRIES = [
    _e("2026-09-30.01", "Standup panel with Slack copy", "Dashboard: press s; y copies it for Slack", "pl standup --slack"),
    _e("2026-09-30.02", "Token spend per card, account, model and loop", "Dashboard Health: tokens; Loops tab: tokens 1h",
       "pl usage --by card"),
    _e("2026-09-30.03", "Loop context care: an idle loop over 80% context restarts fresh",
       "Loops tab: context; [loops.<name>] max_context (0 = off)", "pl usage --by loop"),
    _e("2026-09-30.04", "Alerts that open, remind and clear themselves", "Alerts tab (press !); k acknowledges",
       "pl alerts --all"),
    _e("2026-09-30.05", "A doing now line for each live agent", "Needs you and Kanban: under each working card", "pl"),
    _e("2026-09-30.06", "Move a live agent to another account in place", "done by itself on a usage limit; Activity tab",
       "pl move-agent <card> <account>"),
    _e("2026-09-30.07", "The machine manager runs every profile's dispatcher, on by default",
       "Dashboard: the machine line; D stops or starts this profile's dispatcher", "pl manager status"),
    _e("2026-09-30.08", "Claude's weekly limit is detected; the account parks until its reset date",
       "header: PARKED until <date>", "pl accounts"),
    _e("2026-09-30.09", "Memory guard: new agents wait when memory is low; a runaway agent is stopped",
       "Dashboard: the memory line", "pl manager status"),
    _e("2026-09-30.10", "One dispatcher per profile: a second start is refused", "Activity tab: dispatcher_started",
       "pl profiles"),
    _e("2026-09-30.11", "pl retry starts a card's failed agent fresh", "Needs you: cards whose agent died",
       "pl retry <id|all>"),
    _e("2026-09-30.12", "The Assistant: a pl-aware Claude session you chat with", "tab 0 Assistant",
       "open it and ask \"what is stuck?\""),
    _e("2026-09-30.13", "Pipeline agents and loops run in auto mode",
       "an agent stuck at a permission prompt opens an alert; README: Permissions", "pl alerts"),
    _e("2026-09-30.14", "Skills: view, edit, create and share every account's skills",
       "Settings tab, Skills section (ctrl+p \"Skills\"): e edit, n new, d delete, S share, l link",
       "pl skills list"),
    _e("2026-09-30.15", "Update check: pl says when a newer release exists (it never installs it)",
       "a notice at console start; U shows the update commands, c copies them", "pl update --check"),
    _e("2026-09-30.16", "Built-in stage skills: pl-spec, pl-design, pl-plan, pl-run and pl-review",
       "new profiles use them; edit them in Settings, Skills (library); pl skills reset NAME puts one back",
       "pl setup --use-builtin-stages"),
    _e("2026-10-01.01", "pl restarts itself after an update; D offers restart or stop",
       "the manager and dispatchers pick up a new install within a minute (the first time: open a new console, or "
       "pl manager stop then pl manager start, once); D: r restart, s stop", "pl manager status"),
    _e("2026-10-01.02", "The Assistant tab is a chat; its questions show as a card a number key answers",
       "tab 0 Assistant: turns, short tool lines, the status line; it restarts on a pl update when idle and unwatched",
       "open it and ask \"what is stuck?\""),
    _e("2026-10-02.01", "Alerts and Pipeline have their own tabs; the board is now Kanban",
       "! Alerts · @ Pipeline · 4 Kanban", "pl alerts"),
    _e("2026-10-02.02", "Optional local model (Gemma 4 on Ollama) writes a short standup summary",
       "pl setup offers it on a new profile; [local_model] enabled = true; loopback url only, no API key", "pl standup --summary"),
]


def _path():
    return C.STATE_DIR / "whatsnew.json"


def last_shown():
    """The key of the newest entry shown, or None (never shown, or a file that does not read)."""
    try:
        v = json.loads(_path().read_text()).get("last")
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return v if isinstance(v, str) else None


def unseen():
    last = last_shown()
    return [e for e in ENTRIES if last is None or e["key"] > last]


def mark_seen(key=None):
    """Remember key (default: the newest entry) as shown. A state folder that cannot be written is not an error."""
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        tmp = _path().with_name(f".whatsnew.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"last": key or ENTRIES[-1]["key"]}))
        os.replace(tmp, _path())
    except (OSError, TypeError):
        pass


def text(entries):
    return "\n\n".join(f"{e['title']}\n  where: {e['where']}\n  try:   {e['try']}" for e in entries)


def cmd_whatsnew(_argv=None):
    print("What's new in pl\n\n" + text(ENTRIES))
    return 0
