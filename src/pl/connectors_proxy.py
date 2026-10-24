from pl import audit


def record_tool_call(connector: str, tool: str, access: str = "read", result: str = "ok") -> None:
    audit.append(
        "connector.tool",
        target=connector,
        detail={"tool": tool, "access": access, "result": result},
    )


def call_tool(connector: str, tool: str, args: dict | None = None, access: str = "read", handler=None):
    result = "ok"
    try:
        if handler:
            return handler(tool, args or {})
        return {"ok": True}
    except PermissionError:
        result = "forbidden"
        raise
    finally:
        record_tool_call(connector, tool, access=access, result=result)
