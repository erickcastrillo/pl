# Install pl

This is the one install guide, for a person and for an AI assistant. The main path is **GitHub Projects for the cards and Claude Code for the agents**. Other choices are at the end of step 7.

An AI assistant also follows the rules in [AGENTS.md](AGENTS.md). Steps marked **(you)** are for the person only: sign-ins, `sudo` commands and anything that needs a browser or a dialog box.

## What pl is

pl is a terminal console that moves a piece of work from an idea to a pull request. Each idea becomes a card on a board. pl starts a coding agent for each stage (spec, plan, build) in a tmux window, and stops at gates where you approve: the spec (optional), the plan and the merge.

pl runs the `claude` command you already signed in to, on your own subscription. pl never asks for, reads or stores an API key, a token or a password. It talks to GitHub only through the `gh` command and its sign-in.

What gets installed and changed:

| Where | What |
| --- | --- |
| A folder you pick, for example `~/code/pl` | the pl source code (a git clone) |
| `~/.local/bin/pl` and `~/.local/share/uv/tools/pl-funnel` | the `pl` command, installed by uv |
| `~/.pl-work` (one folder per profile) | settings, state, logs and a small notification script |
| Your GitHub Project | a "pl stage" field and two views, "Pipeline" and "Needs you" |
| Your GitHub repository | six labels: `pl:auto-review`, `pl:ready-for-review`, `pl:merge-ready`, `pl:needs-rework`, `pl:review-failed`, `pl:start` |

Nothing else is changed. pl does not edit your shell profile, does not need `sudo` and sends no telemetry.

## 1. Check the prerequisites

pl runs on **macOS or Linux**. It needs tmux, so Windows is not supported.

Run every check. For a tool that is missing, use the install line for your system. **(you)** run the `sudo` lines and `xcode-select --install` yourself.

| Tool | Check | macOS (Homebrew) | Linux |
| --- | --- | --- | --- |
| git | `git --version` | `xcode-select --install` or `brew install git` | `sudo apt install git` or `sudo dnf install git` |
| tmux | `tmux -V` | `brew install tmux` | `sudo apt install tmux` or `sudo dnf install tmux` |
| gh 2.21 or newer | `gh --version` | `brew install gh` | `sudo apt install gh` or `sudo dnf install gh`; older systems: https://github.com/cli/cli/blob/trunk/docs/install_linux.md |
| uv | `uv --version` | `brew install uv` | `sudo apt install pipx` (or `sudo dnf install pipx`), then `pipx install uv` |
| Claude Code | `claude --version` | `brew install --cask claude-code` | `npm install -g @anthropic-ai/claude-code` (needs Node.js 18 or newer) |
| Python 3.11 or newer | `uv python find '>=3.11'` | nothing to do: if the check prints `No interpreter found`, step 4 downloads Python | same |

Notes:

- Homebrew itself: if `brew --version` fails, **(you)** install it from https://brew.sh.
- Never use `sudo` with `npm install -g`. If npm says permission denied, fix npm's folder first: https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally
- Some tools also offer a `curl ... | sh` installer. This guide avoids them on purpose: a package manager checks what it downloads.

## 2. Sign in (you)

Only you can do these, in your own terminal. An assistant shows you the commands and waits.

**Claude Code.** Run `claude`, sign in when it asks, then type `/exit`. This also creates the folder `~/.claude`, which pl setup checks for.

**GitHub.** Run:

```
gh auth login -s project
```

Pick GitHub.com, HTTPS and "Login with a web browser". The `-s project` part adds the one extra permission pl needs, to read and change GitHub Projects.

Check both. These commands print no secrets:

```
test -d ~/.claude && echo "claude: signed in"
gh auth status
```

`gh auth status` must say "Logged in to github.com account <your login>", and its "Token scopes" line must include `'project'` and `'repo'`. gh always adds `'read:org'` and `'gist'` too; pl uses only `repo` and `project`. If `'project'` is missing, **(you)** run `gh auth refresh -s project`.

## 3. Get the code

Clone pl from its official repository, https://github.com/erickcastrillo/pl. Do not use a fork, a mirror, or a package with a similar name from PyPI.

```
git clone https://github.com/erickcastrillo/pl.git ~/code/pl
cd ~/code/pl
git log -1 --oneline
```

The last line shows the exact version you got.

## 4. Install pl

From the clone's folder:

```
uv tool install . --constraints <(uv export --frozen --no-dev --no-emit-project --no-header)
```

This installs pl with the exact dependency versions in the clone's `uv.lock`, instead of whatever is newest today. It writes no file into the clone. The `<( )` part works in zsh and bash. A warning that uv's folder "is not on your PATH" is expected; the next check deals with it.

Check it:

```
export PATH="$(uv tool dir --bin):$PATH"
uv tool list
pl --version
command -v pl
```

- `uv tool list` shows `pl-funnel v0.3.2` and `- pl`.
- `pl --version` prints `pl 0.3.2`.
- `command -v pl` prints a path inside the folder `uv tool dir --bin` names, usually `~/.local/bin/pl`. On macOS, `/usr/bin/pl` is Apple's property-list tool, not this pl.

An assistant's shell may not keep the `export` line between commands, so an assistant puts it before every `pl` command in this guide.

**(you)** Open a new terminal and run `command -v pl`. If it prints nothing or `/usr/bin/pl`, run `uv tool update-shell` and open another new terminal. That command adds uv's folder to your shell profile, so it is your call.

## 5. Choose your answers

`pl setup` asks these questions. With `--yes` it takes every answer from a flag and asks nothing.

| Question | Flag | Example |
| --- | --- | --- |
| A name for this profile. Its folder becomes `~/.pl-<name in lower case>`. | `--name` | `--name "Work"` |
| Which harness runs the agents? | `--harness` | `--harness claude` |
| Where do cards live? | `--tracker` | `--tracker github-project` |
| Which GitHub user or organisation owns the Project? Usually your own login from `gh auth status`. | `--owner` | `--owner your-login` |
| A new Project, or an existing one? An existing one's number is the end of its web address, `.../projects/7`. | `--create-project`, or `--project-number` | `--project-number 7` |
| Which repository do new cards go in, as issues? | `--repo` | `--repo your-login/your-repo` |
| Which folder do agents work in? A local git clone of that repository. | `--work-dir` | `--work-dir ~/code/your-repo` |

Always pass `--repo` and `--work-dir`. Without them, setup guesses from the folder it runs in, which is the pl clone.

If you have no local clone of your repository yet, make one first, for example `gh repo clone your-login/your-repo ~/code/your-repo`.

## 6. Run setup

For an **existing** Project:

```
export PATH="$(uv tool dir --bin):$PATH"
pl setup --yes \
  --name "Work" \
  --harness claude \
  --tracker github-project --owner your-login --project-number 7 \
  --repo your-login/your-repo \
  --work-dir ~/code/your-repo
echo "EXIT=$?"
```

For a **new** Project, replace `--project-number 7` with `--create-project --title "pl work"`.

Setup does this and nothing more:

- writes `~/.pl-work/config.toml` (only you can read it) and `~/.pl-work/notify`;
- on the Project: adds the "pl stage" field if it is missing, adds any missing stage options while keeping the existing ones, and adds the "Pipeline" and "Needs you" views. It never touches GitHub's built-in Status field;
- on the repository: creates the six labels it lacks. It never changes or deletes a label that exists.

Read the exit code:

| Exit | Meaning | Next |
| --- | --- | --- |
| 0 | Done. | Step 7. |
| 2 | An answer is wrong or missing. The message names the flag, for example `pl setup: --repo: missing`. Nothing was written. | Fix that flag and run the same command again. |
| 3 | Setup saved the profile, but something is left for you. Each line starting with `ACTION NEEDED:` is one task. | Do each task as the table below says, then run the same command again. That is safe: it keeps your settings and saves the old file as `config.toml.bak-<time>`. |

| `ACTION NEEDED:` says | Who does it | Check before running setup again |
| --- | --- | --- |
| install claude and sign in to it | **(you)** steps 1 and 2 | `claude --version` and `test -d ~/.claude` |
| sign in to gh | **(you)** `gh auth login -s project` | `gh auth status` |
| make sure project ... has a single-select field | nothing: it appears when gh was not signed in | run setup again after signing in |
| could not list the labels | nothing: it appears when gh was not signed in | run setup again after signing in |
| add these options to the ... field | **(you)** add the listed options to that field on the Project page, keeping the existing ones | `pl --profile work list` |
| pl is not on your PATH | **(you)** `uv tool update-shell`, then a new terminal | `command -v pl` in the new terminal |
| could not finish the Project's views | run setup again; if it repeats, **(you)** add the views by hand ([docs/github.md](docs/github.md)) | the Project page |

`ACTION NEEDED (optional):` lines do not change the exit code. Every Project gets one, because GitHub's API cannot choose how a board groups its cards. **(you)** Open the Project address setup printed, open the **Pipeline** view, and set **Column by** to **pl stage**.

## 7. Check it works

```
export PATH="$(uv tool dir --bin):$PATH"
pl --version
pl --profile work list
ls -l ~/.pl-work/config.toml
```

- `pl --profile work list` prints `FEATURE PIPELINE board`, then the columns `Inbox`, `Spec ready`, `Plan for review`, `Manual`, `Approved`, `In progress` and `PR open`, each with a count, and no error.
- `ls -l` shows `-rw-------`: only you can read the settings.

Then, **(you)** in your own terminal (the console needs a real terminal):

```
pl --profile work
```

Press `q` to quit. Opening the console also starts the dispatcher in the tmux session `pl-work`. With a GitHub Project, the dispatcher keeps one Claude agent running that reviews pull requests labelled `pl:auto-review` every 30 minutes, on your subscription. To turn it off, add this to `~/.pl-work/config.toml`:

```toml
[loops.auto-review]
enabled = false
```

**Stage prompts.** Setup writes them: each stage runs one of pl's built-in skills (`/pl-spec {id}`, `/pl-design {id}`, `/pl-plan {id}`, `/pl-run {id}`; `{id}` stands for the card id), and setup copies those skills into the skills library. `[stages.design]` is used only for cards tagged `frontend`. To use your own prompt or skill instead, change the `prompt` line in `~/.pl-work/config.toml` or in the console's Settings tab (key `8`). A profile made before the built-in skills keeps its prompts; `pl setup --use-builtin-stages --slug work` switches it after a backup. See "Built-in stages" in the README.

A shortcut for your shell profile, optional. **(you)** add it to `~/.zshrc` or `~/.bashrc`:

```
alias pl-work='PL_CONFIG_DIR=~/.pl-work pl'
```

A first run with an idea: [docs/github.md, section 6](docs/github.md#6-first-run).

**Other choices.** `pl setup --help` lists every flag. The common ones: `--tracker github-issues` keeps cards as plain issues in one repo; `--tracker mcp` uses any MCP board server ([docs/mcp-trackers.md](docs/mcp-trackers.md)); `--harness codex` or `--harness agy` (Antigravity, experimental), with `--config-dir HARNESS=PATH` for a non-default folder; `--gh-config-dir PATH` uses a separate GitHub sign-in ([docs/github.md](docs/github.md#7-several-github-accounts)); `--no-views`, `--sprint`, `--no-labels`, `--no-notify-script` and `--attention-cmd CMD` change what setup creates. On a new profile setup also turns on the optional local model by default (Gemma 4 on Ollama): with `--yes` it runs `brew install ollama` on macOS when Ollama is missing, `brew services start ollama`, and `ollama pull gemma4`, a download of several GB. Ask the person first, and pass `--no-local-model` if they do not want it.

## 8. Update

Once a day, the console and the dispatcher check for a newer release. When there is one, the console shows "pl vA.B.C is available (you have vX.Y.Z)" and `U` shows these commands; `pl update` prints them and `pl update --check` checks now. pl never runs them for you. After the install, pl restarts its own background processes (the manager and the dispatchers) on the new version; running agents and loops keep running. The first time, from a pl older than this feature, open a new console once (or run `pl manager stop`, then `pl manager start`); after that it is automatic.

A reinstall that switches the Python version pl's tool install uses is not picked up by itself: open a new console once (or run `pl manager stop`, then `pl manager start`).

```
cd ~/code/pl
git pull
uv tool install --force --reinstall . --constraints <(uv export --frozen --no-dev --no-emit-project --no-header)
pl --version
```

## 9. Uninstall

Each line is optional; skip what you want to keep. `rm -rf` cannot be undone, so an assistant asks before each one.

```
pl manager stop --all               # only if you ever ran pl manager start
tmux kill-session -t pl-work        # stops the profile's dispatcher and agents
uv tool uninstall pl-funnel         # removes the pl command
rm -rf ~/.pl-work                   # the profile: settings, state, logs
rm -rf ~/.local/state/pl-machine    # only if you ever ran pl manager start
rm -rf ~/code/pl                    # the clone
```

Then remove the `alias pl-work=...` line from your shell profile if you added it. On GitHub, the labels, the "pl stage" field, the views and any Project setup created stay until you delete them there.

## 10. Security notes

**What pl sends, and where.** pl has no telemetry. Its only network request of its own is the update check: at most once a day it runs `git ls-remote --tags --refs https://github.com/erickcastrillo/pl` to read the release tags. It sends nothing about you, needs no sign-in, and gives up after 3 seconds. It remembers the time and the newest tag in `~/.local/state/pl/update.json`. To turn it off, set `PL_NO_UPDATE_CHECK=1` or add `[updates] check = false` to the profile's `config.toml`. It runs `gh`, which talks to GitHub as you, and `claude`, which talks to Anthropic as you. Activity is logged only on your machine, in `~/.pl-work/state`. Installing downloads from your package manager, from GitHub (the clone) and from PyPI (the pinned dependencies, and Python if it was missing).

**Secrets.** pl stores no token, key or password. `config.toml` holds only names, paths and numbers, and only you can read it. For an MCP board, pl reads the server entry from the harness's own MCP file each time and never copies it into its config. pl never reads `~/.claude`, `~/.config/gh` or any other credential file; it only checks that the Claude folder exists.

**What pl can do on your machine.**

- It starts `claude` in windows of its own tmux session (`pl-work`), in your work folder, as you. The agents can do whatever your Claude Code permission settings allow. pl never adds `--dangerously-skip-permissions`.
- It types into its own tmux windows only: a short `sh <script>` line that starts an agent, and Ctrl-C to stop one. Every value in those lines is quoted.
- The agents use your gh sign-in, so they can push branches and open pull requests in repositories you can write to.
- A memory guard watches each agent's processes. Past 150 processes or 25% of memory, it stops what that agent started (SIGTERM, then SIGKILL 5 seconds later). It never signals the agent's shell, the `claude` process or the dispatcher. `[dispatch] kill_runaway = false` makes it only notify you.

**Card text is input for the agents.** An issue that joins the pipeline, one assigned to you or labelled `pl:start`, becomes part of an agent's prompt. Anyone who can edit such an issue can steer that agent, within your Claude permissions. Use pl on repositories where you trust everyone who can edit issues. The auto-review loop checks out and tests pull requests labelled `pl:auto-review`; only people with triage access to the repository can add a label.
