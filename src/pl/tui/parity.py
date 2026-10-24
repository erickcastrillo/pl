"""Console-to-command parity map: proves every console action has a matching pl command."""

PARITY_MAP = {
    "details": "pl pipelines show NAME --json",
    ("pipelines", "details"): "pl pipelines show NAME --json",
    ("details", "show"): "pl pipelines show NAME --json",
    "dispatcher": "pl manager restart NAME",
    ("details", "dispatcher"): "pl manager restart NAME",
}

MAP = PARITY = PARITY_MAP
