# pl

To install, open this folder in your AI coding assistant and ask it: "Read INSTALL.md and set this up for me." The steps are in [INSTALL.md](INSTALL.md).

pl is a terminal console and dispatcher for an idea-to-PR funnel. A card moves through spec, plan, build and pull request. pl starts a coding agent for each stage, headless, in a tmux window. You approve at gates: the spec (optional), the plan, and the merge.

Agents run through the Claude Code, Codex or Antigravity command-line tools (Antigravity is experimental).

## Your subscription

pl runs the harness CLIs you already installed and signed in to, on your own subscription. It never asks for or stores an API key. pl adds no subscription and needs no model API key. If `claude`, `codex` or `agy` does not work in your terminal, pl cannot use it either. pl never reads a harness's credential files.

## Install

Python 3.11 or newer.

```
uv tool install /path/to/pl        # or a git URL
pip install /path/to/pl
```

Then run `pl setup`. It asks for a profile name, your harnesses, where cards live, your GitHub sign-in and the work folder, then writes and checks `~/.pl-<name>/config.toml`. Assistants and scripts can pass every answer as a flag with `pl setup --yes` (see `pl setup --help`); a missing answer exits 2 naming its flag. It never installs a harness or signs in for you.

Bare `pl` in a terminal opens the console. You also need `tmux`, and `gh` if you use GitHub.

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

With more than one profile, let one manager run them all. It starts each profile's dispatcher (those with `[dispatch] autostart` not false), restarts one that exits, and caps agents across every profile.

```
pl manager start          # once; after that every console starts it when it is not running
pl manager status         # each profile's dispatcher, live agents, any hold
pl manager stop [--all]   # --all also stops the dispatchers
```

The limits live in `~/.local/state/pl-machine/machine.toml`, which `pl manager start` creates:

```toml
[limits]
max_live_agents = 8          # agent and loop windows across every profile; more hold new starts
max_agents_memory = "60%"    # past it the largest agent tree is stopped; over 80% of it holds new starts
min_free_memory = "15%"      # less free memory holds new starts
kill_runaway = true          # false: only notify
```

A usage limit hit by one profile parks that account folder for every profile that uses it. Delete `machine.toml` to go back to one dispatcher per console.

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
| 1 to 8 | Dashboard, Needs you, Ideas, Pipeline, Pull requests, Loops, Activity, Settings |
| w | cycle the Dashboard time window |
| s | standup summary of the last 24 hours on the Dashboard (y copies it) |
| a / x | approve / send back the selected card |
| enter | open review on Needs you |
| e | open in `$EDITOR` (review screen) |
| r | refresh |
| q | quit |

Other commands: `pl idea`, `pl list`, `pl approve`, `pl reject`, `pl dispatch`, `pl pull`, `pl adopt`, `pl done`, `pl board init`, `pl card`, `pl pause`, `pl resume`, `pl accounts`, `pl intent`, `pl standup`. Each has `--help`.

## Standup

`pl standup` prints a short summary of the last 24 hours, ready to paste in a chat standup: PRs merged, opened and closed without merging; ideas added, specs and plans written and approved, cards sent back and done; cards per column and agents running now; what needs you; and errors, if any. Each line has up to five titles under it, never card text.

`--since` takes `24h`, `90m`, `7d`, a date (`2026-09-29`) or a local time (`2026-09-29T09:00`). `--markdown` prints Markdown for docs. PR numbers cover every PR in the `[code_host]` owner's repos (or its `repos`), with how many merged ones are yours (authored by or assigned to you); they come from three GitHub searches. When GitHub cannot answer, the line says `PRs: unavailable` and why.

## License

MIT.
