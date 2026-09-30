# One dispatcher per profile

Each profile runs one dispatcher. A second `pl dispatch` for the same profile, from the console, by hand or from a restored tmux window, prints `a dispatcher is already running for this profile (pid N)` and exits without starting anything. A dispatcher that does start prints `dispatcher started for <profile> (pid N)` and logs a `dispatcher_started` event, which the console's Activity tab shows.

## Keep pl out of tmux session restore

tmux-resurrect and tmux-continuum can re-create the `pl-<profile>` sessions after a reboot and start a dispatcher nobody asked for. pl cannot tell a restored window from one you opened, so keep pl's sessions out of the save file. Do not add `python` or `pl` to `@resurrect-processes` (and do not use `':all:'`), and drop the `pl-*` lines after each save:

```
set -g @resurrect-hook-post-save-all 'f=$(readlink -f ~/.local/share/tmux/resurrect/last); sed -i.bak "/^[a-z_]*	pl-/d" "$f"'
```

The character before `pl-` is a tab. Use `~/.tmux/resurrect/last` if that is where your resurrect saves live. Start the dispatcher yourself with `pl watch` (it starts one when none runs) or `pl dispatch`.

## Runaway agents are stopped

Every ~5 s the dispatcher checks each agent window's process tree. Past `[dispatch] max_agent_processes` (150) or `max_agent_memory` (25%), it sends SIGTERM to everything the harness started, SIGKILL 5 s later to what is left, and notifies you. The pane shell, the harness and the dispatcher are never signalled. Set `kill_runaway = false` to only get the notification.
