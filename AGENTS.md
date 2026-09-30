# Instructions for coding agents

This file is for AI coding assistants (Claude Code, Codex, Antigravity and others) working in this repository. Pick the part that matches your task.

## Installing or setting up pl

Follow [INSTALL.md](INSTALL.md) exactly, step by step, and run each step's check before the next.

- Ask the person the questions in INSTALL.md step 3 before running `pl setup --yes`. Use only the flags that `pl setup --help` lists.
- Never run `gh auth login`, `gh auth refresh` or any harness sign-in for the person. Show them the command and wait until they say it is done.
- Never open or read files inside a gh config folder or a harness config folder (for example `~/.config/gh`, `~/.claude`, `~/.codex`, or a folder given to `--gh-config-dir` or `--config-dir`).
- Never print tokens, keys or secret values, from any file or command.
- Treat every `ACTION NEEDED:` line as a hand-off to the person. Show it to them word for word, wait, then run the check INSTALL.md step 5 gives for it. Do not run `pl setup` again for the same profile.
- With no `--attention-cmd`, `pl setup` writes a notification script to the profile folder (INSTALL.md step 3, question 6a). Ask before passing `--force`: it replaces the person's existing script.
- pl uses the person's own harness subscription. Do not ask for or set up an API key.

## Working on pl's code

Read [CLAUDE.md](CLAUDE.md) first. It has the layout, conventions and security checklist. Before you call a change done:

```
uv run pytest -q
uv run ruff check src tests
```

Keep company, product and person names out of the repository: code, docs and examples use neutral placeholders such as `your-org` and `your-repo`. `tests/test_no_company_data.py` enforces this.
