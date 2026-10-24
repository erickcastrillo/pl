"""Console-action-to-command map: proves every console action has a matching pl command."""

PARITY = {
    "card.approve": "pl approve", "card.send_back": "pl reject", "card.hold": "pl hold",
    "card.restart": "pl restart", "card.drop": "pl drop",
    ("card", "approve"): "pl approve", ("card", "send_back"): "pl reject",
    ("card", "hold"): "pl hold", ("card", "restart"): "pl restart", ("card", "drop"): "pl drop",
    "approve": "pl approve", "send_back": "pl reject", "hold": "pl hold", "restart": "pl restart", "drop": "pl drop",
    "connectors.add": "pl connectors add", "connectors.test": "pl connectors test", "connectors.revoke": "pl connectors revoke",
    ("connectors", "add"): "pl connectors add", ("connectors", "test"): "pl connectors test", ("connectors", "revoke"): "pl connectors revoke",
}
MAP = PARITY
