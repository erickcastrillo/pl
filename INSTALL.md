# Install and set up pl

This guide takes you from a fresh clone to a working `pl`. It is written for a person and for an AI coding assistant (Claude Code, Codex or Antigravity) reading it together. A person can start by telling their assistant: "Read INSTALL.md and set this up for me."

Each step says how to check it worked. Do not move on until the check passes.

## 0. Who does what

- **The assistant** runs the commands in this guide and reads their output.
- **You**, the person, answer the questions in step 3 and do every step marked **(you)**. These are sign-ins: to GitHub and to your harness. Only you can do them, in your own terminal.
- pl uses your own harness subscription. It runs the `claude`, `codex` or `agy` command you already installed and signed in to. pl never asks for, reads or stores an API key or a harness login.

Rules for the assistant:

- Never run `gh auth login`, `gh auth refresh` or any harness sign-in for the person. Show the command and wait.
- Never open or read files inside a gh config folder or a harness config folder (for example `~/.claude`, `~/.codex`, `~/.config/gh`).
- Never print tokens or secret values.

## 1. Check the requirements

Run each check. If one fails, install the tool with the hint on the same line, then run the check again.

| Tool | Check | Install hint |
| --- | --- | --- |
| uv | `uv --version` | https://docs.astral.sh/uv/getting-started/installation/ |
| Python 3.11 or newer | `uv python find '>=3.11'` | If this errors with "No interpreter found", continue. Step 2 fetches Python automatically. |
| git | `git --version` | https://git-scm.com/downloads |
| gh (GitHub CLI) | `gh --version` | https://cli.github.com |
| tmux | `tmux -V` | `brew install tmux` (macOS) or `sudo apt install tmux` (Debian, Ubuntu) |
| A harness CLI | `command -v claude`, `command -v codex` or `command -v agy` | Claude Code: https://docs.claude.com/en/docs/claude-code/setup. Codex: https://github.com/openai/codex. Antigravity: https://antigravity.google |

pl needs tmux: the dispatcher starts every agent in a tmux window.

You need at least one harness. Installing it and signing in to it is outside pl. **(you)** Start the harness once in your own terminal and sign in. Signing in also creates its config folder (`~/.claude` for Claude Code, `~/.codex` for Codex), which pl setup checks for. Antigravity support in pl is experimental.

**(you)** Sign in to GitHub before step 4, so setup can check your GitHub Project:

```
gh auth login
gh auth refresh -s project
```

The second line is needed only for GitHub Projects. Check it worked:

```
gh auth status
```

It should say "Logged in to github.com account <your login>". Look at the line that starts with "Token scopes", for example `- Token scopes: 'gist', 'read:org', 'project', 'repo'`. It must include 'project'. If it does not, **(you)** run `gh auth refresh -s project`.

If you use several GitHub accounts, you can give pl its own sign-in folder instead. See step 3, question 4, and [Several GitHub accounts](docs/github.md#7-several-github-accounts).

## 2. Install pl

From the clone's folder:

```
uv tool install .
```

If you will change pl's code, install it in editable mode instead, so your edits take effect without reinstalling:

```
uv tool install --editable .
```

Check it worked. An assistant's shell often has an old PATH, so the assistant puts uv's tool folder first and checks:

```
export PATH="$(uv tool dir --bin):$PATH"
command -v pl
```

The path must be inside the folder `uv tool dir --bin` prints (usually `~/.local/bin/pl`). On macOS, `/usr/bin/pl` is a different program (Apple's property-list tool), not this pl. The assistant repeats that `export` line before every pl command in this guide.

**(you)** Open a new terminal and run `command -v pl` to confirm your own shell finds it. If it shows nothing or `/usr/bin/pl`, run this and open another new terminal:

```
uv tool update-shell
```

## 3. Ask the questions

The assistant asks the person these questions in plain words before running setup. Each answer becomes a `pl setup` flag. Run `pl setup --help` to see every flag.

| # | Question | Flag |
| --- | --- | --- |
| 1 | What should this profile be called? For example "Work". Its folder becomes `~/.pl-<slug>`. | `--name "Work"`, optional `--slug work` (lower-case letters, digits and `-`) |
| 2 | Which harnesses will agents use? One or more of claude, codex, agy. | `--harness claude` (repeat for more) |
| 2a | Is each harness's config folder somewhere other than the default (`~/.claude`, `~/.codex`)? | `--config-dir claude=~/.claude-work` (one per harness) |
| 2b | With more than one harness: which one should every stage use? Default: the first. | `--default-account codex` |
| 3 | Where do cards live? | `--tracker github-project`, `--tracker github-issues` or `--tracker mcp` |
| 3a | GitHub Project: which user or organisation owns it? | `--owner your-org` |
| 3b | GitHub Project: an existing one (its number, from the project URL), or a new one? | `--project-number 7`, or `--create-project` with optional `--title "pl work"` |
| 3c | GitHub Project or Issues: which repository do new cards go in? Default: the work folder's GitHub remote. | `--repo your-org/your-repo` |
| 3d | MCP board server: which harness MCP file lists it, and which server is it? | `--mcp-config ./.mcp.json` (default: the work folder's `.mcp.json`, then `~/.claude.json`), `--server NAME` |
| 3e | GitHub Project: set up its views? A "Pipeline" board and a "Needs you" table of cards waiting on you. Default: yes. Views with those names are left alone; a lone default "View 1" is renamed to Pipeline. | nothing for yes; `--no-views` for none |
| 3f | GitHub Project: add a 2-week "Sprint" iteration field and a "This sprint" board? Default: no. | `--sprint`, optional `--sprint-weeks 3` |
| 4 | Which GitHub account? The default gh sign-in, or a separate sign-in folder (useful if you have several GitHub accounts). | nothing for the default; `--gh-config-dir ~/.config/gh-work` for a separate folder |
| 4a | When a GitHub repo is known: which PR labels should pl use? Enter keeps the defaults `pl:auto-review`, `pl:ready-for-review`, `pl:merge-ready`, `pl:needs-rework`, `pl:review-failed`. Setup creates the ones the repo lacks and never changes a label that exists. | nothing for the defaults; `--label-review`, `--label-ready`, `--label-merge-ready`, `--label-rework`, `--label-failed NAME` change one; `--no-create-labels` writes them without creating any; `--no-labels` writes none (pl then does not track PRs) |
| 5 | Which folder should agents work in? This is your project's local clone. Default: the current folder, if it is a git repo. | `--work-dir ~/code/your-repo` |
| 6 | Optional: a command pl runs to notify you. It must be an executable file. Default: none. | `--attention-cmd ~/bin/notify-me` |
| 6a | With no command: create a notification script? Default: yes. Setup writes `~/.pl-SLUG/notify` (mode 0700) and uses it. It shows a desktop notification: `osascript` on macOS, `notify-send` on Linux, else a terminal bell and a line on stderr. `PL_NOTIFY_SOUND=Glass` adds a sound on macOS. An existing `notify` file is kept unless you pass `--force`. To use your own command instead, pass `--attention-cmd` or set `[paths] attention_cmd`; pl runs it as `CMD notify "<title>" "<message>"`. | nothing for yes; `--no-notify-script` for none; `--force` replaces an existing script |

More about GitHub Projects, Issues and sign-ins: [docs/github.md](docs/github.md). More about MCP boards: [docs/mcp-trackers.md](docs/mcp-trackers.md).

An existing Project gets a single-select field named "pl stage" with one option per pl column. Setup creates the field if it is missing. If the field lacks some options, setup adds them in one update that keeps every existing option and its id (`--no-add-missing-stages` prints them instead). `--status-field NAME` picks another field name. pl never touches GitHub's built-in Status field.

## 4. Run setup

Run `pl setup --yes` with the answers. `--yes` means pl asks nothing and takes every answer from the flags. A full example for an existing GitHub Project:

```
pl setup --yes \
  --name "Work" \
  --harness claude \
  --tracker github-project --owner your-org --project-number 7 \
  --repo your-org/your-repo \
  --work-dir ~/code/your-repo
```

For GitHub Issues, drop `--owner` and `--project-number` and use `--tracker github-issues --repo your-org/your-repo`. For a new Project, use `--create-project` in place of `--project-number 7`.

A person working alone can run `pl setup` without flags. It asks the same questions one by one.

At the end, setup prints the shell alias for the profile under "Next:". Keep it for step 6.

Running setup again on a profile that already exists is fine. It says the profile exists and continues: your old values are the defaults, your answers are merged into the old `config.toml` (keys setup does not ask about are kept), and the old file is saved as `config.toml.bak-<timestamp>` (mode 0600). Setup checks the result before it replaces the old file. If the check fails, it exits 3 and the old file is unchanged. On a re-run you can leave out flags whose values are already saved.

## 5. Read the result

Setup ends with an exit code. Check it with `echo $?` right after.

- **Exit 0: done.** Go to step 6.
- **Exit 2: an answer was wrong or missing.** The message names the flag, for example `pl setup: --repo: missing (owner/name; new cards are issues there)`. Nothing was written. Fix that flag and run the same setup command again.
- **Exit 3: something is left for you.** Each line starting with `ACTION NEEDED:` is one task. The assistant shows every such line to the person and waits until they say it is done. Then it fixes the item and runs the same `pl setup --yes ...` command again. Setup continues the saved profile, so flags whose values are already saved may be left out. If setup instead says "not saved" or the profile "does not pass its checks", it lists the settings that failed. Fix those flags or lines in `~/.pl-<slug>/config.toml` and run setup again.

What the person does for each ACTION NEEDED line, and how to check it before re-running setup:

| ACTION NEEDED says | The person does | Then the assistant checks |
| --- | --- | --- |
| install a harness and sign in to it | installs it and signs in | `command -v claude` (or codex, agy) |
| sign in to gh for a folder | runs the `GH_CONFIG_DIR=<path> gh auth login -s project` line it printed | `GH_CONFIG_DIR=<path> gh auth status` exits 0 |
| sign in to gh | runs `gh auth login -s project` | `gh auth status` exits 0 |
| pl is not on your PATH, or another pl is first | nothing; the assistant does step 2's fix | `command -v pl` in a new terminal |
| add options to the stage field | adds the listed options to the field in GitHub, keeping the ones it has | `pl --profile <slug> list` |
| create the PR labels | signs in to gh, then runs the printed `gh label create` lines (or re-runs setup) | `gh label list --repo <owner/name>` |
| add a `[tracker.tools]` table | writes the tools mapping, see [docs/mcp-trackers.md](docs/mcp-trackers.md) | `pl --profile <slug> list` |

If the person had to sign in to gh, setup could not check a GitHub Project's stage field. After they sign in, re-run setup so it checks the field (and creates it if missing).

`ACTION NEEDED (optional):` lines do not change the exit code. For a GitHub Project there is one: GitHub's API cannot choose a board's columns. **(you)** Open the Project URL setup printed, open the **Pipeline** view, and set **Column by** to the "pl stage" field. Cards then show in pl's columns. With `--no-views`, first switch the view to **Board**.

With a GitHub repo, setup writes the five PR labels and creates the ones the repo lacks. If gh is not signed in yet, it prints the `gh label create` commands as an ACTION NEEDED line; run them after signing in, or re-run setup. With `--no-labels`, or an MCP board without `--repo`, setup says "PR checks are off until [code_host] labels are set in config.toml." See the pull requests section of [docs/github.md](docs/github.md).

## 6. Confirm it works

Replace `<slug>` with the profile's folder name (for "Work" it is `work`).

```
pl --profile <slug> list
```

It should print a title line, then the board's stage columns with their cards, without an error. The Done column is hidden. For example:

```
FEATURE PIPELINE board
Inbox  (0)
Spec ready  (0)
Plan for review  (0)
Manual  (0)
Approved  (0)
In progress  (0)
PR open  (0)
```

 An empty board shows empty columns.

**(you)** Open the console in your own terminal. It needs a real terminal, so an assistant cannot open it for you:

```
pl --profile <slug>
```

Add the alias setup printed under "Next:" at the end of its output to your shell profile (for zsh, `~/.zshrc`), then open a new terminal:

```
alias pl-<slug>='PL_CONFIG_DIR=~/.pl-<slug> pl'
```

One more step before agents can run: give each stage a prompt. Setup does not write them yet. Edit `~/.pl-<slug>/config.toml` and add a `prompt` under each `[stages.<stage>]` table (spec, design, plan, run), or use the console's Settings tab. `{id}` stands for the card id:

```toml
[stages.spec]
account = "claude"
prompt = "Write the spec for card {id}."
```

Until then, `pl --profile <slug> dispatch` stops with "set [stages.spec] prompt".

## 7. Troubleshooting

| Problem | Fix |
| --- | --- |
| `pl` runs Apple's property-list tool, or "command not found" | `uv tool update-shell`, then a new terminal. Or `export PATH="$(uv tool dir --bin):$PATH"`. |
| `gh is missing the project scope` | **(you)** `gh auth refresh -s project` (add `GH_CONFIG_DIR=<path>` in front for a separate sign-in folder). |
| `--config-dir: ~/.codex does not exist` | **(you)** Start the harness once and sign in, which creates the folder. Or point at the right one with `--config-dir codex=PATH`. |
| An MCP board: `pl list` fails after setup | The MCP tracker needs a `[tracker.tools]` mapping from pl's card actions to the server's tools. Setup cannot guess it. Follow [docs/mcp-trackers.md](docs/mcp-trackers.md). |

## 8. Updating pl

In the clone, pull the new code and reinstall:

```
git pull
uv tool install --force --reinstall .
```

Plain `--force` may reuse a cached build of the same version and leave the old code installed, so add `--reinstall`. If you installed once with `uv tool install --editable .`, `git pull` is enough: changes apply without reinstalling.

## Not supported yet

- Setup does not write stage prompts. Add them as in step 6.
- Setup does not write the MCP `[tracker.tools]` mapping.
- Codex's TOML MCP config file cannot be used as `--mcp-config`. Use a JSON file with `mcpServers`.
- Antigravity (`agy`) support is experimental.
