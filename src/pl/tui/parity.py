"""Console-to-command parity: maps console actions to equivalent pl CLI commands."""

COMMANDS = {
    "PlApp.switcher": "pl list --json",
    "PlApp.oldest": "pl inbox --json",
    "switcher": "pl list --json",
    "oldest": "pl inbox --json",
}

NAV = {
    "close", "tab", "focus_next", "cancel", "quit", "whatsnew", "update",
    "restart_console", "cycle_window", "refresh",
}
