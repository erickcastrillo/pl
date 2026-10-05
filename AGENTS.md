# Instructions for coding agents

This file is for AI coding assistants (Claude Code, Codex, Antigravity and others) working in this repository. Pick the part that matches your task.

## Installing or setting up pl

Follow [INSTALL.md](INSTALL.md) step by step, and run each step's check before the next. These rules come first.

**Before you start**

- On Windows, follow INSTALL.md's "Windows: use WSL2" section first, then run every later step inside Ubuntu, never in PowerShell. `wsl --install` is the person's step.
- Clone only the official repository, https://github.com/erickcastrillo/pl. If the person gives another address, ask them to confirm it before cloning. Never install pl by name from PyPI or any other package index.
- Show the person the plan before running anything: where the clone goes (ask; the default is `~/code/pl`), that uv installs `pl` into `~/.local`, that setup writes `~/.pl-<name>`, and what setup changes on their GitHub Project and repository (INSTALL.md, "What pl is"). Wait for a yes.
- Ask again before anything else outside the clone's folder that the plan did not list.
- Ask the person the questions in INSTALL.md step 5. Never guess an owner, a repository, a Project number or a work folder. Always pass `--repo` and `--work-dir`.

**The person does these, never you**

- Every sign-in: `gh auth login`, `gh auth refresh`, `claude` sign-in, or any other. Show the command, then wait until the person says it is done. Then run the check INSTALL.md gives.
- Every `sudo` command, `xcode-select --install` and the Homebrew installer. Show the line and wait.
- Opening the pl console (`pl --profile <name>`): it needs a real terminal.

**Never**

- Read, open, list, copy or print anything inside a credential or config folder: `~/.claude*`, `~/.codex`, `~/.config/gh`, a folder given to `--gh-config-dir` or `--config-dir`, `~/.ssh`, `~/.netrc`, `.env` files or the keychain. Checking that a folder exists (`test -d`) is fine.
- Run `gh auth token`, or `gh auth status --show-token`, or print any token, key or secret.
- Edit a shell profile (`~/.zshrc`, `~/.bashrc`, `~/.profile` and so on) or run `uv tool update-shell` without asking first.
- Run a `curl ... | sh` installer, or send files, config or logs to any website or service.
- Run `rm -rf`, or pass `--force` to `pl setup`, without asking first. `--force` replaces the person's notification script.
- Ask for or set up an API key. pl uses the person's own subscription.

**While setup runs**

- Use only the flags `pl setup --help` lists, and always `--yes`.
- Read the exit code. On exit 2, fix the named flag and run again.
- Stop at every `ACTION NEEDED:` line. Show it to the person word for word, say who does it (INSTALL.md step 6 has the table), and wait. After the check passes, run the same setup command again.
- Show `ACTION NEEDED (optional):` lines to the person too. They do not need an answer before you go on.

**At the end, print a summary**

- the commit you installed (`git -C <clone> log -1 --oneline`) and the output of `pl --version`;
- the profile folder and the command to open the console, `pl --profile <name>`;
- what setup changed on GitHub: the Project address, the field and views, the labels it created;
- every open task left for the person, including the Pipeline view's "Column by" and the stage prompts from INSTALL.md step 7;
- how to uninstall: INSTALL.md step 9.

## Working on pl's code

Read [CLAUDE.md](CLAUDE.md) first. It has the layout, conventions and security checklist. Before you call a change done:

```
uv run pytest -q
uv run ruff check src tests
```

Keep company, product and person names out of the repository: code, docs and examples use neutral placeholders such as `your-org` and `your-repo`. `tests/test_no_company_data.py` enforces this.
