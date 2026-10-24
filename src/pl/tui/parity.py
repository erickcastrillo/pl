"""Console-action-to-command map: proves every console action has a matching pl command."""

PARITY = {
    "connectors.add": "pl connectors add",
    "connectors.test": "pl connectors test",
    "connectors.revoke": "pl connectors revoke",
    ("connectors", "add"): "pl connectors add",
    ("connectors", "test"): "pl connectors test",
    ("connectors", "revoke"): "pl connectors revoke",
}

MAP = PARITY
