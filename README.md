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
| Alerts that open, remind and clear themselves | Alerts tab (6) | `pl alerts --all` |
| Every card in a list by column, with its full text; a red ! on the cards that need you | Pipeline tab (3); `n` shows only those | `pl list` |
| A "doing now" line for each live agent | Pipeline tab | `pl watch` |
| Move a live agent to another account in place | on a usage limit; Activity tab | `pl move-agent <card> <account>` |
| One machine manager for every profile's dispatcher | Dashboard: machine line; `D` | `pl manager status` |
| Claude's weekly limit detected; the account parks until its reset | header: accounts | `pl accounts` |
| Memory guard: low memory holds new agents; a runaway agent is stopped | Dashboard: memory line | `pl manager status` |
| One dispatcher per profile | Activity tab | `pl profiles` |
| Start a failed agent fresh | Pipeline tab: `t` | `pl retry all` |
| Drop a card no longer needed, and undo it | Pipeline tab: `d`, then `z` and `u` | `pl drop <id> --reason TEXT` |
| Hand a card to Manual, mark it done, edit its idea | Pipeline tab: `h`, `f`, `e` | `pl done <id>` |

## Your subscription

pl runs the harness CLIs you already installed and signed in to, on your own subscription. It never asks for or stores an API key. pl adds no subscription and needs no model API key. If `claude`, `codex` or `agy` does not work in your terminal, pl cannot use it either. pl never reads a harness's credential files.

## Install

macOS or Linux, with git, tmux, gh, uv and a harness CLI. [INSTALL.md](INSTALL.md) has the install commands, the sign-ins, the exact `pl setup` flags, how to check it works, how to uninstall, and security notes.

`pl setup` asks for a profile name, your harnesses, where cards live, your GitHub sign-in and the work folder, then writes and checks `~/.pl-<name>/config.toml`. For a new profile it also offers the optional local model (Gemma 4 on Ollama, see [Local model](#local-model-optional)), yes by default. `pl setup --yes` takes every answer from flags (`pl setup --help`); a missing answer exits 2 naming its flag. It never installs a harness or signs in for you. Bare `pl` in a terminal opens the console.

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
fallback = true                 # while main is parked, run on another account of its harness; false: wait for main

[gates]
spec = true                     # a person approves the spec before the plan starts

[dispatch]
max_runs = 3
max_prep = 2
interval = 120
autostart = true                # the console starts this dispatcher in tmux; D starts or stops it by hand
account_checks = [10, 30, 60]   # minutes between health checks of a parked account; the last repeats; [] turns them off

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

[updates]
check = true                    # once a day, read pl's release tags on GitHub; false (or PL_NO_UPDATE_CHECK=1) turns it off
```

**Updates.** Once a day the console and the dispatcher read pl's release tags from https://github.com/erickcastrillo/pl with `git ls-remote`. Nothing is sent and no sign-in is used. When a newer release exists, the console says so and `U` shows the update commands; `pl update` prints them. pl never installs an update by itself. After you install one, pl restarts itself: within seconds the manager starts again on the new version, and each dispatcher does the same at the end of its pass (running agents and loops keep running; a dispatcher you stopped stays stopped). A new install that does not import is reported once and the old version keeps running. The first time, from a pl older than this feature, open a new console once (or run `pl manager stop`, then `pl manager start`); after that nothing to press.

**Parked accounts.** An account that hits its usage limit is parked: no new agent or loop starts on it. When the limit message names a reset time, it stays parked until then. When it does not (for example "You're out of usage credits"), the dispatcher checks the account 10 minutes later with one tiny call: `claude -p "Reply with the word ok." --no-session-persistence --disable-slash-commands --tools ""` (Codex: `codex exec --skip-git-repo-check --ephemeral --sandbox read-only "Reply with the word ok."`), with the account's own config folder and a 60 s limit. If it answers, the account is un-parked at once. If it is still limited, the next check comes 30 minutes later, then every 60 minutes (`[dispatch] account_checks`). An error or a timeout is recorded and tried again at the next interval. Antigravity accounts have no check and wait for their 1 h timer. A check runs once per account folder on the machine, even when several profiles share it. `pl accounts` shows each parked account's last check, its result and the next one. A loop whose account is parked runs on another account of the same harness and goes back to its own account at its next restart; `[loops.<name>] fallback = false` makes it wait instead. A loop that has not run for 2 of its intervals opens an alert.

Every key can also be edited in the console's Settings tab. Custom harnesses go under `[harnesses.<name>]` with `bin`, `interactive` and `headless`.

## One manager per machine

The manager is on by default: every console starts it when it is not running. It starts each profile's dispatcher (those with `[dispatch] autostart` not false), restarts one that exits, and caps agents across every profile. A dispatcher that already runs for a profile is adopted, never started twice.

```
pl manager start          # start it by hand; a console does this for you
pl manager status         # each profile's dispatcher, agents and GitHub points used and allowed, why it is held
pl manager stop [--all]   # --all also stops the dispatchers
pl manager restart NAME   # start a profile's dispatcher again (after a give-up or a stop), or restart a running one
pl manager stop NAME      # stop one profile's dispatcher; the manager leaves it alone until restart
```

The settings live in `~/.local/state/pl-machine/machine.toml`, which `pl setup` and `pl manager start` create. Without the file the defaults apply:

```toml
[manager]
enabled = true               # false: each console starts its own profile's dispatcher instead of the manager

[limits]
# max_live_agents = 12       # spec/design/plan/run agent windows across every profile; each profile gets a share.
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
| 0 to 9 | Dashboard, Assistant, Ideas, Pipeline, Pull requests, Loops, Alerts, Activity, Settings, Background (the console opens on the Dashboard) |
| 1 | Assistant: a live harness session that runs pl for you; ctrl+t switches chat and idea mode, ctrl+o opens its window, ctrl+r starts over, ctrl+f files an idea it marked ready |
| 3 | Pipeline: every card by column; a red ! marks the cards that need you (a spec or plan to review, a manual card, an agent that waits or died) and n shows only those. enter opens a card, c in browser, w agent window, t try again, m move account, a / x approve / send back a spec or plan, o answer a spec's questions, v move to another column, d drop (asks for an optional reason), h hand off to Manual, f mark done, e edit the card's INPUT in `$EDITOR`, z show or hide Done, u undo a drop (in the Done group). d, h, f, u and v ask first |
| 6 | Alerts (k acknowledges) |
| w | cycle the Dashboard time window |
| s | standup summary of the last 24 hours on the Dashboard (y copies it) |
| a / x | approve / send back the selected card |
| enter | open the whole card on the Pipeline |
| e | open in `$EDITOR` (review screen) |
| D | start or stop this profile's dispatcher (through the manager when it runs it; `pl manager stop` stops the manager) |
| r | refresh |
| ? | what's new (also in the ctrl+p palette) |
| q | quit |

### Skills

The Settings tab has a **Skills, agents and commands** section (ctrl+p "Skills" jumps there). It lists every `.md` under `skills/`, `agents/` and `commands/` of every account, plus `~/.claude/skills` and the shared library, with a read-only preview. `e` edits in the console (ctrl+s shows the diff and keeps `<file>.bak-<time>`; files over 1 MiB or not UTF-8 open only with `E`), `E` uses `$EDITOR`, `n` writes a new one from a template, `d` moves it to `<account folder>/.pl-trash/`. Plugin files are read-only, and a link leading out of the account folder is not followed.

`S` moves a skill into the library (`~/.local/share/pl/skills`, or `[skills] library`, an absolute path that is not HOME or an account folder) and leaves a link in its place. Every Claude session pl starts, except the Assistant, loads the library as a plugin (`--plugin-dir <profile>/state/plugin`), so it runs as `/pl:<name>`; a stage prompt `/<name>` is rewritten to that when neither the account nor the work folder (`.claude/skills/`) has a skill of its own by that name. Other harnesses get the skill inlined: a prompt `/<name> args` becomes "Follow the instructions in <file>". `l` links a library skill into one Claude or Codex account for use outside pl. Same from the shell: `pl skills list`, `pl skills share NAME`, `pl skills link NAME ACCOUNT`.

### Built-in stages

pl ships five skills, one per stage, so a new profile has a working pipeline with no prompts to write:

| Skill | Stage prompt | What it does |
| --- | --- | --- |
| `pl-spec` | `/pl-spec {id}` | turns the card's INPUT into a spec with numbered Given/When/Then acceptance criteria, out of scope and open questions; writes SPEC and moves the card to Spec ready |
| `pl-design` | `/pl-design {id}` | for cards tagged `frontend`: screens, the five states, layout and words; writes DESIGN |
| `pl-plan` | `/pl-plan {id}` | work packages (what, where, the change, tests first, acceptance criteria) and a minimum-change budget; writes PLAN and moves the card to Plan for review |
| `pl-run` | `/pl-run {id}` | builds the approved plan in a git worktree, one package at a time, tests first; opens a pull request with the `review` label and moves the card to PR open |
| `pl-review` | the built-in auto-review loop | reviews pull requests with the `review` label, then labels them ready, rework or failed; never merges |

They use only `pl` and `git` (and `gh` for pull requests), so they run on every harness: Claude gets them as `/pl:<name>` through the library plugin, Codex and Antigravity get them inlined. Stage agents write their result with `pl section <id> SPEC --from <file>` (also DESIGN and PLAN), read a section in full with `pl section <id> INPUT`, and move the card with `pl move`.

`pl setup` copies them into the library, and so does each console start when one is missing or pl ships a newer version. **Edit them** in the Skills section (`e` on a `library` row) or in `~/.local/share/pl/skills/<name>/SKILL.md`. pl never overwrites a copy you edited: when a newer version ships, the console says "built-in X has an update; your edited copy was kept". **Reset one** to the shipped version with `pl skills reset NAME` (it asks first and keeps your copy as `SKILL.md.bak-<time>`).

**Use your own skills instead:** set the stage's `prompt` (for example `/my-spec {id}`) in `config.toml` or the Settings tab, and `[loops.auto-review] prompt` for reviews. An account skill with the same name as a built-in wins over the library copy. A profile made before the built-in skills keeps its prompts; `pl setup --use-builtin-stages --slug NAME` switches it, after a backup of `config.toml`, and prints each prompt it changes.

Other commands: `pl setup`, `pl idea`, `pl list`, `pl review`, `pl approve`, `pl reject`, `pl dispatch`, `pl pull`, `pl adopt`, `pl done`, `pl move`, `pl retry`, `pl board init`, `pl card`, `pl section`, `pl pause`, `pl resume`, `pl accounts`, `pl intent`, `pl standup`, `pl usage`, `pl alerts`, `pl move-agent`, `pl manager`, `pl skills`, `pl whatsnew`. Each has `--help`. `pl --version` prints the installed version.

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

A Claude Code agent in a folder it has never opened stops at the "Is this a project you trust?" prompt. pl shows the card as "waiting: trust the folder" and opens an alert, "agent waiting: trust the folder <path> once (open the window or run claude in it)". It does not restart the agent and never answers the prompt: open the window and trust the folder once.

## Assistant

Tab `1` is a side chat that runs pl for you: "what is stuck?", "retry that card", "install this plugin", "change the review skill". It is a real, interactive harness session in the tmux window `assistant` of this profile's session, started the first time you open the tab. The tab shows its screen and types what you enter; a lone digit answers the harness's numbered permission menu. The window outlives the console, so reopening the console reattaches to the same conversation; if tmux lost the window, pl resumes the saved Claude conversation.

It uses `[assistant] account`, else the first account not parked, and its usage counts on that account. It starts in the profile's `work_dir`, where the code lives (the profile folder when that folder is missing). pl always starts it in the harness's ask-first mode, whatever the account's own default mode is: Claude with `--permission-mode manual`, Codex with `--ask-for-approval on-request --sandbox read-only`. Claude also gets `--add-dir` for the account's config folder, the profile folder and the folder of pl's guide (never HOME), and permission rules through `--settings`. Claude Code checks deny, then ask, then allow:

- **Runs without asking:** reading, listing and searching files, and `pl list`, `pl card`, `pl alerts`, `pl usage`, `pl standup`, `pl manager status`, `pl accounts`, `pl whatsnew`, `pl local-model`, `git status`, `git log`, `git diff`, `gh pr view`, `gh pr list`.
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

With `proactive` on (the default), new or escalated alerts are typed into an idle assistant as one line per dispatcher pass, at most once a minute: `[pl alert] <title> (<key>). Want me to look?`, or "two alerts open: ..." for several. Alert titles hold ids only. pl waits while the assistant shows a numbered menu or text you have not sent yet. A busy assistant gets the line on a later pass; a closed one gets nothing, and the alert still shows on the Alerts tab.

## Standup

`pl standup` prints a short summary of the last 24 hours, ready to paste in a chat standup: PRs merged, opened and closed without merging; ideas added, specs and plans written and approved, cards sent back and done; cards per column and agents running now; what needs you; and errors, if any. Each line has up to five titles under it, never card text.

`--since` takes `24h`, `90m`, `7d`, a date (`2026-09-29`) or a local time (`2026-09-29T09:00`). `--markdown` prints Markdown for docs, `--slack` Slack formatting. PR numbers cover every PR in the `[code_host]` owner's repos (or its `repos`), with how many merged ones are yours (authored by or assigned to you); they come from three GitHub searches. When GitHub cannot answer, the line says `PRs: unavailable` and why.

`--summary` adds two or three plain sentences above the standup, written by the local model (below). When the model is off or fails, the standup prints unchanged and one note on stderr says why there is no summary.

## Local model (optional)

pl can use a small model that runs on your machine, Gemma 4 served by [Ollama](https://ollama.com), for small, bounded jobs. It runs only when `[local_model] enabled = true`. When it is off, unreachable, slow or gives a bad answer, pl works exactly as without it.

Setup: `pl setup` offers it when it creates a new profile, and the answer defaults to yes (also with `--yes`). It then asks before each step: install Ollama if it is missing (`brew install ollama` on macOS; elsewhere it prints https://ollama.com/download and never runs a download script), start the server if it is not running (`brew services start ollama` when brew installed it; otherwise it tells you to run `ollama serve` or open the Ollama app), and download the model with `ollama pull gemma4` (several GB). A step that fails prints a warning and setup goes on. Re-running setup on an existing profile does not ask again and keeps its `[local_model]`; `--local-model` or `--no-local-model` changes it. By hand: install Ollama, run `ollama pull gemma4`, then set:

```toml
[local_model]
enabled = false                    # true turns it on
url = "http://localhost:11434"     # Ollama's address; only localhost, 127.0.0.1 or ::1
model = "gemma4"                   # any model you pulled, for example "gemma4:e4b"
timeout = 30                       # seconds to wait for an answer
```

Only a loopback url is accepted. Any other host is refused, so the text stays on this machine. There is no API key setting, and pl never holds one. The model's answer is shown as plain text only: control characters are removed, its length is capped, and it is never run.

`pl local-model` prints whether it is on, the url, the model, and whether Ollama answers with that model pulled.

What uses it today: `pl standup --summary`.

## License

MIT.
