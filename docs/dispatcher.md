# One dispatcher per profile

Each profile runs one dispatcher. A second `pl dispatch` for the same profile, from the console, by hand or from a restored tmux window, prints `a dispatcher is already running for this profile (pid N)` and exits without starting anything. A dispatcher that does start prints `dispatcher started for <profile> (pid N)` and logs a `dispatcher_started` event, which the console's Activity tab shows.

## One manager per machine

`pl manager start` runs one detached manager for the whole machine. It starts the dispatcher of every profile whose `[dispatch] autostart` is not false, restarts one that exits (after 10 s, 30 s, 2 min, then 5 min) and gives up on a profile after 5 restarts in an hour. `pl manager status` shows each profile; `pl manager stop` stops the manager and leaves the dispatchers running, `pl manager stop --all` stops them too. Machine state lives in `~/.local/state/pl-machine/` (or `$PL_MACHINE_DIR`).

While a manager runs, a `pl dispatch` it did not start, for example one a tmux restore re-created, prints `this machine is run by pl manager (pid N)` and exits. The manager is not a tmux window, so a restore never brings it back: after a reboot nothing runs until you open a console or run `pl manager start`.

## Keep pl out of tmux session restore (without the manager)

tmux-resurrect and tmux-continuum can re-create the `pl-<profile>` sessions after a reboot and start a dispatcher nobody asked for. pl cannot tell a restored window from one you opened, so keep pl's sessions out of the save file. Do not add `python` or `pl` to `@resurrect-processes` (and do not use `':all:'`), and drop the `pl-*` lines after each save:

```
set -g @resurrect-hook-post-save-all 'f=$(readlink -f ~/.local/share/tmux/resurrect/last); sed -i.bak "/^[a-z_]*	pl-/d" "$f"'
```

The character before `pl-` is a tab. Use `~/.tmux/resurrect/last` if that is where your resurrect saves live. Start the dispatcher yourself with `pl watch` (it starts one when none runs) or `pl dispatch`.

## Runaway agents are stopped

Every ~5 s the dispatcher checks each agent window's process tree. Past `[dispatch] max_agent_processes` (150) or `max_agent_memory` (25%), it sends SIGTERM to everything the harness started, SIGKILL 5 s later to what is left, and notifies you. The pane shell, the harness and the dispatcher are never signalled. Set `kill_runaway = false` to only get the notification.
