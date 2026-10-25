from pl import connectors


def is_write_forbidden(connector: str, tool: str) -> bool:
    return connectors.is_write_forbidden(connector, tool)


def call_tool(connector: str, tool: str, args: dict | None = None, access: str = "read", handler=None):
    if is_write_forbidden(connector, tool):
        raise PermissionError(f"Connector {connector}: write tool {tool} forbidden while tools are unapproved")
    if handler:
        return handler(tool, args or {})
    return connectors.call(connector, tool, args)
