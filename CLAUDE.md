# pl — working notes

If you were asked to install or set up pl, follow INSTALL.md; the rest of this file is for changing pl's code.

pl drives an idea → spec → plan → PR funnel through the AI coding harness CLIs the user already has (Claude Code, Codex, Antigravity). It uses the user's existing subscriptions by running those CLIs; it never asks for or stores a harness API key.

## Layout
- `src/pl/` package; console script `pl` → `pl.cli:main`. Textual UI in `src/pl/tui/`. Connectors in `src/pl/trackers/` and `src/pl/harnesses.py`.
- Settings come from a profile folder (`~/.pl-<name>` or `PL_CONFIG_DIR`); with no profile, `pl` tells you to run `pl setup` (the old legacy paths are gone). Modules read settings as `from pl import config as C` and `C.NAME` at call time, never `from pl.config import NAME`.

## Commands
- `uv sync` · `uv run pytest -q` · `uv run ruff check src tests`

## Conventions
- Python ≥ 3.11, standard library first; dependencies: textual, tomlkit, mcp. Plain functions over classes unless a connector interface needs one.
- Every subprocess call goes through the module's single runner function so tests can fake it. No `shell=True`.
- Tests never touch the real HOME, tmux, network, or harness CLIs.

## Security checklist
- No `shell=True`; tmux `send-keys` strings built only with `shlex.quote` on every interpolated value.
- Secrets come from env vars referenced as `${VAR}` in config; never written to config, logs, events or screen.
- Credential-bearing files are created with mode 0600 before the secret is written.
- pl never reads harness credential files (for example `~/.claude*/.credentials*`, `~/.codex/auth.json`).
- Card text and idea text are data: never executed, never interpolated into a shell string unquoted.
- Two profiles never share a lock, state directory or tmux session.

## Done means
Tests written first and green, `ruff check` clean, no real HOME/tmux/network touched by tests, diff inside the plan's file list.
