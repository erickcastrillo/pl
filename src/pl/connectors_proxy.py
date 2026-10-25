from pl import audit, connectors


def is_write_forbidden(connector: str, tool: str) -> bool:
    return connectors.is_write_forbidden(connector, tool)


def record_tool_call(connector: str, tool: str, access: str = "read", result: str = "ok") -> None:
    audit.append("connector.tool", target=connector, detail={"tool": tool, "access": access, "result": result})


def call_tool(connector: str, tool: str, args: dict | None = None, access: str = "read", handler=None):
    """One connector tool call: refused while a write tool's definition is unapproved; every call is audited."""
    result = "ok"
    try:
        if is_write_forbidden(connector, tool):
            raise PermissionError(f"Connector {connector}: write tool {tool} forbidden while tools are unapproved")
        if handler:
            return handler(tool, args or {})
        return connectors.call(connector, tool, args)
    except PermissionError:
        result = "forbidden"
        raise
    finally:
        record_tool_call(connector, tool, access=access, result=result)
