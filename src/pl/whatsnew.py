"""What's new: one entry per feature, oldest first. The console shows the entries newer than the last one it showed
(kept in <profile>/state/whatsnew.json) once after an upgrade; the palette shows them all; `pl whatsnew` prints them.
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
    _e("2026-09-30.04", "Alerts that open, remind and clear themselves", "Alerts tab (press 6); k acknowledges",
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
    _e("2026-10-02.03", "pl commands take a GitHub card's short id: repo#43 or #43",
       "pl card, retry, review, approve, reject, done, move; hints show the id quoted (quote it in a shell: '#' starts a comment)",
       "pl card '#43'"),
    _e("2026-10-02.04", "pl says who stopped a dispatcher; D then s asks first", "header and pl manager status",
       "pl manager status"),
    _e("2026-10-03.01", "A folder trust prompt shows on the card and raises an alert",
       "Needs you: waiting: trust the folder; pl never answers it", "pl alerts"),
    _e("2026-10-03.02", "pl uses about 20× fewer GitHub points; profiles on one GitHub account share a fair budget",
       "pl usage --github", "pl usage --github"),
    _e("2026-10-03.03", "Profiles no longer block each other: each gets its own agent share", "pl manager status",
       "pl manager status"),
    _e("2026-10-05.01", "Needs you and Kanban are gone: the Pipeline tab lists every card, a red ! on those that need you",
       "tab 3 Pipeline; n shows only the cards that need you; tab keys are digits 0-9 (0 Dashboard, 1 Assistant, 2 Ideas, 4 Pull requests, 5 Loops, 6 Alerts)", "pl"),
    _e("2026-10-05.02", "Drop a card no longer needed, and undo it: it goes to Done marked dropped, its agent stopped",
       "Pipeline: d drops (asks for a reason), z shows Done, u undoes a drop", "pl drop <id> --reason TEXT"),
    _e("2026-10-05.03", "More Pipeline keys: hand off to Manual, mark done, edit the idea",
       "Pipeline: h to Manual, f done (both ask first), e edits the card's INPUT in $EDITOR", "pl done <id>"),
    _e("2026-10-05.04", "A parked account is checked and comes back as soon as it works; its loops run elsewhere",
       "pl accounts: last check and next check; header: checked; alert when a loop misses 2 of its fires; "
       "[dispatch] account_checks, [loops.<name>] fallback = false", "pl accounts"),
    _e("2026-10-05.05", "A run agent waiting 30 min is stopped to free its place; the card shows blocked",
       "Pipeline: a red ! with blocked: <reason> · retry HH:MM; [dispatch] release_waiting_after (minutes, 0 = off)",
       "pl retry <id>"),
    _e("2026-10-05.06", "Restart, hold or adopt a card from the Pipeline; agents stuck on an API error restart",
       "Pipeline: R restarts its agent, p holds or releases it, i hands a manual card to the pipeline (all ask first)",
       "pl restart <id>, pl hold <id> --reason TEXT"),
    _e("2026-10-05.07", "Antigravity agents start again; a card whose agent died says why",
       "Pipeline: <stage> agent died 3x with its last error line; t or pl retry starts it fresh", "pl list"),
    _e("2026-10-07.01", "Spread a stage over several accounts, of any harness, to use each subscription",
       "[stages.<stage>] accounts = [\"main\", \"agy\"]; Settings shows the pool", "pl accounts"),
    _e("2026-10-08.01", "Antigravity's quota is detected: the account parks until \"Resets in\" and its slots free up",
       "pl list: limit hit; finished Antigravity spec/design/plan windows close by themselves", "pl accounts"),
    _e("2026-10-08.02", "Pick the model and reasoning effort per account, and the effort per stage",
       "Settings: Models and effort, and <stage> effort", "[accounts.main] model = \"opus\", effort = \"high\""),
    _e("2026-10-09.01", "The console restarts itself on a new pl install, like the dispatchers",
       "a notice and N when a dialog or unsent text is open", "pl"),
    _e("2026-10-09.02", "An idle Antigravity run agent frees its run slot, like Claude's",
       "pl list: run agent waiting (idle N min); the next card starts", "pl list"),
    _e("2026-10-09.03", "An Antigravity run window closes once its card reaches PR open and its screen goes quiet",
       "the dispatcher window: closing finished run agent window", "pl list"),
    _e("2026-10-09.04", "In progress cards whose agent must start again get run slots before Approved cards",
       "the dispatcher window: In progress -> run agent", "pl list"),
    _e("2026-10-09.05", "A refused agent move names the agent's real harness (Antigravity, Codex)",
       "Activity tab: agent_move_refused", "pl move-agent <card> <account>"),
    _e("2026-10-09.07", "Agents keep pull requests small",
       "plans aim at the auto-merge size and split work over the PR limit, and an oversized run goes to Manual "
       "instead of opening a PR; [code_host] pr_max_lines (300), pr_max_files (10), pr_auto_merge_lines (150), "
       "pr_auto_merge_files (6)", "pl section <id> PLAN"),
    _e("2026-10-09.08", "Claude's read outside the working directories prompt opens an alert",
       "Alerts tab: agent waiting for permission in <window>: Read(<path>)", "pl alerts"),
    _e("2026-10-11.01", "One worktrees folder per profile: agents read and write their worktrees without asking",
       "Settings, General: worktrees dir; [paths] worktrees_dir (default <work_dir>-pl-worktrees); "
       "{worktrees_dir} in a stage prompt", "pl"),
    _e("2026-10-11.02", "Closing the console while the Assistant tab reads its screen no longer ends in a crash",
       "Assistant tab", "pl"),
    _e("2026-10-13.01", "Auto-rework: a PR the review labels for rework goes back to its run agent, which fixes it",
       "Pipeline: the card returns to In progress, then PR open; after [dispatch] rework_rounds (3) the PR waits "
       "for you with the failed label", "pl rework --dry-run"),
    _e("2026-10-23.01", "A built-in local board and the pl board protocol",
       "pl setup: the default board for a new non-code pipeline", "pl board check local"),
    _e("2026-10-23.02", "pl inbox: every gate waiting on you, in every profile",
       "CLI: pl inbox, or pl inbox --json", "pl inbox"),
    _e("2026-10-23.03", "Kill switch: hold every outward action across profiles",
       "pl kill-switch on pauses runs with outward actions; pl kill-switch off releases them",
       "pl kill-switch status"),
    _e("2026-10-23.04", "Gate messages in chat: approve, edit or send back directly from your phone",
       "manager posts waiting gates to Telegram/chat with Approve and Send back buttons", "pl manager status"),
    _e("2026-10-23.05", "Send back notes and /stop guard from chat",
       "chat approvals: reply with notes to send back, /stop pauses outward actions", "pl"),
    _e("2026-10-23.06", "Discord chat connector: approve, edit and send back gates from Discord",
       "machine.toml [connectors.discord]; buttons and Send back modal", "pl chat test discord"),
    _e("2026-10-24.01", "Exit codes for scripts: 0 done, 1 failed, 2 bad input, with a one-line reason",
       "every pl command; an unknown card or column now exits 2 (it was 1)", "pl card nosuch; echo $?"),
    _e("2026-10-25.01", "The footer shows the actions that fit the selected card, and ? opens help for the screen",
       "Pipeline: approve and send back show only on a spec or plan waiting for you, p reads hold; ? is help, "
       "What's new is in the palette (ctrl+p)", "pl"),
    _e("2026-10-25.02", "Link cards across pipelines", "pl card shows links",
       "pl link <id> engineering:<id> --relation built-for"),
    _e("2026-10-25.03", "Triggers: matching mail becomes a card in its pipeline",
       "[triggers.<name>] in profile; runs in dispatcher pass", "pl triggers list"),
    _e("2026-10-25.04", "Agents ask you questions; pl answer continues the stage",
       "Pipeline: questions show in pl inbox and in chat; the owner's answer continues the stage",
       "pl answer <id> \"text\""),
    _e("2026-10-25.05", "Home across pipelines, ctrl+k switcher and new Settings panels",
       "Home tab; ctrl+k; Settings", "pl pipelines list"),
    _e("2026-10-25.06", "Connectors whose tools change lose write access until you approve them",
       "Alerts tab; pl connectors approve-tools <name>", "pl connectors approve-tools <name>"),
    _e("2026-10-25.07", "pl ask: central read-only brain answers with checked sources",
       "ask questions across every pipeline and connector; every fact links a source", "pl ask \"what needs me today?\""),
    _e("2026-10-25.08", "Chat answers questions via the brain with checked source links",
       "Chat: plain questions and 'remember this' answered when [brain] chat = true",
       "send a question in chat"),
    _e("2026-10-25.09", "Bootstrap fresh worktrees via .pl/worktree.yaml",
       "fresh worktrees bootstrapped by pl-run; README: Worktrees folder", "pl worktrees bootstrap <path> --dry-run"),
    _e("2026-10-25.10", "Plan cards: pl idea --plan N --slice M titles the card 'Plan N slice M: ...'",
       "pl idea", "pl idea \"...\" --plan 3 --slice 2 --title \"...\""),
    _e("2026-10-25.11", "pl titles check lists plan cards whose 'Plan N slice M' title and metadata disagree",
       "pl titles check", "pl titles check --json"),
    _e("2026-10-25.12", "Plans say what they depend on; pl depends --from-plans records it",
       "pl-plan skill, pl depends", "pl depends --from-plans --dry-run"),
    _e("2026-10-25.13", "Plans split work into slices that merge in any order",
       "pl-plan: each later slice says independent or after <slice>; pl-review labels a PR for rework when it "
       "depends on another unmerged PR", "pl section <id> PLAN"),
    _e("2026-10-25.14", "A window that only runs a shell, or one pl did not start, is not counted as an agent",
       "pl closes its stage windows left holding a shell; a restored window running something else (a resume picker) "
       "is left open and opens one alert", "pl manager status"),
    _e("2026-10-25.15", "pl e2e: your repo's Playwright tests in a throwaway Multipass VM",
       "set up from the repo's .pl/e2e.yaml; ~/pl-e2e/<repo>/<time>/report.md: each failing test with file:line, its trace, video and screenshot; "
       "pl e2e --help. pl-plan now plans Playwright tests per card and pl-run builds them", "pl e2e --keep-vm"),
    _e("2026-10-25.16", "Pods: rules, knowledge and skills per team",
       "pl pods show NAME --json adds approval rate, edits per draft and escalations over 30 days; pod.md versions, "
       "rule add, rollback and rules suggested from repeated edits", "pl pods show engineering"),
    _e("2026-10-25.17", "pl titles check --fix repairs drift; next and set number plan cards",
       "pl titles next, pl titles set", "pl titles check --fix --dry-run"),
    _e("2026-10-25.18", "Small changes run on a cheaper model",
       "[stages.run.small] in config.toml: a run agent whose approved PLAN budget is at or under max_lines and max_files "
       "starts on the small model for its harness; rework rounds use the normal one",
       "[stages.run.small] max_lines = 60"),
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
