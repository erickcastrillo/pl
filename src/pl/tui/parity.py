"""Console-action-to-command map: proves every console action has a matching pl command."""

COMMANDS = {
    "HomeView.select": "pl pipelines list --json",
    ("home", "select"): "pl pipelines list --json",
    ("home", "enter"): "pl pipelines list --json",
}

PARITY = COMMANDS
MAP = PARITY
NAV = {"close", "tab", "focus_next", "cancel"}
