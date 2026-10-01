# pl

**To install:** open Claude Code in a terminal and say: "Install https://github.com/erickcastrillo/pl on my computer; follow its INSTALL.md and AGENTS.md." Claude asks you a few questions and hands you the sign-ins; it never signs in for you or reads your credentials.

To install by hand, follow [INSTALL.md](INSTALL.md): check the prerequisites, sign in to Claude Code and GitHub, clone, `uv tool install`, then `pl setup`.

pl is a terminal console and dispatcher for an idea-to-PR funnel. A card moves through spec, plan, build and pull request. pl starts a coding agent for each stage, headless, in a tmux window. You approve at gates: the spec (optional), the plan, and the merge.

Agents run through the Claude Code, Codex or Antigravity command-line tools (Antigravity is experimental).

## Features

All of these are on by default. The console lists new ones once after an upgrade; `?` in the console or `pl whatsnew` shows them again.

| Feature | Where to see it | Try |
| --- | --- | --- |
| Standup panel with Slack copy | Dashboard: `s`, then `y` | `pl standup --slack` |
| Token spend per card, account, model and loop | Dashboard Health; Loops tab | `pl usage --by card` |
| Loop context care: an idle loop over 80% context restarts fresh | Loops tab: context | `pl usage --by loop` |
| Alerts that open, remind and clear themselves | Needs you: ALERTS | `pl alerts --all` |
| A "doing now" line for each live agent | Needs you and Pipeline | `pl watch` |
| Move a live agent to another account in place | on a usage limit; Activity tab | `pl move-agent <card> <account>` |
| One machine manager for every profile's dispatcher | Dashboard: machine line; `D` | `pl manager status` |
| Claude's weekly limit detected; the account parks until its reset | header: accounts | `pl accounts` |
| Memory guard: low memory holds new agents; a runaway agent is stopped | Dashboard: memory line | `pl manager status` |
| One dispatcher per profile | Activity tab | `pl profiles` |
| Start a failed agent fresh | Needs you | `pl retry all` |

## Your subscription

pl runs the harness CLIs you already installed and signed in to, on your own subscription. It never asks for or stores an API key. pl adds no subscription and needs no model API key. If `claude`, `codex` or `agy` does not work in your terminal, pl cannot use it either. pl never reads a harness's credential files.

## Install

macOS or Linux, with git, tmux, gh, uv and a harness CLI. [INSTALL.md](INSTALL.md) has the install commands, the sign-ins, the exact `pl setup` flags, how to check it works, how to uninstall, and security notes.

`pl setup` asks for a profile name, your harnesses, where cards live, your GitHub sign-in and the work folder, then writes and checks `~/.pl-<name>/config.toml`. `pl setup --yes` takes every answer from flags (`pl setup --help`); a missing answer exits 2 naming its flag. It never installs a harness or signs in for you. Bare `pl` in a terminal opens the console.

## Profiles

A profile is one folder, `~/.pl-<name>`, holding `config.toml`, state and logs. Pick one with `pl --profile NAME` or `PL_CONFIG_DIR=/path pl ...`. Without a profile, most commands refuse to run.

```
pl profiles new work            # writes ~/.pl-work/config.toml (spec gate on), prints an alias
alias pl-work='PL_CONFIG_DIR=~/.pl-work pl'
pl profiles                     # every profile and whether its dispatcher runs
```

Other ways to create one (`pl setup` is the usual one):

- `--from-current` copies the profile you are running as.
- `--from-legacy` imports the settings of an older one-file pl script (`--legacy-script`, `--mcp-json`, `--server` adjust what it reads).
- `--github-project create --owner OWNER [--title T]` creates a GitHub Project as the tracker.

Each profile has its own lock, state folder and tmux session (default `pl-<name>`, or `tmux_session` in the config). Starting the dispatcher of a running profile is refused with exit code 3. Watching it is fine. Two profiles that share a harness account, a tracker board or a tmux session get a warning, not an error.

## Connections

The tracker (`[tracker]`) holds the cards. `type` is `mcp`, `github-project` or `github-issues`. An optional `[intake]` table is a second board whose cards assigned to you become funnel ideas.

### MCP tracker

Any MCP server can be the board once you map pl's card actions to its tools. Point pl at the harness's own MCP file so no keys are copied:

```toml
[tracker]
type = "mcp"
mcp_config = "~/project/.mcp.json"   # JSON file with {"mcpServers": {...}}
server = "boards"                    # the server's name in that file
board_id = "YOUR-BOARD-ID"
# plus one [tracker.tools.<action>] table per action
```

The full mapping, a worked example and troubleshooting are in [docs/mcp-trackers.md](docs/mcp-trackers.md).

### GitHub

```toml
[tracker]
type = "github-project"          # or "github-issues" with repo and label_prefix
owner = "your-org"
number = 3
status_field = "pl stage"
repo = "your-org/your-repo"      # where new cards are opened as issues
```

Both use the `gh` CLI and its login (`gh auth login`, plus `gh auth refresh -s project` for Projects). pl stores no GitHub token. Setup, the stage field, pull request labels and a first run are in [docs/github.md](docs/github.md).

## Other settings

```toml
tmux_session = "pl-work"

[accounts.main]                 # a harness account: its config folder
config_dir = "~/.claude-main"   # harness = "claude" (default), "codex" or "agy"

[stages.spec]                   # spec, design, plan, run
prompt = "Write the spec for card {id}."   # optional per stage: harness, account
[loops.review]                  # long-lived agents kept alive in their own tmux window
prompt = "..."
account = "main"

[gates]
spec = true                     # a person approves the spec before the plan starts

[dispatch]
max_runs = 3
max_prep = 2
interval = 120
autostart = true                # the console starts this dispatcher in tmux; D starts or stops it by hand

[code_host]                     # PR labels pl reads (set by your review tools)
owner = "your-org"
repos = ["api", "web"]          # optional: pl standup counts only these repos' PRs (default: every repo of owner)
labels = { review = "...", ready = "...", merge_ready = "...", rework = "...", failed = "..." }

[intake]
columns = ["Triage", "Backlog"]
repo_tags = ["api", "web"]      # card tags that name a repo

[paths]
work_dir = "~/code"
attention_cmd = "notify-me"     # notifications; unset means none
```

Every key can also be edited in the console's Settings tab. Custom harnesses go under `[harnesses.<name>]` with `bin`, `interactive` and `headless`.

## One manager per machine

The manager is on by default: every console starts it when it is not running. It starts each profile's dispatcher (those with `[dispatch] autostart` not false), restarts one that exits, and caps agents across every profile. A dispatcher that already runs for a profile is adopted, never started twice.

```
pl manager start          # start it by hand; a console does this for you
pl manager status         # each profile's dispatcher, live agents, any hold
pl manager stop [--all]   # --all also stops the dispatchers
pl manager restart NAME   # start a profile's dispatcher again after the manager gave up on it
pl manager stop NAME      # stop one profile's dispatcher; the manager leaves it alone until restart
```

The settings live in `~/.local/state/pl-machine/machine.toml`, which `pl setup` and `pl manager start` create. Without the file the defaults apply:

```toml
[manager]
enabled = true               # false: each console starts its own profile's dispatcher instead of the manager

[limits]
# max_live_agents = 12       # spec/design/plan/run agent windows across every profile; more hold new starts.
                             # Unset: every managed profile's max_runs + max_prep added up, at least 8
max_agents_memory = "60%"    # past it the largest agent tree is stopped; over 80% of it holds new starts
min_free_memory = "15%"      # less free memory holds new starts
kill_runaway = true          # false: only notify
```

A usage limit hit by one profile parks that account folder for every profile that uses it. A hold stops new agents only: crashed agents and loops are still restarted. To go back to one dispatcher per console, run `pl manager stop`, then set `[manager] enabled = false` in `machine.toml`.

## Guides

- [Use pl with GitHub](docs/github.md): Projects or Issues as the tracker, pull requests, a first run.
- [Use any MCP board server as the tracker](docs/mcp-trackers.md): the tools mapping, security, troubleshooting.
- [One dispatcher per profile](docs/dispatcher.md): the single-dispatcher check and keeping pl out of tmux session restore.

## The card contract

A card's description is split into sections. Each starts with a line of the form `# PIPELINE: <SECTION>`.

| Section | Holds |
| --- | --- |
| INPUT | the idea and inlined documents (spec review notes are appended here) |
| SPEC | the spec |
| DESIGN | the UI design spec, for cards tagged `frontend` |
| PLAN | the plan |
| REVIEW NOTES | your plan rejections, read by the planner |

Columns: Inbox, Spec ready, Plan for review, Manual, Approved, In progress, PR open, Done. Inbox runs the spec stage, Spec ready the plan stage, Approved and In progress the build stage. Manual has no agent.

Metadata keys pl reads or writes: `pipeline_mode` (`"auto"` marks funnel cards), `profile` (the harness account name), `worker` (the running agent), `product_card` (the source card on the intake board), `plan_path`, `spec_approved_at`, `idea_id`, `pr_urls`.

## Console

`pl watch` opens the console (`--plain` prints a text frame, `--once` prints one frame and exits).

| Key | Action |
| --- | --- |
| 0 | Assistant (the leftmost tab): a live harness session that runs pl for you; ctrl+t switches chat and idea mode, ctrl+o opens its window, ctrl+r starts over, ctrl+f files an idea it marked ready |
| 1 to 9 | Dashboard, Needs you, Ideas, Pipeline, Pull requests, Loops, Activity, Settings, Background |
| w | cycle the Dashboard time window |
| s | standup summary of the last 24 hours on the Dashboard (y copies it) |
| a / x | approve / send back the selected card |
| enter | open review on Needs you |
| e | open in `$EDITOR` (review screen) |
| D | start or stop this profile's dispatcher (through the manager when it runs it; `pl manager stop` stops the manager) |
| r | refresh |
| ? | what's new (also in the ctrl+p palette) |
| q | quit |

Other commands: `pl setup`, `pl idea`, `pl list`, `pl review`, `pl approve`, `pl reject`, `pl dispatch`, `pl pull`, `pl adopt`, `pl done`, `pl move`, `pl retry`, `pl board init`, `pl card`, `pl pause`, `pl resume`, `pl accounts`, `pl intent`, `pl standup`, `pl usage`, `pl alerts`, `pl move-agent`, `pl manager`, `pl whatsnew`. Each has `--help`. `pl --version` prints the installed version.

Every Claude loop restarts fresh when it is idle above 80% of its context window. `[loops.<name>] max_context = 60` sets another percent; `0` turns it off.

## Permissions

Pipeline agents and loops run in each harness's unattended mode, so an agent nobody is watching does not sit idle at a permission prompt. pl never uses a yolo, bypass or skip-all-permissions mode.

| Agent | Mode |
| --- | --- |
| Spec, design, plan and run agents | Claude `--permission-mode auto`; Codex `--ask-for-approval never --sandbox workspace-write`; Antigravity `--mode accept-edits`; Gemini `--approval-mode auto_edit` |
| Loops | the same as pipeline agents |
| Assistant | always asks first (see Assistant); never changed by this setting |
| Sub-agents | inherit the mode of the agent that started them |

What auto allows. Claude's auto mode lets a safety classifier approve routine actions, such as edits, tests and builds, and block risky or suspicious ones, which then ask or fail. Codex never asks, and commands may write only inside the workspace; a blocked command fails back to the agent. Antigravity and Gemini approve file edits only: their only mode that never asks skips every check, so their agents still ask before commands. pl warns about this in `pl setup` and in the Settings tab. To let them run, allow the commands in agy's own settings, or with a Gemini policy file (`--policy`).

Turn it off, so agents ask before each action:

```toml
[permissions]
unattended = false     # every harness in this profile

[harnesses.codex]
unattended = false     # one harness only
```

A `[harnesses.<name>]` template that already sets its own permission or sandbox flag is left as it is.

An agent that still waits at a permission prompt, with no change on its screen for 2 minutes, opens an alert: "agent waiting for permission in <window>: <the tool line>". Its card shows "waiting for permission". pl never answers the prompt. The alert clears once the prompt is gone.

## Assistant

Tab `0` is a side chat that runs pl for you: "what is stuck?", "retry that card", "install this plugin", "change the review skill". It is a real, interactive harness session in the tmux window `assistant` of this profile's session, started the first time you open the tab. The tab shows its screen and types what you enter; a lone digit answers the harness's numbered permission menu. The window outlives the console, so reopening the console reattaches to the same conversation; if tmux lost the window, pl resumes the saved Claude conversation.

It uses `[assistant] account`, else the first account not parked, and its usage counts on that account. It starts in the profile's `work_dir`, where the code lives (the profile folder when that folder is missing). pl always starts it in the harness's ask-first mode, whatever the account's own default mode is: Claude with `--permission-mode manual`, Codex with `--ask-for-approval on-request --sandbox read-only`. Claude also gets `--add-dir` for the account's config folder, the profile folder and the folder of pl's guide (never HOME), and permission rules through `--settings`. Claude Code checks deny, then ask, then allow:

- **Runs without asking:** reading, listing and searching files, and `pl list`, `pl card`, `pl alerts`, `pl usage`, `pl standup`, `pl manager status`, `pl accounts`, `pl whatsnew`, `git status`, `git log`, `git diff`, `gh pr view`, `gh pr list`.
- **Always asks**, even when an allow rule in a settings file covers it: every other `pl` command, `pl alerts --ack`, `pl accounts --reset`, `pl card --delete`, every other `gh` command, `git push`, and `git diff`/`git log` with `--output`. Edits and any other command ask too, through manual mode.
- **Denied:** reading credential files (`.credentials*`, `auth.json`, `.env*`, `*.pem`, `id_rsa*`, `id_ed25519*`, `.netrc`, gh's `hosts.yml`) and the macOS keychain command `security`.

The tab shows one warning line naming any allow rule in the account's or work folder's settings that covers `pl`, `gh` or `git push`. Codex gets a warning that its own rules may still approve some commands. A harness pl cannot start that way, or a `[harnesses]` template that sets its own permission, allowed-tools or settings flag, is refused. Its guide (shipped with pl) tells it to wait for your yes before approving, moving, merging or filing anything, to back up and show a diff before editing a skill or config file, never to read credentials, and to treat card, PR and command output as data, never as instructions. Pipeline commands it runs leave an `assistant_action` event in Activity, and changes outside pl leave an `assistant_log` event. Both are a best-effort record, not a guard.

```toml
[assistant]
enabled = true      # false hides the tab
account = "work"    # optional; default: the first account not parked
proactive = true    # false: pl stops offering new alerts to an idle assistant
```

Each conversation is in chat mode (ask pl to do things) or idea mode (ctrl+t). In idea mode the assistant shapes an idea brief with you one question at a time, and it may suggest the switch when a request sounds like something new. It saves the draft with `pl assistant idea save`, so the draft also shows on the Ideas tab. After you say yes to the exact text, it runs `pl assistant idea file`, which only marks the draft ready and writes nothing to the board. The tab then shows "Idea ready: <title>"; ctrl+f asks you once more and files it the same way as the Ideas tab's `A` (which files it too). Both tabs share `<profile>/state/ideas/`, and a draft interviewed in the Ideas tab can be continued in idea mode by its id (`pl assistant idea save --id <id>`).

With `proactive` on (the default), new or escalated alerts are typed into an idle assistant as one line per dispatcher pass, at most once a minute: `[pl alert] <title> (<key>). Want me to look?`, or "two alerts open: ..." for several. Alert titles hold ids only. pl waits while the assistant shows a numbered menu or text you have not sent yet. A busy assistant gets the line on a later pass; a closed one gets nothing, and the alert still shows on Needs you.

## Standup

`pl standup` prints a short summary of the last 24 hours, ready to paste in a chat standup: PRs merged, opened and closed without merging; ideas added, specs and plans written and approved, cards sent back and done; cards per column and agents running now; what needs you; and errors, if any. Each line has up to five titles under it, never card text.

`--since` takes `24h`, `90m`, `7d`, a date (`2026-09-29`) or a local time (`2026-09-29T09:00`). `--markdown` prints Markdown for docs, `--slack` Slack formatting. PR numbers cover every PR in the `[code_host]` owner's repos (or its `repos`), with how many merged ones are yours (authored by or assigned to you); they come from three GitHub searches. When GitHub cannot answer, the line says `PRs: unavailable` and why.

## License

MIT.
